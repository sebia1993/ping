from __future__ import annotations

import time
from concurrent.futures import Future
from datetime import datetime
from pathlib import Path

from PySide6.QtWidgets import QApplication

from app.core.alerts import AlertRuleConfig
from app.core.models import STATUS_ERROR, STATUS_OK, STATUS_PAUSED, STATUS_TIMEOUT, HopInfo, PingResult
from app.storage.route_log import RouteLogWriter, route_changes_in_range
from app.storage.session_index import SESSION_STATE_ARCHIVED, SESSION_STATE_PAUSED, SessionIndexStore
from app.ui import worker as worker_module
from app.ui.worker import (
    MEASUREMENT_MODE_FINAL_HOP_ONLY,
    MEASUREMENT_MODE_FULL_ROUTE,
    PROBE_ENGINE_TCP_CONNECT,
    RECENT_OBSERVATION_LIMIT,
    MeasurementWorker,
    TargetProbeState,
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_worker_reports_invalid_target_without_starting_network() -> None:
    _app()
    worker = MeasurementWorker("https://example.com", interval_seconds=0, max_cycles=1)
    errors: list[str] = []
    worker.error_message.connect(errors.append)

    worker.run()

    assert errors
    assert "IPv4" in errors[0]


def test_worker_emits_route_change_when_refreshed_trace_differs() -> None:
    _app()
    worker = MeasurementWorker("198.51.100.10", interval_seconds=0, max_cycles=1)
    changes: list[object] = []
    worker.route_changed.connect(changes.append)

    first: Future[list[HopInfo]] = Future()
    first.set_result([
        HopInfo(index=1, address="192.0.2.1", hostname="gateway"),
        HopInfo(index=2, address="198.51.100.10", hostname="target", is_target=True),
    ])
    metrics, hops = worker._refresh_trace_result(None, [], first, first_check=True)

    second: Future[list[HopInfo]] = Future()
    second.set_result([
        HopInfo(index=1, address="192.0.2.254", hostname="backup"),
        HopInfo(index=2, address="198.51.100.10", hostname="target", is_target=True),
    ])
    worker._refresh_trace_result(metrics, hops, second)

    assert len(changes) == 1
    assert getattr(changes[0], "changed_hops") == (1,)


def test_worker_persists_route_change_snapshots(tmp_path) -> None:
    _app()
    worker = MeasurementWorker("198.51.100.10", interval_seconds=0, max_cycles=1)
    route_log_path = tmp_path / "routes.csv"

    with RouteLogWriter(route_log_path) as route_log:
        first: Future[list[HopInfo]] = Future()
        first.set_result([
            HopInfo(index=1, address="192.0.2.1", hostname="gateway"),
            HopInfo(index=2, address="198.51.100.10", hostname="target", is_target=True),
        ])
        metrics, hops = worker._refresh_trace_result(None, [], first, route_log, first_check=True)

        second: Future[list[HopInfo]] = Future()
        second.set_result([
            HopInfo(index=1, address="192.0.2.254", hostname="backup"),
            HopInfo(index=2, address="198.51.100.10", hostname="target", is_target=True),
        ])
        worker._refresh_trace_result(metrics, hops, second, route_log)

    changes = route_changes_in_range(route_log_path, datetime(2026, 1, 1), datetime(2099, 1, 1))

    assert len(changes) == 1
    assert changes[0].changed_hops == (1,)
    assert "Hop 1" in changes[0].summary


def test_worker_rejects_domain_without_dns_lookup() -> None:
    _app()

    worker = MeasurementWorker("missing.example", interval_seconds=0, max_cycles=1)
    errors: list[str] = []
    worker.error_message.connect(errors.append)

    worker.run()

    assert errors == ["IPv4 주소만 입력 가능합니다: missing.example"]


def test_worker_emits_trace_and_one_measurement_cycle(monkeypatch) -> None:
    _app()
    monkeypatch.setattr(
        worker_module,
        "run_traceroute",
        lambda target, timeout_ms, stop_event: [
            HopInfo(index=1, address="192.0.2.1", hostname="gateway"),
            HopInfo(index=2, address="198.51.100.10", hostname="target.example"),
        ],
    )
    monkeypatch.setattr(worker_module, "CommandPingRunner", _FakePingRunner)

    worker = MeasurementWorker("198.51.100.10", interval_seconds=0, max_cycles=1)
    traces: list[list[HopInfo]] = []
    updates: list[tuple[object, object, object, object, object, object]] = []
    statuses: list[str] = []
    worker.trace_completed.connect(traces.append)
    worker.measurement_updated.connect(lambda *args: updates.append(args))
    worker.status_message.connect(statuses.append)

    worker.run()

    assert len(traces) == 1
    assert [hop.index for hop in traces[0]] == [1, 2]
    assert len(updates) == 1
    snapshots = updates[0][0]
    target_snapshot = updates[0][1]
    target_snapshots = updates[0][2]
    observations = updates[0][4]
    assert len(snapshots) == 2
    assert len(target_snapshots) == 1
    assert target_snapshot.sent == 1
    assert len(observations) == 3
    assert statuses[-1] == "측정이 완료되었습니다."


def test_worker_uses_target_fallback_when_traceroute_returns_no_hops(monkeypatch) -> None:
    _app()
    monkeypatch.setattr(worker_module, "run_traceroute", lambda target, timeout_ms, stop_event: [])
    monkeypatch.setattr(worker_module, "CommandPingRunner", _FakePingRunner)

    worker = MeasurementWorker("203.0.113.10", interval_seconds=0, max_cycles=1)
    traces: list[list[HopInfo]] = []
    updates: list[tuple[object, object, object, object, object, object]] = []
    worker.trace_completed.connect(traces.append)
    worker.measurement_updated.connect(lambda *args: updates.append(args))

    worker.run()

    assert len(traces) == 1
    assert traces[0][0].is_target is True
    assert traces[0][0].address == "203.0.113.10"
    assert updates


def test_worker_emits_target_ping_before_slow_traceroute_finishes(monkeypatch) -> None:
    _app()
    monkeypatch.setattr(worker_module, "CommandPingRunner", _FakePingRunner)

    worker = MeasurementWorker(
        "198.51.100.10",
        interval_seconds=0,
        max_cycles=1,
        traceroute_probe=_DelayedTracerouteProbe(
            [
                HopInfo(index=1, address="192.0.2.1", hostname="gateway"),
                HopInfo(index=2, address="198.51.100.10", hostname="target.example"),
            ],
            delay_seconds=2.0,
        ),
    )
    traces: list[list[HopInfo]] = []
    updates: list[tuple[object, object, object, object, object, object]] = []
    worker.trace_completed.connect(traces.append)
    worker.measurement_updated.connect(lambda *args: updates.append(args))

    worker.run()

    assert updates[0][0] == []
    assert updates[0][1].sent >= 1
    assert traces == [] or traces[0]


def test_worker_final_hop_only_skips_traceroute_and_hop_pings() -> None:
    _app()
    traceroute_probe = _FakeTracerouteProbe(
        [
            HopInfo(index=1, address="192.0.2.1", hostname="gateway"),
            HopInfo(index=2, address="198.51.100.10", hostname="target.example"),
        ]
    )
    ping_calls: list[tuple[str, int]] = []

    worker = MeasurementWorker(
        "198.51.100.10",
        interval_seconds=0,
        max_cycles=1,
        targets=["198.51.100.10", "203.0.113.10"],
        ping_probe_factory=lambda timeout_ms: _RecordingPingRunner(timeout_ms, ping_calls),
        traceroute_probe=traceroute_probe,
        measurement_mode=MEASUREMENT_MODE_FINAL_HOP_ONLY,
    )
    traces: list[list[HopInfo]] = []
    changes: list[object] = []
    updates: list[tuple[object, object, object, object, object, object]] = []
    diagnostics: list[object] = []
    worker.trace_completed.connect(traces.append)
    worker.route_changed.connect(changes.append)
    worker.measurement_updated.connect(lambda *args: updates.append(args))
    worker.diagnostics_updated.connect(diagnostics.append)

    worker.run()

    assert traceroute_probe.calls == []
    assert traces == []
    assert changes == []
    assert len(updates) == 1
    assert updates[0][0] == []
    target_snapshots = {snapshot.address: snapshot for snapshot in updates[0][2]}
    assert set(target_snapshots) == {"198.51.100.10", "203.0.113.10"}
    assert all(snapshot.sent == 1 for snapshot in target_snapshots.values())
    assert {target for target, _timeout in ping_calls} == {"198.51.100.10", "203.0.113.10"}
    assert getattr(diagnostics[-1], "tracert_status") == "final hop only"


def test_worker_pause_targets_skips_scheduling_and_marks_snapshot() -> None:
    _app()
    ping_calls: list[tuple[str, int]] = []
    worker = MeasurementWorker(
        "198.51.100.10",
        interval_seconds=0,
        max_cycles=1,
        targets=["198.51.100.10", "203.0.113.10"],
        ping_probe_factory=lambda timeout_ms: _RecordingPingRunner(timeout_ms, ping_calls),
        measurement_mode=MEASUREMENT_MODE_FINAL_HOP_ONLY,
    )
    worker.pause_targets(["203.0.113.10"])
    updates: list[tuple[object, object, object, object, object, object]] = []
    diagnostics: list[object] = []
    worker.measurement_updated.connect(lambda *args: updates.append(args))
    worker.diagnostics_updated.connect(diagnostics.append)

    worker.run()

    target_snapshots = {snapshot.address: snapshot for snapshot in updates[0][2]}
    assert {target for target, _timeout in ping_calls} == {"198.51.100.10"}
    assert target_snapshots["203.0.113.10"].status == STATUS_PAUSED
    assert target_snapshots["203.0.113.10"].sent == 0
    assert getattr(diagnostics[-1], "paused_target_count") == 1


def test_worker_applies_target_specific_interval_override() -> None:
    _app()
    worker = MeasurementWorker(
        "198.51.100.10",
        interval_seconds=1,
        max_cycles=1,
        targets=["198.51.100.10", "203.0.113.10"],
        measurement_mode=MEASUREMENT_MODE_FINAL_HOP_ONLY,
    )

    worker.set_target_interval_seconds(["203.0.113.10"], 5)
    state = TargetProbeState("203.0.113.10", last_started_at=100.0)
    result = PingResult("203.0.113.10", True, 12.0, STATUS_OK, datetime.now())
    state.record_result(result, worker._target_base_interval_seconds("203.0.113.10"), now=101.0)

    assert worker.target_interval_overrides() == {"203.0.113.10": 5}
    assert worker._target_base_interval_seconds("198.51.100.10") == 1
    assert state.current_interval_seconds == 5
    assert state.next_due == 105.0

    worker.set_interval_seconds(2)

    assert worker.target_interval_overrides() == {}
    assert worker._target_base_interval_seconds("203.0.113.10") == 2


def test_worker_auto_promotes_final_hop_only_to_full_route_on_latency_alert() -> None:
    _app()
    traceroute_probe = _FakeTracerouteProbe(
        [
            HopInfo(index=1, address="192.0.2.1", hostname="gateway"),
            HopInfo(index=2, address="198.51.100.10", hostname="target.example"),
        ]
    )
    ping_calls: list[str] = []

    worker = MeasurementWorker(
        "198.51.100.10",
        interval_seconds=0,
        max_cycles=2,
        ping_probe_factory=lambda timeout_ms: _HighLatencyPingRunner(timeout_ms, ping_calls),
        traceroute_probe=traceroute_probe,
        measurement_mode=MEASUREMENT_MODE_FINAL_HOP_ONLY,
    )
    traces: list[list[HopInfo]] = []
    updates: list[tuple[object, object, object, object, object, object]] = []
    statuses: list[str] = []
    worker.trace_completed.connect(traces.append)
    worker.measurement_updated.connect(lambda *args: updates.append(args))
    worker.status_message.connect(statuses.append)

    worker.run()

    assert worker.measurement_mode == MEASUREMENT_MODE_FULL_ROUTE
    assert traceroute_probe.calls == [("198.51.100.10", 1000, False)]
    assert [hop.index for hop in traces[0]] == [1, 2]
    assert updates[-1][0]
    assert any("Auto Full Route enabled" in status for status in statuses)
    assert "192.0.2.1" in ping_calls


def test_worker_route_adjustment_can_be_disabled() -> None:
    _app()
    traceroute_probe = _FakeTracerouteProbe(
        [
            HopInfo(index=1, address="192.0.2.1", hostname="gateway"),
            HopInfo(index=2, address="198.51.100.10", hostname="target.example"),
        ]
    )
    ping_calls: list[str] = []

    worker = MeasurementWorker(
        "198.51.100.10",
        interval_seconds=0,
        max_cycles=2,
        ping_probe_factory=lambda timeout_ms: _HighLatencyPingRunner(timeout_ms, ping_calls),
        traceroute_probe=traceroute_probe,
        measurement_mode=MEASUREMENT_MODE_FINAL_HOP_ONLY,
        auto_full_route_on_alert=False,
    )
    statuses: list[str] = []
    worker.status_message.connect(statuses.append)

    worker.run()

    assert worker.measurement_mode == MEASUREMENT_MODE_FINAL_HOP_ONLY
    assert traceroute_probe.calls == []
    assert not any("Auto Full Route enabled" in status for status in statuses)


def test_worker_route_adjustment_uses_configured_alert_threshold() -> None:
    _app()
    traceroute_probe = _FakeTracerouteProbe(
        [
            HopInfo(index=1, address="192.0.2.1", hostname="gateway"),
            HopInfo(index=2, address="198.51.100.10", hostname="target.example"),
        ]
    )
    ping_calls: list[str] = []

    worker = MeasurementWorker(
        "198.51.100.10",
        interval_seconds=0,
        max_cycles=2,
        ping_probe_factory=lambda timeout_ms: _HighLatencyPingRunner(timeout_ms, ping_calls),
        traceroute_probe=traceroute_probe,
        measurement_mode=MEASUREMENT_MODE_FINAL_HOP_ONLY,
        alert_rule_config=AlertRuleConfig(latency_threshold_ms=500.0),
    )

    worker.run()

    assert worker.measurement_mode == MEASUREMENT_MODE_FINAL_HOP_ONLY
    assert traceroute_probe.calls == []


def test_worker_route_adjustment_can_restore_final_hop_on_recovery() -> None:
    _app()
    traceroute_probe = _FakeTracerouteProbe(
        [
            HopInfo(index=1, address="192.0.2.1", hostname="gateway"),
            HopInfo(index=2, address="198.51.100.10", hostname="target.example"),
        ]
    )
    ping_calls: dict[str, int] = {}

    worker = MeasurementWorker(
        "198.51.100.10",
        interval_seconds=0,
        max_cycles=3,
        ping_probe_factory=lambda timeout_ms: _RecoveringLatencyPingRunner(timeout_ms, ping_calls),
        traceroute_probe=traceroute_probe,
        measurement_mode=MEASUREMENT_MODE_FINAL_HOP_ONLY,
        auto_restore_final_hop_on_recovery=True,
    )
    statuses: list[str] = []
    worker.status_message.connect(statuses.append)

    worker.run()

    assert worker.measurement_mode == MEASUREMENT_MODE_FINAL_HOP_ONLY
    assert traceroute_probe.calls == [("198.51.100.10", 1000, False)]
    assert any("Auto Full Route enabled" in status for status in statuses)
    assert any("Auto Final Hop Only restored" in status for status in statuses)


def test_worker_does_not_stack_duplicate_pings_for_slow_targets(monkeypatch) -> None:
    _app()
    monkeypatch.setattr(worker_module, "run_traceroute", lambda target, timeout_ms, stop_event: [])
    calls: dict[str, int] = {}

    worker = MeasurementWorker(
        "198.51.100.10",
        interval_seconds=1,
        max_cycles=2,
        targets=["198.51.100.10", "203.0.113.10"],
        ping_probe_factory=lambda timeout_ms: _SlowTargetPingRunner(timeout_ms, calls),
    )
    updates: list[tuple[object, object, object, object, object, object]] = []
    worker.measurement_updated.connect(lambda *args: updates.append(args))

    started_at = time.monotonic()
    worker.run()
    elapsed = time.monotonic() - started_at

    # This includes ThreadPoolExecutor cleanup for simulated 1.5s timeout probes.
    # Keep the ceiling tight enough to catch serial probing while avoiding sub-100ms scheduler jitter failures.
    assert elapsed < 3.0
    assert len(updates) == 2
    assert calls["198.51.100.10"] == 2
    assert calls["203.0.113.10"] == 1
    final_target_snapshots = {snapshot.address: snapshot for snapshot in updates[-1][2]}
    assert final_target_snapshots["198.51.100.10"].sent == 2
    assert final_target_snapshots["203.0.113.10"].sent == 1


def test_worker_reuses_ping_probe_per_executor_thread(monkeypatch) -> None:
    _app()
    monkeypatch.setattr(worker_module, "run_traceroute", lambda target, timeout_ms, stop_event: [])
    counters = {"instances": 0, "pings": 0, "closed": 0}

    worker = MeasurementWorker(
        "198.51.100.10",
        interval_seconds=0,
        max_cycles=3,
        ping_probe_factory=lambda timeout_ms: _CountingPingRunner(timeout_ms, counters),
    )

    worker.run()

    assert counters == {"instances": 1, "pings": 3, "closed": 1}


def test_worker_backs_off_repeated_timeout_targets(monkeypatch) -> None:
    _app()
    monkeypatch.setattr(worker_module, "run_traceroute", lambda target, timeout_ms, stop_event: [])
    calls: dict[str, int] = {}

    worker = MeasurementWorker(
        "198.51.100.1",
        interval_seconds=1,
        max_cycles=5,
        targets=["198.51.100.1", "198.51.100.2"],
        ping_probe_factory=lambda timeout_ms: _BackoffPingRunner(timeout_ms, calls),
    )
    updates: list[tuple[object, object, object, object, object, object]] = []
    diagnostics: list[object] = []
    worker.measurement_updated.connect(lambda *args: updates.append(args))
    worker.diagnostics_updated.connect(diagnostics.append)

    worker.run()

    assert len(updates) == 5
    assert calls["198.51.100.1"] >= calls["198.51.100.2"]
    assert calls["198.51.100.2"] < len(updates)
    assert any(getattr(item, "backoff_target_count", 0) >= 1 for item in diagnostics)


def test_target_probe_state_resets_backoff_after_recovery() -> None:
    state = TargetProbeState("198.51.100.2")
    failed = PingResult("198.51.100.2", False, None, STATUS_TIMEOUT, datetime.now())
    recovered = PingResult("198.51.100.2", True, 10.0, STATUS_OK, datetime.now())

    for index in range(10):
        state.record_result(failed, base_interval_seconds=1, now=float(index))

    assert state.consecutive_failures == 10
    assert state.current_interval_seconds == 5.0

    state.record_result(recovered, base_interval_seconds=1, now=11.0)

    assert state.consecutive_failures == 0
    assert state.current_interval_seconds == 1
    assert state.next_due == 12.0


def test_worker_keeps_twenty_targets_responsive_with_many_timeouts(monkeypatch) -> None:
    _app()
    monkeypatch.setattr(worker_module, "run_traceroute", lambda target, timeout_ms, stop_event: [])
    targets = [f"198.51.100.{index}" for index in range(1, 21)]
    calls: dict[str, int] = {}

    worker = MeasurementWorker(
        targets[0],
        interval_seconds=1,
        max_cycles=2,
        targets=targets,
        ping_probe_factory=lambda timeout_ms: _TwentyTargetPingRunner(timeout_ms, calls),
    )
    updates: list[tuple[object, object, object, object, object, object]] = []
    worker.measurement_updated.connect(lambda *args: updates.append(args))

    started_at = time.monotonic()
    worker.run()
    elapsed = time.monotonic() - started_at

    # The expected runtime is dominated by one 1.5s timeout wave plus executor cleanup.
    # Serial probing would take far longer across 20 targets, so this still catches
    # the regression while avoiding scheduler jitter on busy Windows hosts.
    assert elapsed < 3.0
    assert len(updates) == 2
    final_target_snapshots = {snapshot.address: snapshot for snapshot in updates[-1][2]}
    assert len(final_target_snapshots) == 20
    assert final_target_snapshots["198.51.100.1"].sent == 2
    assert final_target_snapshots["198.51.100.20"].sent == 1
    assert calls["198.51.100.1"] == 2
    assert calls["198.51.100.20"] == 1


def test_worker_stop_request_before_run_does_not_emit_trace(monkeypatch) -> None:
    _app()
    monkeypatch.setattr(worker_module, "run_traceroute", lambda target, timeout_ms, stop_event: [])

    worker = MeasurementWorker("198.51.100.10", interval_seconds=0, max_cycles=1)
    traces: list[list[HopInfo]] = []
    statuses: list[str] = []
    worker.trace_completed.connect(traces.append)
    worker.status_message.connect(statuses.append)
    worker.request_stop()

    worker.run()

    assert traces == []
    assert statuses[-1] == "측정이 중지되었습니다."


def test_worker_does_not_sleep_after_final_cycle(monkeypatch) -> None:
    _app()
    monkeypatch.setattr(
        worker_module,
        "run_traceroute",
        lambda target, timeout_ms, stop_event: [HopInfo(index=1, address="198.51.100.10")],
    )
    monkeypatch.setattr(worker_module, "CommandPingRunner", _FakePingRunner)

    worker = MeasurementWorker("198.51.100.10", interval_seconds=5, max_cycles=1)
    started_at = time.monotonic()
    worker.run()
    elapsed = time.monotonic() - started_at

    assert elapsed < 1.0


def test_worker_accumulates_multiple_measurement_cycles(monkeypatch) -> None:
    _app()
    monkeypatch.setattr(
        worker_module,
        "run_traceroute",
        lambda target, timeout_ms, stop_event: [
            HopInfo(index=1, address="192.0.2.1", hostname="gateway"),
            HopInfo(index=2, address="198.51.100.10", hostname="target.example"),
        ],
    )
    monkeypatch.setattr(worker_module, "CommandPingRunner", _FakePingRunner)

    worker = MeasurementWorker("198.51.100.10", interval_seconds=0, max_cycles=5)
    updates: list[tuple[object, object, object, object, object, object]] = []
    worker.measurement_updated.connect(lambda *args: updates.append(args))

    worker.run()

    assert len(updates) == 5
    snapshots = updates[-1][0]
    target_snapshot = updates[-1][1]
    observations = updates[-1][4]
    assert all(snapshot.sent == 5 for snapshot in snapshots)
    assert target_snapshot.sent == 5
    assert len(observations) == 15


def test_worker_fast_stability_run_accumulates_many_cycles(monkeypatch) -> None:
    _app()
    traceroute_probe = _FakeTracerouteProbe(
        [
            HopInfo(index=1, address="192.0.2.1", hostname="gateway"),
            HopInfo(index=2, address="198.51.100.10", hostname="target.example"),
        ]
    )
    pattern_calls: dict[str, int] = {}

    worker = MeasurementWorker(
        "198.51.100.10",
        interval_seconds=0,
        max_cycles=60,
        ping_probe_factory=lambda timeout_ms: _PatternPingRunner(timeout_ms, pattern_calls),
        traceroute_probe=traceroute_probe,
    )
    updates: list[tuple[object, object, object, object, object, object]] = []
    worker.measurement_updated.connect(lambda *args: updates.append(args))

    worker.run()

    assert len(updates) == 60
    snapshots = updates[-1][0]
    target_snapshot = updates[-1][1]
    observations = updates[-1][4]
    assert all(snapshot.sent == 60 for snapshot in snapshots)
    assert target_snapshot.sent == 60
    assert len(observations) == 180


def test_worker_keeps_recent_observations_and_streams_full_session_log(monkeypatch, tmp_path) -> None:
    _app()
    monkeypatch.setattr(
        worker_module,
        "run_traceroute",
        lambda target, timeout_ms, stop_event: [
            HopInfo(index=1, address="192.0.2.1", hostname="gateway"),
            HopInfo(index=2, address="198.51.100.10", hostname="target.example"),
        ],
    )
    monkeypatch.setattr(worker_module, "CommandPingRunner", _FakePingRunner)

    log_path = tmp_path / "session_samples.csv"

    def create_log(cls, target: str, root=None):
        return cls(log_path)

    monkeypatch.setattr(worker_module.SessionLogWriter, "create", classmethod(create_log))

    worker = MeasurementWorker("198.51.100.10", interval_seconds=0, max_cycles=120)
    updates: list[tuple[object, object, object, object, object, object]] = []
    log_paths: list[str] = []
    worker.measurement_updated.connect(lambda *args: updates.append(args))
    worker.session_log_ready.connect(log_paths.append)

    worker.run()

    assert log_paths == [str(log_path)]
    assert len(updates[-1][4]) == RECENT_OBSERVATION_LIMIT
    assert _count_csv_rows(log_path) == 360
    sessions = SessionIndexStore.create(tmp_path).list_sessions(target="198.51.100.10")
    assert len(sessions) == 1
    assert sessions[0].sample_path == log_path
    assert sessions[0].samples == 360
    assert sessions[0].state == SESSION_STATE_ARCHIVED


def test_worker_can_write_session_logs_under_custom_root(monkeypatch, tmp_path) -> None:
    _app()
    monkeypatch.setattr(worker_module, "run_traceroute", lambda target, timeout_ms, stop_event: [])
    monkeypatch.setattr(worker_module, "CommandPingRunner", _FakePingRunner)
    log_root = tmp_path / "soak_session_logs"

    worker = MeasurementWorker(
        "198.51.100.10",
        interval_seconds=0,
        max_cycles=2,
        session_log_root=log_root,
    )
    worker.resumed_from_session_id = "previous-session"
    log_paths: list[str] = []
    worker.session_log_ready.connect(log_paths.append)

    worker.run()

    assert len(log_paths) == 1
    log_path = Path(log_paths[0])
    assert log_path.exists()
    assert log_path.is_relative_to(log_root)
    sessions = SessionIndexStore.create(log_root).list_sessions(target="198.51.100.10")
    assert len(sessions) == 1
    assert sessions[0].sample_path == log_path
    assert sessions[0].samples >= 2
    assert sessions[0].state == SESSION_STATE_ARCHIVED
    assert sessions[0].resumed_from_session_id == "previous-session"


def test_worker_marks_session_paused_when_session_log_write_fails(monkeypatch, tmp_path) -> None:
    _app()
    monkeypatch.setattr(worker_module, "run_traceroute", lambda target, timeout_ms, stop_event: [])
    monkeypatch.setattr(worker_module, "CommandPingRunner", _FakePingRunner)
    log_path = tmp_path / "failed.samples.csv"

    def create_log(cls, target: str, root=None):
        return _FailingSessionLogWriter(log_path)

    monkeypatch.setattr(worker_module.SessionLogWriter, "create", classmethod(create_log))

    worker = MeasurementWorker("198.51.100.10", interval_seconds=0, max_cycles=1)
    errors: list[str] = []
    worker.error_message.connect(errors.append)

    worker.run()

    sessions = SessionIndexStore.create(tmp_path).list_sessions(target="198.51.100.10")
    assert len(sessions) == 1
    assert sessions[0].state == SESSION_STATE_PAUSED
    assert "SESSION_LOG_WRITE_FAILED: RuntimeError" in sessions[0].last_error
    assert any("SESSION_LOG_WRITE_FAILED" in error for error in errors)


def test_worker_thread_start_stop_repeats_without_lingering(monkeypatch) -> None:
    _app()

    for _ in range(3):
        worker = MeasurementWorker(
            "198.51.100.10",
            interval_seconds=1,
            max_cycles=None,
            traceroute_probe=_BlockingTracerouteProbe(),
        )

        worker.start()
        time.sleep(0.05)
        worker.request_stop()

        assert worker.wait(1500) is True
        assert worker.isRunning() is False


def test_worker_records_ping_exceptions_as_error_results(monkeypatch) -> None:
    _app()
    worker = MeasurementWorker("198.51.100.10", interval_seconds=0, max_cycles=1)
    monkeypatch.setattr(worker_module, "CommandPingRunner", _FailingPingRunner)

    results = worker._ping_unique_targets([HopInfo(index=1, address="192.0.2.1")])

    assert results["192.0.2.1"].status == STATUS_ERROR
    assert results["198.51.100.10"].status == STATUS_ERROR


def test_worker_uses_injected_probe_boundaries(monkeypatch) -> None:
    _app()
    traceroute_probe = _FakeTracerouteProbe(
        [
            HopInfo(index=1, address="192.0.2.1", hostname="gateway"),
            HopInfo(index=2, address="198.51.100.10", hostname="target.example"),
        ]
    )
    ping_calls: list[tuple[str, int]] = []

    worker = MeasurementWorker(
        "198.51.100.10",
        interval_seconds=0,
        max_cycles=1,
        timeout_ms=750,
        ping_probe_factory=lambda timeout_ms: _RecordingPingRunner(timeout_ms, ping_calls),
        traceroute_probe=traceroute_probe,
    )
    traces: list[list[HopInfo]] = []
    updates: list[tuple[object, object, object, object, object, object]] = []
    worker.trace_completed.connect(traces.append)
    worker.measurement_updated.connect(lambda *args: updates.append(args))

    worker.run()

    assert traceroute_probe.calls == [("198.51.100.10", 750, False)]
    assert [hop.index for hop in traces[0]] == [1, 2]
    assert {target for target, _timeout in ping_calls} == {
        "192.0.2.1",
        "198.51.100.10",
    }
    assert {timeout for _target, timeout in ping_calls} == {750}
    assert updates


def test_worker_uses_tcp_connect_probe_engine(monkeypatch) -> None:
    _app()
    calls: list[tuple[object, ...]] = []

    class RecordingTcpConnectRunner:
        def __init__(self, timeout_ms: int, port: int) -> None:
            calls.append(("init", timeout_ms, port))

        def ping(self, target: str) -> PingResult:
            calls.append(("ping", target))
            return PingResult(target, True, 14.0, STATUS_OK, datetime.now())

        def close(self) -> None:
            calls.append(("close",))

    monkeypatch.setattr(worker_module, "TcpConnectRunner", RecordingTcpConnectRunner)

    worker = MeasurementWorker(
        "198.51.100.10",
        interval_seconds=0,
        max_cycles=1,
        timeout_ms=650,
        measurement_mode=MEASUREMENT_MODE_FINAL_HOP_ONLY,
        probe_engine=PROBE_ENGINE_TCP_CONNECT,
        tcp_port=8443,
    )
    updates: list[tuple[object, object, object, object, object, object]] = []
    diagnostics: list[object] = []
    worker.measurement_updated.connect(lambda *args: updates.append(args))
    worker.diagnostics_updated.connect(diagnostics.append)

    worker.run()

    assert ("init", 650, 8443) in calls
    assert ("ping", "198.51.100.10") in calls
    assert ("close",) in calls
    assert updates[0][1].status == STATUS_OK
    assert diagnostics
    assert getattr(diagnostics[-1], "target_probe_engine") == "TCP Connect:8443"
    assert getattr(diagnostics[-1], "route_probe_engine") == "disabled"
    assert getattr(diagnostics[-1], "tcp_port") == 8443


def test_worker_uses_icmp_for_full_route_hops_with_tcp_target_engine(monkeypatch) -> None:
    _app()
    calls: list[tuple[object, ...]] = []

    class RecordingTcpConnectRunner:
        def __init__(self, timeout_ms: int, port: int) -> None:
            calls.append(("tcp_init", timeout_ms, port))

        def ping(self, target: str) -> PingResult:
            calls.append(("tcp_ping", target))
            return PingResult(target, True, 14.0, STATUS_OK, datetime.now())

        def close(self) -> None:
            calls.append(("tcp_close",))

    class RecordingCommandPingRunner:
        def __init__(self, timeout_ms: int) -> None:
            calls.append(("icmp_init", timeout_ms))

        def ping(self, target: str) -> PingResult:
            calls.append(("icmp_ping", target))
            return PingResult(target, True, 2.0, STATUS_OK, datetime.now())

        def close(self) -> None:
            calls.append(("icmp_close",))

    monkeypatch.setattr(worker_module, "TcpConnectRunner", RecordingTcpConnectRunner)
    monkeypatch.setattr(worker_module, "CommandPingRunner", RecordingCommandPingRunner)
    traceroute_probe = _FakeTracerouteProbe(
        [
            HopInfo(index=1, address="192.0.2.1", hostname="gateway"),
            HopInfo(index=2, address="198.51.100.10", hostname="target.example"),
        ]
    )
    worker = MeasurementWorker(
        "198.51.100.10",
        interval_seconds=0,
        max_cycles=1,
        timeout_ms=650,
        traceroute_probe=traceroute_probe,
        measurement_mode=MEASUREMENT_MODE_FULL_ROUTE,
        probe_engine=PROBE_ENGINE_TCP_CONNECT,
        tcp_port=8443,
    )
    diagnostics: list[object] = []
    worker.diagnostics_updated.connect(diagnostics.append)

    worker.run()

    assert ("tcp_ping", "198.51.100.10") in calls
    assert ("icmp_ping", "192.0.2.1") in calls
    assert ("tcp_ping", "192.0.2.1") not in calls
    assert ("icmp_ping", "198.51.100.10") not in calls
    assert diagnostics
    assert getattr(diagnostics[-1], "target_probe_engine") == "TCP Connect:8443"
    assert getattr(diagnostics[-1], "route_probe_engine") == "tracert/ICMP"


class _FakePingRunner:
    def __init__(self, timeout_ms: int) -> None:
        self.timeout_ms = timeout_ms

    def ping(self, target: str) -> PingResult:
        latency = {
            "192.0.2.1": 2.0,
            "198.51.100.10": 20.0,
            "203.0.113.10": 30.0,
        }.get(target, 10.0)
        return PingResult(target, True, latency, STATUS_OK, datetime.now())


class _FailingSessionLogWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.paths = [path]

    def write_many(self, _observations) -> None:
        raise RuntimeError("simulated write failure")

    def close(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)


class _SlowTargetPingRunner:
    def __init__(self, timeout_ms: int, calls: dict[str, int]) -> None:
        self.timeout_ms = timeout_ms
        self.calls = calls

    def ping(self, target: str) -> PingResult:
        self.calls[target] = self.calls.get(target, 0) + 1
        if target == "203.0.113.10":
            time.sleep(1.5)
            return PingResult(target, False, None, STATUS_TIMEOUT, datetime.now())
        return PingResult(target, True, 20.0, STATUS_OK, datetime.now())


class _CountingPingRunner:
    def __init__(self, timeout_ms: int, counters: dict[str, int]) -> None:
        self.timeout_ms = timeout_ms
        self.counters = counters
        self.counters["instances"] += 1

    def ping(self, target: str) -> PingResult:
        self.counters["pings"] += 1
        return PingResult(target, True, 20.0, STATUS_OK, datetime.now())

    def close(self) -> None:
        self.counters["closed"] += 1


class _TwentyTargetPingRunner:
    def __init__(self, timeout_ms: int, calls: dict[str, int]) -> None:
        self.timeout_ms = timeout_ms
        self.calls = calls

    def ping(self, target: str) -> PingResult:
        self.calls[target] = self.calls.get(target, 0) + 1
        if int(target.rsplit(".", 1)[1]) > 5:
            time.sleep(1.5)
            return PingResult(target, False, None, STATUS_TIMEOUT, datetime.now())
        return PingResult(target, True, 10.0, STATUS_OK, datetime.now())


class _BackoffPingRunner:
    def __init__(self, timeout_ms: int, calls: dict[str, int]) -> None:
        self.timeout_ms = timeout_ms
        self.calls = calls

    def ping(self, target: str) -> PingResult:
        self.calls[target] = self.calls.get(target, 0) + 1
        if target == "198.51.100.2":
            return PingResult(target, False, None, STATUS_TIMEOUT, datetime.now())
        return PingResult(target, True, 10.0, STATUS_OK, datetime.now())


class _FailingPingRunner:
    def __init__(self, timeout_ms: int) -> None:
        self.timeout_ms = timeout_ms

    def ping(self, target: str) -> PingResult:
        raise OSError("simulated ping failure")


class _FakeTracerouteProbe:
    def __init__(self, hops: list[HopInfo]) -> None:
        self.hops = hops
        self.calls: list[tuple[str, int, bool]] = []

    def trace(self, target: str, timeout_ms: int, stop_event) -> list[HopInfo]:
        self.calls.append((target, timeout_ms, stop_event.is_set()))
        return self.hops


class _RecordingPingRunner:
    def __init__(self, timeout_ms: int, calls: list[tuple[str, int]]) -> None:
        self.timeout_ms = timeout_ms
        self.calls = calls

    def ping(self, target: str) -> PingResult:
        self.calls.append((target, self.timeout_ms))
        latency = 20.0 if target == "198.51.100.10" else 2.0
        return PingResult(target, True, latency, STATUS_OK, datetime.now())


class _HighLatencyPingRunner:
    def __init__(self, timeout_ms: int, calls: list[str]) -> None:
        self.timeout_ms = timeout_ms
        self.calls = calls

    def ping(self, target: str) -> PingResult:
        self.calls.append(target)
        latency = 125.0 if target == "198.51.100.10" else 2.0
        return PingResult(target, True, latency, STATUS_OK, datetime.now())


class _RecoveringLatencyPingRunner:
    def __init__(self, timeout_ms: int, calls: dict[str, int]) -> None:
        self.timeout_ms = timeout_ms
        self.calls = calls

    def ping(self, target: str) -> PingResult:
        count = self.calls.get(target, 0) + 1
        self.calls[target] = count
        latency = 125.0 if target == "198.51.100.10" and count == 1 else 20.0
        return PingResult(target, True, latency, STATUS_OK, datetime.now())


class _PatternPingRunner:
    def __init__(self, timeout_ms: int, calls: dict[str, int]) -> None:
        self.timeout_ms = timeout_ms
        self.calls = calls

    def ping(self, target: str) -> PingResult:
        count = self.calls.get(target, 0) + 1
        self.calls[target] = count
        if count % 15 == 0 and target == "198.51.100.10":
            return PingResult(target, False, None, STATUS_TIMEOUT, datetime.now())
        latency = 20.0 + (count % 7) if target == "198.51.100.10" else 2.0
        return PingResult(target, True, latency, STATUS_OK, datetime.now())


class _BlockingTracerouteProbe:
    def trace(self, target: str, timeout_ms: int, stop_event) -> list[HopInfo]:
        while not stop_event.is_set():
            time.sleep(0.01)
        return []


class _DelayedTracerouteProbe:
    def __init__(self, hops: list[HopInfo], *, delay_seconds: float = 0.15) -> None:
        self.hops = hops
        self.delay_seconds = delay_seconds

    def trace(self, target: str, timeout_ms: int, stop_event) -> list[HopInfo]:
        time.sleep(self.delay_seconds)
        return self.hops


def _count_csv_rows(path: Path) -> int:
    return max(len(path.read_text(encoding="utf-8").splitlines()) - 1, 0)
