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
    evaluate_target_alerts,
    route_change_alert,
)
from app.core.models import STATUS_OK, STATUS_TIMEOUT, HopObservation


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
    assert [event.title for event in events] == ["Loss alert", "Latency alert"]
    assert events[0].message == "Packet loss 20.0% for 3m"
    assert events[1].message == "Target latency 125.0 ms >= 100 ms"


def test_route_change_alert_uses_unique_timestamp_key() -> None:
    timestamp = datetime(2026, 1, 1, 12, 0, 0)

    event = route_change_alert(timestamp, "changed Hop 1")

    assert event.key == "route_changed:2026-01-01T12:00:00"
    assert event.title == "Route changed"
    assert event.message == "changed Hop 1"


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
    assert events[0].message == "Packet loss 33.3% for 1m"
    assert events[1].message == "Target latency 90.0 ms >= 80 ms"


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
    assert events[-1].title == "Sample count alert"
    assert events[-1].message == "3 of last 4 samples failed or exceeded 100 ms"


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
            jitter_threshold_ms=20.0,
            sample_window_count=4,
            sample_failure_count=4,
        ),
    )

    assert active_keys == {JITTER_ALERT_KEY}
    assert events[0].title == "Jitter alert"
    assert events[0].message == "Target jitter 28.9 ms >= 20 ms over last 4 samples"


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
    assert events[-1].title == "Timer alert"
    assert events[-1].message == "Target stayed failed or >= 100 ms for 1m"


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
    assert events[0].title == "MOS alert"
    assert events[0].message.startswith("Estimated MOS ")
    assert events[0].message.endswith("< 3.5 over 1m")


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
    assert event.title == "Alert ended"
    assert event.message == "Sample count alert recovered"
