from __future__ import annotations

from datetime import datetime, timedelta

from app.core.alerts import (
    AlertRuleConfig,
    JITTER_ALERT_KEY,
    LATENCY_ALERT_KEY,
    LOSS_ALERT_KEY,
    MOS_ALERT_KEY,
    SAMPLE_ALERT_KEY,
    TIMER_ALERT_KEY,
    alert_recovery_event,
    estimate_mos,
    evaluate_route_ip_alert,
    evaluate_target_alerts,
    route_change_alert,
)
from app.core.models import STATUS_OK, STATUS_TIMEOUT, HopObservation, MetricSnapshot


def test_evaluate_target_alerts_detects_sustained_loss_and_latency() -> None:
    now = datetime(2026, 1, 1, 12, 0, 0)
    observations = [
        HopObservation(now, 0, "198.51.100.10", "Target", True, 20.0, STATUS_OK, True),
        HopObservation(now + timedelta(seconds=45), 0, "198.51.100.10", "Target", False, None, STATUS_TIMEOUT, True),
        HopObservation(now + timedelta(seconds=90), 0, "198.51.100.10", "Target", True, 30.0, STATUS_OK, True),
        HopObservation(now + timedelta(seconds=135), 0, "198.51.100.10", "Target", True, 40.0, STATUS_OK, True),
        HopObservation(now + timedelta(seconds=180), 0, "198.51.100.10", "Target", True, 125.0, STATUS_OK, True),
    ]

    active_keys, events = evaluate_target_alerts(observations, current_target="198.51.100.10")

    assert active_keys == {LOSS_ALERT_KEY, LATENCY_ALERT_KEY}
    assert [event.title for event in events] == ["손실 경고", "지연 경고"]
    assert events[0].message == "최근 3분 동안 패킷 손실률 20.0%가 감지되었습니다."
    assert events[1].message == "현재 지연 125.0 ms가 기준 100 ms 이상입니다."


def test_route_change_alert_uses_unique_timestamp_key() -> None:
    timestamp = datetime(2026, 1, 1, 12, 0, 0)

    event = route_change_alert(timestamp, "changed Hop 1")

    assert event.key == "route_changed:2026-01-01T12:00:00"
    assert event.title == "경로 변경"
    assert event.message == "changed Hop 1"


def test_evaluate_route_ip_alert_detects_watched_ip_in_path() -> None:
    timestamp = datetime(2026, 1, 1, 12, 0, 0)
    snapshots = [
        _snapshot(1, "192.0.2.1"),
        _snapshot(2, "203.0.113.50"),
        _snapshot(3, "198.51.100.10"),
    ]

    active_keys, events = evaluate_route_ip_alert(snapshots, "203.0.113.50", timestamp)

    assert active_keys == {"route_ip_present:203.0.113.50"}
    assert events[0].title == "경로 IP 경고"
    assert events[0].message == "감시 IP 203.0.113.50가 Hop 2 경로에 나타났습니다."
    assert events[0].series_key == "hop-2"


def test_evaluate_target_alerts_uses_custom_rule_config() -> None:
    now = datetime(2026, 1, 1, 12, 0, 0)
    observations = [
        HopObservation(now, 0, "198.51.100.10", "Target", True, 20.0, STATUS_OK, True),
        HopObservation(now + timedelta(seconds=30), 0, "198.51.100.10", "Target", False, None, STATUS_TIMEOUT, True),
        HopObservation(now + timedelta(seconds=60), 0, "198.51.100.10", "Target", True, 90.0, STATUS_OK, True),
    ]

    active_keys, events = evaluate_target_alerts(
        observations,
        current_target="198.51.100.10",
        config=AlertRuleConfig(
            loss_threshold_percent=30.0,
            loss_window_seconds=60,
            latency_threshold_ms=80.0,
        ),
    )

    assert active_keys == {LOSS_ALERT_KEY, LATENCY_ALERT_KEY}
    assert events[0].message == "최근 1분 동안 패킷 손실률 33.3%가 감지되었습니다."
    assert events[1].message == "현재 지연 90.0 ms가 기준 80 ms 이상입니다."


