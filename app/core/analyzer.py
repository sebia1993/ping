from __future__ import annotations

from app.core.models import MetricSnapshot, STATUS_OK


LOSS_WARN_PERCENT = 5.0
LOSS_BAD_PERCENT = 20.0
LATENCY_JUMP_MS = 50.0
JITTER_WARN_MS = 30.0


# 이 파일은 "수집된 수치"를 사람이 이해할 수 있는 진단 문장으로 바꾸는 곳입니다.
# 실제 ping/tracert 실행은 하지 않고, 이미 계산된 MetricSnapshot만 해석합니다.


def _has_samples(snapshot: MetricSnapshot, minimum: int = 3) -> bool:
    return snapshot.sent >= minimum


def _lossy(snapshot: MetricSnapshot, threshold: float = LOSS_WARN_PERCENT) -> bool:
    return _has_samples(snapshot) and snapshot.loss_percent >= threshold


def _healthy(snapshot: MetricSnapshot) -> bool:
    return _has_samples(snapshot) and snapshot.loss_percent < LOSS_WARN_PERCENT and snapshot.status == STATUS_OK


def analyze_path(snapshots: list[MetricSnapshot], target_snapshot: MetricSnapshot | None = None) -> list[str]:
    """PingPlotter식 해석 흐름으로 현재 경로의 의심 구간을 설명합니다.

    기본 원칙은 최종 목적지에 실제 문제가 보이는지 먼저 확인하고,
    같은 증상이 어느 hop부터 이어지는지 찾아 원인을 좁히는 것입니다.
    중간 hop만 나쁜 경우는 ICMP rate-limit 가능성을 별도로 안내합니다.
    """

    if not snapshots and target_snapshot is None:
        return [
            "아직 분석할 측정 데이터가 없습니다.",
            _diagnostic_line(
                "ANALYSIS_NO_DATA",
                "No measured samples are available.",
                "Run at least 3 samples before judging the path.",
            ),
        ]

    analysis: list[str] = []
    final = target_snapshot or (snapshots[-1] if snapshots else None)

    # 샘플 수가 너무 적으면 손실률/평균값이 쉽게 흔들리므로 먼저 경고합니다.
    if final and final.sent < 3:
        analysis.append("측정 표본이 아직 적습니다. 최소 3회 이상 누적 후 장애 가능성 판단의 신뢰도가 올라갑니다.")
        analysis.append(
            _diagnostic_line(
                "ANALYSIS_INSUFFICIENT_SAMPLES",
                "The final target has fewer than 3 samples.",
                "Keep the trace running longer, then compare the final target with earlier hops.",
            )
        )

    sampled_hops = [snapshot for snapshot in snapshots if _has_samples(snapshot)]

    # 최종 대상에 큰 손실이 있을 때는 "첫 hop부터 나쁜지", "최종 대상만 나쁜지"를 먼저 나눕니다.
    if final and _lossy(final, LOSS_BAD_PERCENT):
        first = sampled_hops[0] if sampled_hops else None
        if first and _lossy(first):
            analysis.append(
                "첫 Hop 또는 게이트웨이 구간부터 손실이 보입니다. 단말, 무선, AP, 게이트웨이 구간 문제 가능성이 있습니다."
            )
            analysis.append(
                _diagnostic_line(
                    "ANALYSIS_FIRST_HOP_LAN_WIFI",
                    "Packet loss starts at the first hop and reaches the final target.",
                    "Check the local cable, Wi-Fi signal, AP, gateway, and try a wired test.",
                )
            )
            analysis.append(
                _cause_line(
                    "CAUSE_LOCAL_LAN_WIFI",
                    "The first hop and final target show the same packet-loss symptom.",
                    "Start troubleshooting at the local NIC, cable, Wi-Fi/AP, switch port, or default gateway.",
                )
            )
        else:
            earlier_loss = [snapshot for snapshot in sampled_hops[:-1] if _lossy(snapshot)]
            if not earlier_loss:
                target_only_code = (
                    "CAUSE_TARGET_ICMP_OR_FIREWALL_BLOCK"
                    if final.loss_percent >= 100.0
                    else "CAUSE_FIREWALL_OR_TARGET_FILTER"
                )
                target_only_action = (
                    "Confirm whether the host blocks ICMP, then retry with TCP Connect on the real service port such as 443."
                    if final.loss_percent >= 100.0
                    else "Confirm the service port, host firewall, upstream filtering, and test with TCP Connect on the expected port."
                )
                analysis.append(
                    "최종 대상에서 주로 손실이 보입니다. 대상 서버, 방화벽, 서비스 구간 문제 가능성이 있습니다."
                )
                analysis.append(
                    _diagnostic_line(
                        "ANALYSIS_TARGET_ONLY_LOSS_OR_FILTER",
                        "Loss is visible mainly at the final target.",
                        "Check the target service/firewall and retry with TCP Connect on the service port, usually 443.",
                    )
                )
                analysis.append(
                    _cause_line(
                        target_only_code,
                        "Earlier hops are healthy, but the final target shows loss or timeout.",
                        target_only_action,
                    )
                )

    if final and _healthy(final):
        # 최종 대상은 정상인데 중간 hop만 나쁘면 실제 장애보다 ICMP 응답 제한일 가능성이 큽니다.
        isolated = [
            snapshot
            for snapshot in sampled_hops[:-1]
            if _lossy(snapshot, LOSS_BAD_PERCENT)
        ]
        if isolated:
            hop_list = ", ".join(f"Hop {snapshot.hop_index}" for snapshot in isolated[:5])
            analysis.append(
                f"{hop_list}에서만 손실이 높고 최종 대상은 정상입니다. 중간 Hop ICMP 응답 제한 또는 방화벽 정책 가능성이 있습니다."
            )
            analysis.append(
                _diagnostic_line(
                    "ANALYSIS_MIDDLE_HOP_ICMP_RATE_LIMIT",
                    f"Only intermediate hops show loss: {hop_list}. The final target is healthy.",
                    "Do not treat middle-hop-only loss as an outage unless the final target has the same symptom.",
                )
            )
            analysis.append(
                _cause_line(
                    "CAUSE_INTERMEDIATE_HOP_ICMP_RATE_LIMIT",
                    f"{hop_list} shows loss while the final target remains healthy.",
                    "Treat this as ICMP rate-limit or firewall deprioritization unless later hops inherit the same loss.",
                )
            )
        isolated_latency = _isolated_middle_latency_hops(sampled_hops, final)
        if isolated_latency:
            hop_list = ", ".join(f"Hop {snapshot.hop_index}" for snapshot in isolated_latency[:5])
            analysis.append(
                f"{hop_list}에서만 지연시간이 높고 최종 대상은 정상입니다. 중간 장비의 ICMP 응답 지연 또는 낮은 처리 우선순위 가능성이 있습니다."
            )
            analysis.append(
                _diagnostic_line(
                    "ANALYSIS_MIDDLE_HOP_LATENCY_DEPRIORITIZED",
                    f"Only intermediate hops show high latency: {hop_list}. The final target is healthy.",
                    "Do not treat middle-hop-only latency as congestion unless later hops or the final target inherit it.",
                )
            )
            analysis.append(
                _cause_line(
                    "CAUSE_INTERMEDIATE_HOP_ICMP_DEPRIORITIZATION",
                    f"{hop_list} has high latency while the final target remains healthy.",
                    "Treat this as ICMP response deprioritization or control-plane rate limiting unless the final target also slows down.",
                )
            )
        isolated_jitter = _isolated_middle_jitter_hops(sampled_hops, final)
        if isolated_jitter:
            hop_list = ", ".join(f"Hop {snapshot.hop_index}" for snapshot in isolated_jitter[:5])
            analysis.append(
                f"{hop_list} shows high jitter only at intermediate hops while the final target is healthy. This usually points to ICMP/control-plane deprioritization."
            )
            analysis.append(
                _diagnostic_line(
                    "ANALYSIS_MIDDLE_HOP_JITTER_DEPRIORITIZED",
                    f"Only intermediate hops show high jitter: {hop_list}. The final target is healthy.",
                    "Do not treat middle-hop-only jitter as Wi-Fi or bandwidth congestion unless the final target also varies.",
                )
            )
            analysis.append(
                _cause_line(
                    "CAUSE_INTERMEDIATE_HOP_JITTER_DEPRIORITIZATION",
                    f"{hop_list} has high jitter while the final target remains healthy.",
                    "Treat this as ICMP response deprioritization or control-plane rate limiting unless later hops inherit the same jitter.",
                )
            )

    analysis.extend(_detect_segment_issue(sampled_hops, final))

    jitter_hops = _impact_jitter_hops(sampled_hops, final)
    if jitter_hops:
        hop_list = ", ".join(_snapshot_label(snapshot) for snapshot in jitter_hops[:5])
        analysis.append(f"{hop_list}에서 지연 편차가 큽니다. 간헐적 혼잡 또는 무선 품질 저하 가능성이 있습니다.")
        analysis.append(
            _diagnostic_line(
                "ANALYSIS_JITTER_OR_WIRELESS_CONGESTION",
                f"High latency variation is visible at {hop_list}.",
                "Check Wi-Fi quality, local congestion, and bandwidth saturation during the focus window.",
            )
        )
        analysis.append(
            _cause_line(
                "CAUSE_JITTER_OR_LOCAL_CONGESTION",
                f"Latency variation is high at {hop_list}.",
                "Compare with local Wi-Fi signal, link utilization, VPN load, and upload/download saturation.",
            )
        )

    _append_overlap_guidance(analysis)

    if not analysis:
        analysis.append("현재 표본 기준으로 뚜렷한 손실 또는 지연 증가 구간은 보이지 않습니다.")
        analysis.append(
            _diagnostic_line(
                "ANALYSIS_NO_CLEAR_PATH_ISSUE",
                "No clear loss or latency growth is visible in the current samples.",
                "Keep monitoring or apply a focus range around the user-reported bad period.",
            )
        )

    analysis.append("주의: 중간 Hop의 packet loss는 ICMP rate limit 또는 방화벽 정책일 수 있으므로 최종 대상 상태와 함께 판단해야 합니다.")
    return analysis


