from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core.models import HopObservation, MetricSnapshot


# 알림 key는 "같은 알림이 계속 살아 있는지" 판단하는 식별자입니다.
# 화면 표시, 복구 이벤트, 액션 로그에서 같은 key를 기준으로 상태를 맞춥니다.
LOSS_ALERT_KEY = "target_loss_20pct_3m"
LATENCY_ALERT_KEY = "target_latency_100ms"
JITTER_ALERT_KEY = "target_jitter_30ms"
SAMPLE_ALERT_KEY = "target_sample_condition"
TIMER_ALERT_KEY = "target_timer_condition"
MOS_ALERT_KEY = "target_mos_below"
ROUTE_IP_ALERT_KEY_PREFIX = "route_ip_present:"


@dataclass(frozen=True)
class AlertRuleConfig:
    """알림 설정 화면에서 사용자가 조정하는 기준값 묶음입니다."""

    loss_enabled: bool = True
    loss_threshold_percent: float = 20.0
    loss_window_seconds: int = 180
    latency_enabled: bool = True
    latency_threshold_ms: float = 100.0
    jitter_enabled: bool = False
    jitter_threshold_ms: float = 30.0
    sample_enabled: bool = True
    sample_window_count: int = 10
    sample_failure_count: int = 10
    timer_enabled: bool = True
    timer_window_seconds: int = 300
    mos_enabled: bool = False
    mos_threshold: float = 3.5
    mos_window_seconds: int = 300


@dataclass(frozen=True)
class AlertEvent:
    """알림 하나를 화면/로그/export에 공통으로 넘기기 위한 표준 형식입니다."""

    key: str
    timestamp: datetime
    start: datetime
    end: datetime
    severity: str
    title: str
    message: str
    series_key: str | None = "target"


def evaluate_target_alerts(
    observations: list[HopObservation],
    *,
    current_target: str = "",
    loss_threshold_percent: float = 20.0,
    loss_window_seconds: int = 180,
    latency_threshold_ms: float = 100.0,
    config: AlertRuleConfig | None = None,
) -> tuple[set[str], list[AlertEvent]]:
    """최종 대상의 최근 샘플을 보고 현재 활성 알림과 새 이벤트를 계산합니다.

    반환값의 첫 번째 set은 "지금도 살아 있는 알림 key"이고,
    두 번째 list는 UI와 액션 로그에 남길 구체적인 이벤트입니다.
    """

    if config is not None:
        loss_threshold_percent = config.loss_threshold_percent
        loss_window_seconds = config.loss_window_seconds
        latency_threshold_ms = config.latency_threshold_ms
        jitter_threshold_ms = config.jitter_threshold_ms
    else:
        jitter_threshold_ms = 30.0
    points = _target_points(observations, current_target)
    if not points:
        return set(), []

    active_keys: set[str] = set()
    events: list[AlertEvent] = []
    if config is None or config.loss_enabled:
        loss_event = _loss_alert(points, loss_threshold_percent, loss_window_seconds)
        if loss_event is not None:
            active_keys.add(loss_event.key)
            events.append(loss_event)
    if config is None or config.latency_enabled:
        latency_event = _latency_alert(points, latency_threshold_ms)
        if latency_event is not None:
            active_keys.add(latency_event.key)
            events.append(latency_event)
    if config is not None and config.jitter_enabled:
        jitter_event = _jitter_alert(
            points,
            jitter_threshold_ms,
            config.sample_window_count,
        )
        if jitter_event is not None:
            active_keys.add(jitter_event.key)
            events.append(jitter_event)
    if config is None or config.sample_enabled:
        sample_event = _sample_count_alert(
            points,
            latency_threshold_ms,
            config.sample_window_count if config else 10,
            config.sample_failure_count if config else 10,
        )
        if sample_event is not None:
            active_keys.add(sample_event.key)
            events.append(sample_event)
    if config is None or config.timer_enabled:
        timer_event = _timer_alert(
            points,
            latency_threshold_ms,
            config.timer_window_seconds if config else 300,
        )
        if timer_event is not None:
            active_keys.add(timer_event.key)
            events.append(timer_event)
    if config is not None and config.mos_enabled:
        mos_event = _mos_alert(points, config.mos_threshold, config.mos_window_seconds)
        if mos_event is not None:
            active_keys.add(mos_event.key)
            events.append(mos_event)
    return active_keys, events


def alert_recovery_event(alert_key: str, timestamp: datetime) -> AlertEvent:
    title = _alert_title_for_key(alert_key)
    return AlertEvent(
        key=f"{alert_key}:ended:{timestamp.isoformat(timespec='seconds')}",
        timestamp=timestamp,
        start=timestamp,
        end=timestamp,
        severity="info",
        title="정상 복구",
        message=f"{title} 정상 복구",
    )


def route_change_alert(timestamp: datetime, summary: str) -> AlertEvent:
    return AlertEvent(
        key=f"route_changed:{timestamp.isoformat()}",
        timestamp=timestamp,
        start=timestamp,
        end=timestamp,
        severity="warning",
        title="경로 변경",
        message=summary,
        series_key=None,
    )


