from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core.models import HopObservation


LOSS_ALERT_KEY = "target_loss_20pct_3m"
LATENCY_ALERT_KEY = "target_latency_100ms"
JITTER_ALERT_KEY = "target_jitter_30ms"
SAMPLE_ALERT_KEY = "target_sample_condition"
TIMER_ALERT_KEY = "target_timer_condition"


@dataclass(frozen=True)
class AlertRuleConfig:
    loss_threshold_percent: float = 20.0
    loss_window_seconds: int = 180
    latency_threshold_ms: float = 100.0
    jitter_threshold_ms: float = 30.0
    sample_window_count: int = 10
    sample_failure_count: int = 10
    timer_window_seconds: int = 300


@dataclass(frozen=True)
class AlertEvent:
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
    loss_event = _loss_alert(points, loss_threshold_percent, loss_window_seconds)
    if loss_event is not None:
        active_keys.add(loss_event.key)
        events.append(loss_event)
    latency_event = _latency_alert(points, latency_threshold_ms)
    if latency_event is not None:
        active_keys.add(latency_event.key)
        events.append(latency_event)
    jitter_event = _jitter_alert(
        points,
        jitter_threshold_ms,
        config.sample_window_count if config else 10,
    )
    if jitter_event is not None:
        active_keys.add(jitter_event.key)
        events.append(jitter_event)
    sample_event = _sample_count_alert(
        points,
        latency_threshold_ms,
        config.sample_window_count if config else 10,
        config.sample_failure_count if config else 10,
    )
    if sample_event is not None:
        active_keys.add(sample_event.key)
        events.append(sample_event)
    timer_event = _timer_alert(
        points,
        latency_threshold_ms,
        config.timer_window_seconds if config else 300,
    )
    if timer_event is not None:
        active_keys.add(timer_event.key)
        events.append(timer_event)
    return active_keys, events


def alert_recovery_event(alert_key: str, timestamp: datetime) -> AlertEvent:
    title = _alert_title_for_key(alert_key)
    return AlertEvent(
        key=f"{alert_key}:ended:{timestamp.isoformat(timespec='seconds')}",
        timestamp=timestamp,
        start=timestamp,
        end=timestamp,
        severity="info",
        title="Alert ended",
        message=f"{title} recovered",
    )


def route_change_alert(timestamp: datetime, summary: str) -> AlertEvent:
    return AlertEvent(
        key=f"route_changed:{timestamp.isoformat()}",
        timestamp=timestamp,
        start=timestamp,
        end=timestamp,
        severity="warning",
        title="Route changed",
        message=summary,
        series_key=None,
    )


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
        title="Loss alert",
        message=f"Packet loss {loss_percent:.1f}% for {window_seconds // 60}m",
    )


def _latency_alert(points: list[HopObservation], threshold_ms: float) -> AlertEvent | None:
    latest = points[-1]
    if not latest.success or latest.latency_ms is None or latest.latency_ms < threshold_ms:
        return None
    return AlertEvent(
        key=LATENCY_ALERT_KEY,
        timestamp=latest.timestamp,
        start=latest.timestamp,
        end=latest.timestamp,
        severity="warning",
        title="Latency alert",
        message=f"Target latency {latest.latency_ms:.1f} ms >= {threshold_ms:.0f} ms",
    )


def _jitter_alert(
    points: list[HopObservation],
    threshold_ms: float,
    window_count: int,
) -> AlertEvent | None:
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
        title="Jitter alert",
        message=f"Target jitter {jitter_ms:.1f} ms >= {threshold_ms:.0f} ms over last {window_count} samples",
    )


def _sample_count_alert(
    points: list[HopObservation],
    latency_threshold_ms: float,
    window_count: int,
    failure_count: int,
) -> AlertEvent | None:
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
        title="Sample count alert",
        message=(
            f"{len(bad_points)} of last {window_count} samples failed or exceeded "
            f"{latency_threshold_ms:.0f} ms"
        ),
    )


def _timer_alert(
    points: list[HopObservation],
    latency_threshold_ms: float,
    window_seconds: int,
) -> AlertEvent | None:
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
        title="Timer alert",
        message=f"Target stayed failed or >= {latency_threshold_ms:.0f} ms for {duration_label}",
    )


def _is_bad_target_point(point: HopObservation, latency_threshold_ms: float) -> bool:
    return not point.success or (point.latency_ms is not None and point.latency_ms >= latency_threshold_ms)


def _sample_stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return variance**0.5


def _alert_title_for_key(alert_key: str) -> str:
    if alert_key == LOSS_ALERT_KEY:
        return "Loss alert"
    if alert_key == LATENCY_ALERT_KEY:
        return "Latency alert"
    if alert_key == JITTER_ALERT_KEY:
        return "Jitter alert"
    if alert_key == SAMPLE_ALERT_KEY:
        return "Sample count alert"
    if alert_key == TIMER_ALERT_KEY:
        return "Timer alert"
    if alert_key.startswith("route_changed:"):
        return "Route changed"
    return "Alert"