def test_evaluate_target_alerts_respects_disabled_conditions() -> None:
    now = datetime(2026, 1, 1, 12, 0, 0)
    observations = [
        HopObservation(now, 0, "198.51.100.10", "Target", True, 20.0, STATUS_OK, True),
        HopObservation(now + timedelta(seconds=30), 0, "198.51.100.10", "Target", False, None, STATUS_TIMEOUT, True),
        HopObservation(now + timedelta(seconds=60), 0, "198.51.100.10", "Target", True, 120.0, STATUS_OK, True),
    ]

    active_keys, events = evaluate_target_alerts(
        observations,
        current_target="198.51.100.10",
        config=AlertRuleConfig(
            loss_enabled=False,
            latency_enabled=False,
            jitter_enabled=False,
            sample_enabled=False,
            timer_enabled=False,
            loss_threshold_percent=30.0,
            loss_window_seconds=60,
            latency_threshold_ms=80.0,
        ),
    )

    assert active_keys == set()
    assert events == []


def test_evaluate_target_alerts_detects_sample_count_condition() -> None:
    now = datetime(2026, 1, 1, 12, 0, 0)
    observations = [
        HopObservation(now, 0, "198.51.100.10", "Target", True, 20.0, STATUS_OK, True),
        HopObservation(now + timedelta(seconds=1), 0, "198.51.100.10", "Target", False, None, STATUS_TIMEOUT, True),
        HopObservation(now + timedelta(seconds=2), 0, "198.51.100.10", "Target", True, 125.0, STATUS_OK, True),
        HopObservation(now + timedelta(seconds=3), 0, "198.51.100.10", "Target", True, 130.0, STATUS_OK, True),
    ]

    active_keys, events = evaluate_target_alerts(
        observations,
        current_target="198.51.100.10",
        config=AlertRuleConfig(
            loss_threshold_percent=100.0,
            loss_window_seconds=60,
            latency_threshold_ms=100.0,
            sample_window_count=4,
            sample_failure_count=3,
        ),
    )

    assert SAMPLE_ALERT_KEY in active_keys
    assert events[-1].title == "샘플 불량 경고"
    assert events[-1].message == "최근 4개 샘플 중 3개가 실패했거나 기준 100 ms를 초과했습니다."


def test_evaluate_target_alerts_detects_jitter_condition() -> None:
    now = datetime(2026, 1, 1, 12, 0, 0)
    observations = [
        HopObservation(now, 0, "198.51.100.10", "Target", True, 10.0, STATUS_OK, True),
        HopObservation(now + timedelta(seconds=1), 0, "198.51.100.10", "Target", True, 60.0, STATUS_OK, True),
        HopObservation(now + timedelta(seconds=2), 0, "198.51.100.10", "Target", True, 11.0, STATUS_OK, True),
        HopObservation(now + timedelta(seconds=3), 0, "198.51.100.10", "Target", True, 61.0, STATUS_OK, True),
    ]

    active_keys, events = evaluate_target_alerts(
        observations,
        current_target="198.51.100.10",
        config=AlertRuleConfig(
            loss_threshold_percent=100.0,
            loss_window_seconds=60,
            latency_threshold_ms=1000.0,
            jitter_enabled=True,
            jitter_threshold_ms=20.0,
            sample_window_count=4,
            sample_failure_count=4,
        ),
    )

    assert active_keys == {JITTER_ALERT_KEY}
    assert events[0].title == "지연 변동 경고"
    assert events[0].message == "최근 4개 샘플의 지연 변동 28.9 ms가 기준 20 ms 이상입니다."


def test_evaluate_target_alerts_keeps_jitter_disabled_by_default() -> None:
    now = datetime(2026, 1, 1, 12, 0, 0)
    observations = [
        HopObservation(now, 0, "198.51.100.10", "Target", True, 10.0, STATUS_OK, True),
        HopObservation(now + timedelta(seconds=1), 0, "198.51.100.10", "Target", True, 60.0, STATUS_OK, True),
        HopObservation(now + timedelta(seconds=2), 0, "198.51.100.10", "Target", True, 11.0, STATUS_OK, True),
        HopObservation(now + timedelta(seconds=3), 0, "198.51.100.10", "Target", True, 61.0, STATUS_OK, True),
    ]

    active_keys, events = evaluate_target_alerts(observations, current_target="198.51.100.10")

    assert JITTER_ALERT_KEY not in active_keys
    assert events == []