def is_route_alert_key(key: str) -> bool:
    return key.startswith("route_changed:") or key.startswith(ROUTE_IP_ALERT_KEY_PREFIX)


def evaluate_route_ip_alert(
    snapshots: list[MetricSnapshot],
    watched_ip: str,
    timestamp: datetime,
) -> tuple[set[str], list[AlertEvent]]:
    watched_ip = watched_ip.strip()
    if not watched_ip:
        return set(), []
    matching = [snapshot for snapshot in snapshots if snapshot.address == watched_ip]
    if not matching:
        return set(), []
    first = sorted(matching, key=lambda snapshot: snapshot.hop_index)[0]
    key = f"{ROUTE_IP_ALERT_KEY_PREFIX}{watched_ip}"
    return {
        key,
    }, [
        AlertEvent(
            key=key,
            timestamp=timestamp,
            start=timestamp,
            end=timestamp,
            severity="warning",
            title="경로 IP 경고",
            message=f"감시 IP {watched_ip}가 Hop {first.hop_index} 경로에 나타났습니다.",
            series_key=f"hop-{first.hop_index}" if first.hop_index > 0 else "target",
        )
    ]


def _target_points(observations: list[HopObservation], current_target: str) -> list[HopObservation]:
    direct = [
        point
        for point in observations
        if point.hop_index == 0 and (not current_target or point.address == current_target)
    ]
    points = direct or [
        point
        for point in observations
        if point.is_target and (not current_target or point.address == current_target)
    ]
    return sorted(points, key=lambda point: point.timestamp)


def _loss_alert(
    points: list[HopObservation],
    threshold_percent: float,
    window_seconds: int,
) -> AlertEvent | None:
    """정해진 시간 창 안에서 packet loss 비율이 기준을 넘는지 확인합니다."""

    end = points[-1].timestamp
    start = end - timedelta(seconds=window_seconds)
    window = [point for point in points if start <= point.timestamp <= end]
    if len(window) < 2 or (window[-1].timestamp - window[0].timestamp).total_seconds() < window_seconds:
        return None
    failures = sum(1 for point in window if not point.success)
    loss_percent = failures / len(window) * 100
    if loss_percent < threshold_percent:
        return None
    return AlertEvent(
        key=LOSS_ALERT_KEY,
        timestamp=end,
        start=window[0].timestamp,
        end=end,
        severity="critical",
        title="손실 경고",
        message=f"최근 {window_seconds // 60}분 동안 패킷 손실률 {loss_percent:.1f}%가 감지되었습니다.",
    )


def _latency_alert(points: list[HopObservation], threshold_ms: float) -> AlertEvent | None:
    """가장 최근 샘플의 지연 시간이 기준을 넘으면 즉시 경고합니다."""

    latest = points[-1]
    if not latest.success or latest.latency_ms is None or latest.latency_ms < threshold_ms:
        return None
    return AlertEvent(
        key=LATENCY_ALERT_KEY,
        timestamp=latest.timestamp,
        start=latest.timestamp,
        end=latest.timestamp,
        severity="warning",
        title="지연 경고",
        message=f"현재 지연 {latest.latency_ms:.1f} ms가 기준 {threshold_ms:.0f} ms 이상입니다.",
    )


def _jitter_alert(
    points: list[HopObservation],
    threshold_ms: float,
    window_count: int,
) -> AlertEvent | None:
    """최근 N개 샘플의 지연 시간 흔들림이 큰지 확인합니다."""

    window_count = max(int(window_count), 2)
    if len(points) < window_count:
        return None
    window = points[-window_count:]
    latencies = [point.latency_ms for point in window if point.success and point.latency_ms is not None]
    if len(latencies) < 2:
        return None
    jitter_ms = _sample_stdev(latencies)
    if jitter_ms < threshold_ms:
        return None
    return AlertEvent(
        key=JITTER_ALERT_KEY,
        timestamp=window[-1].timestamp,
        start=window[0].timestamp,
        end=window[-1].timestamp,
        severity="warning",
        title="지터 경고",
        message=f"최근 {window_count}개 샘플의 지터 {jitter_ms:.1f} ms가 기준 {threshold_ms:.0f} ms 이상입니다.",
    )


def _sample_count_alert(
    points: list[HopObservation],
    latency_threshold_ms: float,
    window_count: int,
    failure_count: int,
) -> AlertEvent | None:
    """최근 N개 중 실패/고지연 샘플이 몇 개 이상인지 세는 방식의 알림입니다."""

    window_count = max(int(window_count), 1)
    failure_count = max(int(failure_count), 1)
    if len(points) < window_count:
        return None
    window = points[-window_count:]
    bad_points = [
        point
        for point in window
        if not point.success or (point.latency_ms is not None and point.latency_ms >= latency_threshold_ms)
    ]
    if len(bad_points) < min(failure_count, window_count):
        return None
    return AlertEvent(
        key=SAMPLE_ALERT_KEY,
        timestamp=window[-1].timestamp,
        start=window[0].timestamp,
        end=window[-1].timestamp,
        severity="critical",
        title="샘플 불량 경고",
        message=(
            f"최근 {window_count}개 샘플 중 {len(bad_points)}개가 실패했거나 "
            f"기준 {latency_threshold_ms:.0f} ms를 초과했습니다."
        ),
    )