def _detect_segment_issue(
    sampled_hops: list[MetricSnapshot],
    final: MetricSnapshot | None,
) -> list[str]:
    """여러 hop에 이어지는 손실이나 큰 지연 증가가 시작되는 지점을 찾습니다."""

    if len(sampled_hops) < 3:
        return []

    for index, snapshot in enumerate(sampled_hops[:-1]):
        tail = sampled_hops[index:]
        if len(tail) < 2:
            continue
        if all(_lossy(item) for item in tail):
            cause_code = "CAUSE_LOCAL_LAN_WIFI" if snapshot.hop_index <= 1 else "CAUSE_ISP_OR_UPSTREAM_SEGMENT"
            cause_action = (
                "Check the local gateway, cable, Wi-Fi/AP, and switch port before escalating."
                if snapshot.hop_index <= 1
                else f"Escalate with a focused export showing the first affected hop around Hop {snapshot.hop_index}."
            )
            return [
                f"Hop {snapshot.hop_index} 이후 여러 Hop에서 손실이 이어집니다. 해당 구간 이후 장애 가능성이 있습니다.",
                _diagnostic_line(
                    "ANALYSIS_SEGMENT_LOSS_AFTER_HOP",
                    f"Loss is carried from Hop {snapshot.hop_index} to later hops.",
                    f"Focus on the boundary before Hop {snapshot.hop_index} and export that period for the provider.",
                ),
                _cause_line(
                    cause_code,
                    f"Loss starts at Hop {snapshot.hop_index} and is inherited by later hops.",
                    cause_action,
                ),
                _cause_line(
                    _provider_handoff_code(snapshot),
                    f"The first affected hop is Hop {snapshot.hop_index}; later hops and the final target inherit the loss.",
                    _provider_handoff_action(snapshot),
                ),
            ]

    previous_avg = None
    for index, snapshot in enumerate(sampled_hops):
        if snapshot.avg_latency_ms is None:
            continue
        if previous_avg is not None and snapshot.avg_latency_ms - previous_avg >= LATENCY_JUMP_MS:
            if not _latency_jump_is_inherited(sampled_hops[index:], final, previous_avg):
                previous_avg = snapshot.avg_latency_ms
                continue
            return [
                f"Hop {snapshot.hop_index} 이후 평균 지연시간이 크게 증가합니다. 해당 구간 이후 혼잡 또는 라우팅 품질 저하 가능성이 있습니다.",
                _diagnostic_line(
                    "ANALYSIS_BANDWIDTH_SATURATION_OR_CONGESTION",
                    f"Average latency jumps by at least {LATENCY_JUMP_MS:.0f} ms at Hop {snapshot.hop_index}.",
                    "Check upload/download saturation, QoS, VPN load, and ISP congestion during the same period.",
                ),
                _cause_line(
                    "CAUSE_BANDWIDTH_SATURATION",
                    f"Average latency jumps by at least {LATENCY_JUMP_MS:.0f} ms at Hop {snapshot.hop_index}.",
                    "Check interface utilization, upload/download saturation, VPN load, QoS, and congestion at the same time window.",
                ),
                _cause_line(
                    "CAUSE_PROVIDER_OR_BORDER_CONGESTION",
                    f"Latency growth starts at Hop {snapshot.hop_index} and is inherited by the final target.",
                    "Send a focused report with the exact bad time window, the first affected hop, and user-impact notes.",
                ),
            ]
        previous_avg = snapshot.avg_latency_ms

    if final and _lossy(final, LOSS_WARN_PERCENT):
        return [
            "최종 대상까지 손실이 이어집니다. 경로 후단 또는 대상 구간 장애 가능성이 있습니다.",
            _diagnostic_line(
                "ANALYSIS_END_TO_END_LOSS",
                "Loss reaches the final target, but the start point is not isolated yet.",
                "Compare the focused final-hop loss with each earlier hop to find the first matching symptom.",
            ),
        ]

    return []