def test_evaluate_target_alerts_detects_timer_condition_and_resets_on_good_sample() -> None:
    now = datetime(2026, 1, 1, 12, 0, 0)
    observations = [
        HopObservation(now, 0, "198.51.100.10", "Target", False, None, STATUS_TIMEOUT, True),
        HopObservation(now + timedelta(seconds=30), 0, "198.51.100.10", "Target", True, 20.0, STATUS_OK, True),
        HopObservation(now + timedelta(seconds=60), 0, "198.51.100.10", "Target", False, None, STATUS_TIMEOUT, True),
        HopObservation(now + timedelta(seconds=90), 0, "198.51.100.10", "Target", False, None, STATUS_TIMEOUT, True),
    ]

    active_keys, events = evaluate_target_alerts(
        observations,
        current_target="198.51.100.10",
        config=AlertRuleConfig(
            loss_threshold_percent=100.0,
            loss_window_seconds=180,
            latency_threshold_ms=100.0,
            timer_window_seconds=60,
        ),
    )

    assert TIMER_ALERT_KEY not in active_keys

    observations.append(
        HopObservation(now + timedelta(seconds=120), 0, "198.51.100.10", "Target", False, None, STATUS_TIMEOUT, True)
    )
    active_keys, events = evaluate_target_alerts(
        observations,
        current_target="198.51.100.10",
        config=AlertRuleConfig(
            loss_threshold_percent=100.0,
            loss_window_seconds=180,
            latency_threshold_ms=100.0,
            timer_window_seconds=60,
        ),
    )

    assert TIMER_ALERT_KEY in active_keys
    assert events[-1].title == "지속 장애 경고"
    assert events[-1].message == "실패 또는 기준 100 ms 이상 상태가 1m 동안 지속되었습니다."


def test_evaluate_target_alerts_detects_mos_condition_when_enabled() -> None:
    now = datetime(2026, 1, 1, 12, 0, 0)
    observations = [
        HopObservation(now, 0, "198.51.100.10", "Target", True, 160.0, STATUS_OK, True),
        HopObservation(now + timedelta(seconds=20), 0, "198.51.100.10", "Target", False, None, STATUS_TIMEOUT, True),
        HopObservation(now + timedelta(seconds=40), 0, "198.51.100.10", "Target", True, 240.0, STATUS_OK, True),
        HopObservation(now + timedelta(seconds=60), 0, "198.51.100.10", "Target", False, None, STATUS_TIMEOUT, True),
    ]

    active_keys, events = evaluate_target_alerts(
        observations,
        current_target="198.51.100.10",
        config=AlertRuleConfig(
            loss_threshold_percent=100.0,
            loss_window_seconds=60,
            latency_threshold_ms=1000.0,
            jitter_threshold_ms=1000.0,
            sample_window_count=4,
            sample_failure_count=4,
            timer_window_seconds=300,
            mos_enabled=True,
            mos_threshold=3.5,
            mos_window_seconds=60,
        ),
    )

    assert active_keys == {MOS_ALERT_KEY}
    assert events[0].title == "MOS 품질 경고"
    assert events[0].message.startswith("최근 1분 추정 MOS ")
    assert events[0].message.endswith("가 기준 3.5 미만입니다.")


def test_estimate_mos_keeps_good_path_above_common_voice_threshold() -> None:
    now = datetime(2026, 1, 1, 12, 0, 0)
    observations = [
        HopObservation(now + timedelta(seconds=index), 0, "198.51.100.10", "Target", True, 20.0, STATUS_OK, True)
        for index in range(5)
    ]

    assert estimate_mos(observations) > 4.0


def test_alert_recovery_event_records_ended_state() -> None:
    timestamp = datetime(2026, 1, 1, 12, 5, 0)

    event = alert_recovery_event(SAMPLE_ALERT_KEY, timestamp)

    assert event.key == "target_sample_condition:ended:2026-01-01T12:05:00"
    assert event.severity == "info"
    assert event.title == "정상 복구"
    assert event.message == "샘플 불량 경고 정상 복구"


def _snapshot(hop_index: int, address: str) -> MetricSnapshot:
    return MetricSnapshot(
        hop_index=hop_index,
        address=address,
        hostname=None,
        samples=1,
        sent=1,
        received=1,
        timeout_count=0,
        current_latency_ms=10.0,
        avg_latency_ms=10.0,
        min_latency_ms=10.0,
        max_latency_ms=10.0,
        loss_percent=0.0,
        recent_loss_percent=0.0,
        jitter_ms=0.0,
        status=STATUS_OK,
    )