def _timer_alert(
    points: list[HopObservation],
    latency_threshold_ms: float,
    window_seconds: int,
) -> AlertEvent | None:
    """나쁜 상태가 끊기지 않고 일정 시간 이상 이어졌는지 확인합니다."""

    window_seconds = max(int(window_seconds), 1)
    latest = points[-1]
    if not _is_bad_target_point(latest, latency_threshold_ms):
        return None
    bad_tail: list[HopObservation] = []
    for point in reversed(points):
        if not _is_bad_target_point(point, latency_threshold_ms):
            break
        bad_tail.append(point)
    bad_tail.reverse()
    if len(bad_tail) < 2:
        return None
    duration_seconds = (bad_tail[-1].timestamp - bad_tail[0].timestamp).total_seconds()
    if duration_seconds < window_seconds:
        return None
    window_minutes = window_seconds / 60
    duration_label = f"{window_minutes:.1f}m" if window_seconds % 60 else f"{window_seconds // 60}m"
    return AlertEvent(
        key=TIMER_ALERT_KEY,
        timestamp=bad_tail[-1].timestamp,
        start=bad_tail[0].timestamp,
        end=bad_tail[-1].timestamp,
        severity="critical",
        title="지속 장애 경고",
        message=f"실패 또는 기준 {latency_threshold_ms:.0f} ms 이상 상태가 {duration_label} 동안 지속되었습니다.",
    )


def _mos_alert(
    points: list[HopObservation],
    threshold: float,
    window_seconds: int,
) -> AlertEvent | None:
    """VoIP 품질을 거칠게 추정하는 MOS 값이 기준 이하인지 확인합니다."""

    window_seconds = max(int(window_seconds), 1)
    end = points[-1].timestamp
    start = end - timedelta(seconds=window_seconds)
    window = [point for point in points if start <= point.timestamp <= end]
    if len(window) < 2 or (window[-1].timestamp - window[0].timestamp).total_seconds() < window_seconds:
        return None
    mos = estimate_mos(window)
    if mos >= threshold:
        return None
    return AlertEvent(
        key=MOS_ALERT_KEY,
        timestamp=end,
        start=window[0].timestamp,
        end=end,
        severity="critical",
        title="MOS 품질 경고",
        message=f"최근 {window_seconds // 60}분 추정 MOS {mos:.2f}가 기준 {threshold:.1f} 미만입니다.",
    )


def estimate_mos(points: list[HopObservation]) -> float:
    """loss, latency, jitter를 이용해 1.0~5.0 범위의 단순 MOS 추정값을 계산합니다."""

    if not points:
        return 1.0
    total = len(points)
    lost = sum(1 for point in points if not point.success)
    packet_loss = lost / total * 100
    latencies = [point.latency_ms for point in points if point.success and point.latency_ms is not None]
    average_latency = sum(latencies) / len(latencies) if latencies else 0.0
    jitter = _average_consecutive_difference(latencies)
    effective_latency = average_latency + jitter * 2 + 10
    if effective_latency < 160:
        r_value = 93.2 - (effective_latency / 40)
    else:
        r_value = 93.2 - ((effective_latency - 120) / 10)
    r_value -= packet_loss * 2.5
    r_value = min(max(r_value, 0.0), 100.0)
    mos = 1 + (0.035 * r_value) + (0.000007 * r_value * (r_value - 60) * (100 - r_value))
    return min(max(mos, 1.0), 5.0)


def _is_bad_target_point(point: HopObservation, latency_threshold_ms: float) -> bool:
    return not point.success or (point.latency_ms is not None and point.latency_ms >= latency_threshold_ms)


def _average_consecutive_difference(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    differences = [abs(current - previous) for previous, current in zip(values, values[1:])]
    return sum(differences) / len(differences)


def _sample_stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return variance**0.5


def _alert_title_for_key(alert_key: str) -> str:
    if alert_key == LOSS_ALERT_KEY:
        return "손실 경고"
    if alert_key == LATENCY_ALERT_KEY:
        return "지연 경고"
    if alert_key == JITTER_ALERT_KEY:
        return "지터 경고"
    if alert_key == SAMPLE_ALERT_KEY:
        return "샘플 불량 경고"
    if alert_key == TIMER_ALERT_KEY:
        return "지속 장애 경고"
    if alert_key == MOS_ALERT_KEY:
        return "MOS 품질 경고"
    if alert_key.startswith(ROUTE_IP_ALERT_KEY_PREFIX):
        return "경로 IP 경고"
    if alert_key.startswith("route_changed:"):
        return "경로 변경"
    return "알림"
