from __future__ import annotations

from app.core.models import MetricSnapshot, STATUS_OK


LOSS_WARN_PERCENT = 5.0
LOSS_BAD_PERCENT = 20.0
LATENCY_JUMP_MS = 50.0


def _has_samples(snapshot: MetricSnapshot, minimum: int = 3) -> bool:
    return snapshot.sent >= minimum


def _lossy(snapshot: MetricSnapshot, threshold: float = LOSS_WARN_PERCENT) -> bool:
    return _has_samples(snapshot) and snapshot.loss_percent >= threshold


def _healthy(snapshot: MetricSnapshot) -> bool:
    return _has_samples(snapshot) and snapshot.loss_percent < LOSS_WARN_PERCENT and snapshot.status == STATUS_OK


def analyze_path(snapshots: list[MetricSnapshot], target_snapshot: MetricSnapshot | None = None) -> list[str]:
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
        else:
            earlier_loss = [snapshot for snapshot in sampled_hops[:-1] if _lossy(snapshot)]
            if not earlier_loss:
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

    if final and _healthy(final):
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

    analysis.extend(_detect_segment_issue(sampled_hops, final))

    jitter_hops = [
        snapshot for snapshot in sampled_hops if snapshot.jitter_ms is not None and snapshot.jitter_ms >= 30.0
    ]
    if jitter_hops:
        hop_list = ", ".join(f"Hop {snapshot.hop_index}" for snapshot in jitter_hops[:5])
        analysis.append(f"{hop_list}에서 지연 편차가 큽니다. 간헐적 혼잡 또는 무선 품질 저하 가능성이 있습니다.")
        analysis.append(
            _diagnostic_line(
                "ANALYSIS_JITTER_OR_WIRELESS_CONGESTION",
                f"High latency variation is visible at {hop_list}.",
                "Check Wi-Fi quality, local congestion, and bandwidth saturation during the focus window.",
            )
        )

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
    if len(sampled_hops) < 3:
        return []

    for index, snapshot in enumerate(sampled_hops[:-1]):
        tail = sampled_hops[index:]
        if len(tail) < 2:
            continue
        if all(_lossy(item) for item in tail):
            return [
                f"Hop {snapshot.hop_index} 이후 여러 Hop에서 손실이 이어집니다. 해당 구간 이후 장애 가능성이 있습니다.",
                _diagnostic_line(
                    "ANALYSIS_SEGMENT_LOSS_AFTER_HOP",
                    f"Loss is carried from Hop {snapshot.hop_index} to later hops.",
                    f"Focus on the boundary before Hop {snapshot.hop_index} and export that period for the provider.",
                ),
            ]

    previous_avg = None
    for snapshot in sampled_hops:
        if snapshot.avg_latency_ms is None:
            continue
        if previous_avg is not None and snapshot.avg_latency_ms - previous_avg >= LATENCY_JUMP_MS:
            return [
                f"Hop {snapshot.hop_index} 이후 평균 지연시간이 크게 증가합니다. 해당 구간 이후 혼잡 또는 라우팅 품질 저하 가능성이 있습니다.",
                _diagnostic_line(
                    "ANALYSIS_BANDWIDTH_SATURATION_OR_CONGESTION",
                    f"Average latency jumps by at least {LATENCY_JUMP_MS:.0f} ms at Hop {snapshot.hop_index}.",
                    "Check upload/download saturation, QoS, VPN load, and ISP congestion during the same period.",
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


def _diagnostic_line(code: str, summary: str, action: str) -> str:
    return f"{code}: {summary} Action: {action}"