def _isolated_middle_latency_hops(
    sampled_hops: list[MetricSnapshot],
    final: MetricSnapshot | None,
) -> list[MetricSnapshot]:
    if final is None or not _healthy(final) or final.avg_latency_ms is None:
        return []
    return [
        snapshot
        for snapshot in sampled_hops[:-1]
        if snapshot.avg_latency_ms is not None
        and snapshot.avg_latency_ms - final.avg_latency_ms >= LATENCY_JUMP_MS
    ]


def _isolated_middle_jitter_hops(
    sampled_hops: list[MetricSnapshot],
    final: MetricSnapshot | None,
) -> list[MetricSnapshot]:
    if final is None or not _healthy(final) or _jittery(final):
        return []
    return [
        snapshot
        for snapshot in sampled_hops[:-1]
        if _jittery(snapshot)
    ]


def _impact_jitter_hops(
    sampled_hops: list[MetricSnapshot],
    final: MetricSnapshot | None,
) -> list[MetricSnapshot]:
    if final is not None and _jittery(final):
        jittery_hops = [snapshot for snapshot in sampled_hops if _jittery(snapshot)]
        return [*jittery_hops, final]
    if final is not None and _healthy(final):
        return []
    return [snapshot for snapshot in sampled_hops if _jittery(snapshot)]


