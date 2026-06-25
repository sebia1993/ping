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
                "측정된 샘플이 없습니다.",
                "경로를 판단하기 전에 최소 3개 샘플을 측정하세요.",
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
                "최종 대상 샘플이 3개 미만입니다.",
                "측정을 더 진행한 뒤 최종 대상과 이전 Hop을 비교하세요.",
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
                    "첫 Hop에서 시작된 패킷 손실이 최종 대상까지 이어집니다.",
                    "로컬 케이블, Wi-Fi 신호, AP, 게이트웨이를 확인하고 유선 테스트도 시도하세요.",
                )
            )
            analysis.append(
                _cause_line(
                    "CAUSE_LOCAL_LAN_WIFI",
                    "첫 Hop과 최종 대상에 같은 패킷 손실 증상이 보입니다.",
                    "로컬 NIC, 케이블, Wi-Fi/AP, 스위치 포트, 기본 게이트웨이부터 점검하세요.",
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
                    "대상이 ICMP를 차단하는지 확인한 뒤 443 같은 실제 서비스 포트로 TCP 연결 측정을 다시 시도하세요."
                    if final.loss_percent >= 100.0
                    else "서비스 포트, 호스트 방화벽, 상위 구간 필터링을 확인하고 예상 포트로 TCP 연결 측정을 테스트하세요."
                )
                analysis.append(
                    "최종 대상에서 주로 손실이 보입니다. 대상 서버, 방화벽, 서비스 구간 문제 가능성이 있습니다."
                )
                analysis.append(
                    _diagnostic_line(
                        "ANALYSIS_TARGET_ONLY_LOSS_OR_FILTER",
                        "손실이 주로 최종 대상에서 보입니다.",
                        "대상 서비스/방화벽을 확인하고 보통 443 같은 서비스 포트로 TCP 연결 측정을 다시 시도하세요.",
                    )
                )
                analysis.append(
                    _cause_line(
                        target_only_code,
                        "이전 Hop은 정상이나 최종 대상에서 손실 또는 응답 없음이 보입니다.",
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
                    f"중간 Hop에서만 손실이 보입니다: {hop_list}. 최종 대상은 정상입니다.",
                    "최종 대상에도 같은 증상이 없다면 중간 Hop만의 손실을 장애로 판단하지 마세요.",
                )
            )
            analysis.append(
                _cause_line(
                    "CAUSE_INTERMEDIATE_HOP_ICMP_RATE_LIMIT",
                    f"{hop_list}에서는 손실이 보이지만 최종 대상은 정상입니다.",
                    "뒤 Hop까지 같은 손실이 이어지지 않으면 ICMP 응답 제한 또는 방화벽 우선순위 저하로 판단하세요.",
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
                    f"중간 Hop에서만 높은 지연이 보입니다: {hop_list}. 최종 대상은 정상입니다.",
                    "뒤 Hop 또는 최종 대상까지 같은 지연이 이어지지 않으면 혼잡으로 판단하지 마세요.",
                )
            )
            analysis.append(
                _cause_line(
                    "CAUSE_INTERMEDIATE_HOP_ICMP_DEPRIORITIZATION",
                    f"{hop_list}에서는 지연이 높지만 최종 대상은 정상입니다.",
                    "최종 대상도 느려지지 않는다면 ICMP 응답 우선순위 저하 또는 제어평면 제한으로 판단하세요.",
                )
            )
        isolated_jitter = _isolated_middle_jitter_hops(sampled_hops, final)
        if isolated_jitter:
            hop_list = ", ".join(f"Hop {snapshot.hop_index}" for snapshot in isolated_jitter[:5])
            analysis.append(
                f"{hop_list}에서만 지연 변동이 크고 최종 대상은 정상입니다. 중간 장비의 ICMP/제어평면 응답 우선순위 저하 가능성이 큽니다."
            )
            analysis.append(
                _diagnostic_line(
                    "ANALYSIS_MIDDLE_HOP_JITTER_DEPRIORITIZED",
                    f"중간 Hop에서만 높은 지연 변동이 보입니다: {hop_list}. 최종 대상은 정상입니다.",
                    "최종 대상도 함께 흔들리지 않으면 중간 Hop만의 지연 변동을 Wi-Fi 또는 대역폭 혼잡으로 판단하지 마세요.",
                )
            )
            analysis.append(
                _cause_line(
                    "CAUSE_INTERMEDIATE_HOP_JITTER_DEPRIORITIZATION",
                    f"{hop_list}에서는 지연 변동이 크지만 최종 대상은 정상입니다.",
                    "뒤 Hop까지 같은 지연 변동이 이어지지 않으면 ICMP 응답 우선순위 저하 또는 제어평면 제한으로 판단하세요.",
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
                f"{hop_list}에서 높은 지연 변동이 보입니다.",
                "포커스 구간의 Wi-Fi 품질, 로컬 혼잡, 대역폭 포화를 확인하세요.",
            )
        )
        analysis.append(
            _cause_line(
                "CAUSE_JITTER_OR_LOCAL_CONGESTION",
                f"{hop_list}에서 지연 변동이 큽니다.",
                "로컬 Wi-Fi 신호, 링크 사용률, VPN 부하, 업로드/다운로드 포화 상태와 비교하세요.",
            )
        )

    _append_overlap_guidance(analysis)

    if not analysis:
        analysis.append("현재 표본 기준으로 뚜렷한 손실 또는 지연 증가 구간은 보이지 않습니다.")
        analysis.append(
            _diagnostic_line(
                "ANALYSIS_NO_CLEAR_PATH_ISSUE",
                "현재 샘플에서는 뚜렷한 손실 또는 지연 증가가 보이지 않습니다.",
                "모니터링을 계속하거나 사용자가 문제를 느낀 시간대에 포커스 구간을 적용하세요.",
            )
        )

    analysis.append("주의: 중간 Hop의 패킷 손실은 ICMP 응답 제한 또는 방화벽 정책일 수 있으므로 최종 대상 상태와 함께 판단해야 합니다.")
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
                "상위 구간으로 넘기기 전에 로컬 게이트웨이, 케이블, Wi-Fi/AP, 스위치 포트를 확인하세요."
                if snapshot.hop_index <= 1
                else f"Hop {snapshot.hop_index} 주변의 첫 영향 Hop이 보이는 포커스 내보내기 자료로 전달하세요."
            )
            return [
                f"Hop {snapshot.hop_index} 이후 여러 Hop에서 손실이 이어집니다. 해당 구간 이후 장애 가능성이 있습니다.",
                _diagnostic_line(
                    "ANALYSIS_SEGMENT_LOSS_AFTER_HOP",
                    f"Hop {snapshot.hop_index}부터 뒤 Hop까지 손실이 이어집니다.",
                    f"Hop {snapshot.hop_index} 직전 경계를 중심으로 포커스를 잡고 해당 기간을 제공자에게 전달하세요.",
                ),
                _cause_line(
                    cause_code,
                    f"Hop {snapshot.hop_index}에서 손실이 시작되어 뒤 Hop으로 이어집니다.",
                    cause_action,
                ),
                _cause_line(
                    _provider_handoff_code(snapshot),
                    f"첫 영향 구간은 Hop {snapshot.hop_index}이며 뒤 Hop과 최종 대상도 손실을 이어받습니다.",
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
                    f"Hop {snapshot.hop_index}에서 평균 지연이 {LATENCY_JUMP_MS:.0f} ms 이상 증가합니다.",
                    "같은 시간대의 업로드/다운로드 포화, QoS, VPN 부하, ISP 혼잡을 확인하세요.",
                ),
                _cause_line(
                    "CAUSE_BANDWIDTH_SATURATION",
                    f"Hop {snapshot.hop_index}에서 평균 지연이 {LATENCY_JUMP_MS:.0f} ms 이상 증가합니다.",
                    "같은 시간대의 인터페이스 사용률, 업로드/다운로드 포화, VPN 부하, QoS, 혼잡 상태를 확인하세요.",
                ),
                _cause_line(
                    "CAUSE_PROVIDER_OR_BORDER_CONGESTION",
                    f"Hop {snapshot.hop_index}에서 지연 증가가 시작되어 최종 대상까지 이어집니다.",
                    "정확한 장애 시간대, 첫 영향 Hop, 사용자 영향 메모가 포함된 포커스 보고서를 전달하세요.",
                ),
            ]
        previous_avg = snapshot.avg_latency_ms

    if final and _lossy(final, LOSS_WARN_PERCENT):
        return [
            "최종 대상까지 손실이 이어집니다. 경로 후단 또는 대상 구간 장애 가능성이 있습니다.",
            _diagnostic_line(
                "ANALYSIS_END_TO_END_LOSS",
                "손실이 최종 대상까지 도달했지만 시작 지점은 아직 분리되지 않았습니다.",
                "포커스 구간의 최종 Hop 손실과 이전 Hop을 비교해 같은 증상이 처음 시작되는 지점을 찾으세요.",
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
    return "최종 대상" if snapshot.hop_index <= 0 else f"Hop {snapshot.hop_index}"


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
        return "상위 구간으로 넘기기 전에 유선 테스트, Wi-Fi/AP, 게이트웨이, 로컬 스위치 포트, 로컬 링크 오류를 확인하세요."
    return (
        f"Hop {snapshot.hop_index}가 첫 영향 Hop임을 보여 주고, 최종 대상 영향과 사용자 영향 메모를 포함한 보고서를 만드세요."
    )


def _append_overlap_guidance(analysis: list[str]) -> None:
    symptoms = _detected_symptoms(analysis)
    if len(symptoms) < 2:
        return

    symptom_label = ", ".join(symptoms)
    analysis.append(
        _diagnostic_line(
            "ANALYSIS_MULTIPLE_SYMPTOMS_OVERLAP",
            f"현재 샘플에 여러 증상 유형이 함께 보입니다: {symptom_label}.",
            (
                "최종 대상 영향을 먼저 보고, 같은 증상이 처음 시작되는 Hop을 찾으세요. "
                "중간 Hop에만 보이는 ICMP 증상은 별도로 분리해서 판단하세요."
            ),
        )
    )
    analysis.append(
        _cause_line(
            "CAUSE_MULTIPLE_SYMPTOM_OVERLAP",
            f"포커스 샘플에 겹치는 증상 유형이 포함되어 있습니다: {symptom_label}.",
            (
                "상위 구간으로 넘기기 전에 최종 목적지와 같은 증상이 이어지는 첫 Hop을 우선 확인하고, "
                "최종 대상에 같은 증상이 없으면 고립된 중간 Hop을 원인으로 단정하지 마세요."
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
        return "손실"
    if line.startswith(
        (
            "ANALYSIS_MIDDLE_HOP_LATENCY_DEPRIORITIZED:",
            "ANALYSIS_BANDWIDTH_SATURATION_OR_CONGESTION:",
        )
    ):
        return "지연"
    if line.startswith(
        (
            "ANALYSIS_MIDDLE_HOP_JITTER_DEPRIORITIZED:",
            "ANALYSIS_JITTER_OR_WIRELESS_CONGESTION:",
        )
    ):
        return "지연 변동"
    return None


def _diagnostic_line(code: str, summary: str, action: str) -> str:
    return f"{code}: {summary} 조치: {action}"


def _cause_line(code: str, evidence: str, action: str) -> str:
    return f"{code}: 근거: {evidence} 조치: {action}"