def _jittery(snapshot: MetricSnapshot) -> bool:
    return (
        _has_samples(snapshot)
        and snapshot.jitter_ms is not None
        and snapshot.jitter_ms >= JITTER_WARN_MS
    )


def _snapshot_label(snapshot: MetricSnapshot) -> str:
    return "Target" if snapshot.hop_index <= 0 else f"Hop {snapshot.hop_index}"


def _latency_jump_is_inherited(
    tail: list[MetricSnapshot],
    final: MetricSnapshot | None,
    baseline_ms: float,
) -> bool:
    threshold = baseline_ms + LATENCY_JUMP_MS
    latencies = [
        snapshot.avg_latency_ms
        for snapshot in tail
        if snapshot.avg_latency_ms is not None
    ]
    if final is not None and final.avg_latency_ms is not None:
        latencies.append(final.avg_latency_ms)
    return len(latencies) >= 2 and all(latency >= threshold for latency in latencies)


def _provider_handoff_code(snapshot: MetricSnapshot) -> str:
    if snapshot.hop_index <= 1:
        return "CAUSE_LOCAL_ACCESS_LINK"
    return "CAUSE_PROVIDER_OR_BORDER_HANDOFF"


def _provider_handoff_action(snapshot: MetricSnapshot) -> str:
    if snapshot.hop_index <= 1:
        return "Test wired, check Wi-Fi/AP, gateway, local switch port, and local link errors before escalating."
    return (
        f"Build a report for the provider showing Hop {snapshot.hop_index} as the first affected hop, "
        "the final target impact, and any user-impact comments."
    )


def _append_overlap_guidance(analysis: list[str]) -> None:
    symptoms = _detected_symptoms(analysis)
    if len(symptoms) < 2:
        return

    symptom_label = ", ".join(symptoms)
    analysis.append(
        _diagnostic_line(
            "ANALYSIS_MULTIPLE_SYMPTOMS_OVERLAP",
            f"Multiple symptom families are present in the current samples: {symptom_label}.",
            (
                "Start with the final target impact, then find the earliest hop where the same symptoms "
                "begin; keep isolated middle-hop ICMP symptoms separate."
            ),
        )
    )
    analysis.append(
        _cause_line(
            "CAUSE_MULTIPLE_SYMPTOM_OVERLAP",
            f"The focused samples include overlapping symptom families: {symptom_label}.",
            (
                "Prioritize the final destination and the first inherited hop before escalating, "
                "and avoid blaming isolated intermediate hops unless the final target shares the symptom."
            ),
        )
    )


def _detected_symptoms(analysis: list[str]) -> list[str]:
    symptoms: list[str] = []
    for line in analysis:
        symptom = _symptom_family(line)
        if symptom is not None and symptom not in symptoms:
            symptoms.append(symptom)
    return symptoms


def _symptom_family(line: str) -> str | None:
    if line.startswith(
        (
            "ANALYSIS_FIRST_HOP_LAN_WIFI:",
            "ANALYSIS_TARGET_ONLY_LOSS_OR_FILTER:",
            "ANALYSIS_MIDDLE_HOP_ICMP_RATE_LIMIT:",
            "ANALYSIS_SEGMENT_LOSS_AFTER_HOP:",
            "ANALYSIS_END_TO_END_LOSS:",
        )
    ):
        return "loss"
    if line.startswith(
        (
            "ANALYSIS_MIDDLE_HOP_LATENCY_DEPRIORITIZED:",
            "ANALYSIS_BANDWIDTH_SATURATION_OR_CONGESTION:",
        )
    ):
        return "latency"
    if line.startswith(
        (
            "ANALYSIS_MIDDLE_HOP_JITTER_DEPRIORITIZED:",
            "ANALYSIS_JITTER_OR_WIRELESS_CONGESTION:",
        )
    ):
        return "jitter"
    return None


def _diagnostic_line(code: str, summary: str, action: str) -> str:
    return f"{code}: {summary} Action: {action}"


def _cause_line(code: str, evidence: str, action: str) -> str:
    return f"{code}: Evidence: {evidence} Action: {action}"
