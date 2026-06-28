from __future__ import annotations

import threading
from argparse import Namespace

import pytest

from app.storage import atomic_write as atomic_write_module
from scripts.soak_test import (
    EventLoopStats,
    SimulatedPingRunner,
    build_summary,
    evaluate_summary,
    parse_args,
    write_diagnostics_csv,
    write_health_csv,
    write_summary_json,
)


def test_soak_release_profile_sets_fast_fifty_target_defaults() -> None:
    args = parse_args(["--profile", "release"])

    assert args.profile == "release"
    assert args.duration_seconds == 5.0
    assert args.targets == 50
    assert args.timeout_ratio == 0.8
    assert args.timeout_delay_seconds == 0.05
    assert args.event_poll_seconds == 0.02
    assert args.sample_seconds == 0.5
    assert args.progress_seconds == 0.0
    assert args.max_cpu_percent == 250.0
    assert args.with_ui is False
    assert args.event_process_max_milliseconds == 0
    assert args.max_ui_event_process_seconds == 2.0


def test_soak_long_profile_sets_thirty_minute_fifty_target_defaults() -> None:
    args = parse_args(["--profile", "long"])

    assert args.profile == "long"
    assert args.duration_seconds == 1800.0
    assert args.targets == 50
    assert args.timeout_ratio == 0.8
    assert args.timeout_delay_seconds == 1.5
    assert args.max_cpu_percent == 80.0
    assert args.with_ui is False


def test_soak_long_duration_profiles_define_four_eight_and_twenty_four_hour_runs() -> None:
    four = parse_args(["--profile", "long4h"])
    eight = parse_args(["--profile", "long8h"])
    twenty_four = parse_args(["--profile", "long24h"])

    assert four.duration_seconds == 14_400.0
    assert eight.duration_seconds == 28_800.0
    assert twenty_four.duration_seconds == 86_400.0
    assert {four.targets, eight.targets, twenty_four.targets} == {50}
    assert twenty_four.max_memory_growth_mb == 256.0


def test_soak_ui_profile_drives_offscreen_window_by_default() -> None:
    args = parse_args(["--profile", "ui"])

    assert args.profile == "ui"
    assert args.duration_seconds == 60.0
    assert args.targets == 50
    assert args.with_ui is True


def test_soak_ui_freeze_profiles_measure_ten_twenty_and_fifty_targets() -> None:
    ten = parse_args(["--profile", "ui10"])
    twenty = parse_args(["--profile", "ui20"])
    fifty = parse_args(["--profile", "ui50"])

    assert [ten.targets, twenty.targets, fifty.targets] == [10, 20, 50]
    assert ten.with_ui is True
    assert twenty.with_ui is True
    assert fifty.with_ui is True
    assert ten.duration_seconds == 600.0
    assert twenty.duration_seconds == 600.0
    assert fifty.duration_seconds == 600.0
    assert ten.event_poll_seconds == 0.01
    assert twenty.event_poll_seconds == 0.01
    assert fifty.event_poll_seconds == 0.01
    assert ten.max_ui_event_gap_seconds == 0.2
    assert twenty.max_ui_event_gap_seconds == 0.2
    assert fifty.max_ui_event_gap_seconds == 0.2
    assert ten.max_ui_event_process_seconds == 0.2
    assert twenty.max_ui_event_process_seconds == 0.2
    assert fifty.max_ui_event_process_seconds == 0.2
    assert ten.event_process_max_milliseconds == 10
    assert twenty.event_process_max_milliseconds == 10
    assert fifty.event_process_max_milliseconds == 10


def test_soak_profile_allows_explicit_cli_overrides() -> None:
    args = parse_args([
        "--profile",
        "release",
        "--duration-seconds",
        "12",
        "--targets",
        "7",
        "--no-ui",
        "--max-cpu-percent",
        "90",
    ])

    assert args.profile == "release"
    assert args.duration_seconds == 12.0
    assert args.targets == 7
    assert args.with_ui is False
    assert args.max_cpu_percent == 90.0


def test_event_loop_stats_keep_top_gap_and_process_samples() -> None:
    stats = EventLoopStats()
    diagnostics = {
        "active_ping_count": 2,
        "pending_ping_count": 1,
        "log_queue_depth": 3,
    }

    stats.record(
        10.0,
        elapsed_seconds=0.0,
        event_process_seconds=0.01,
        updates=0,
        diagnostic_samples=0,
        last_diagnostics=diagnostics,
    )
    stats.record(
        10.05,
        elapsed_seconds=0.05,
        event_process_seconds=0.02,
        updates=1,
        diagnostic_samples=1,
        last_diagnostics=diagnostics,
    )
    stats.record(
        10.35,
        elapsed_seconds=0.35,
        event_process_seconds=0.18,
        updates=2,
        diagnostic_samples=2,
        last_diagnostics=diagnostics,
    )

    assert stats.tick_count == 3
    assert stats.max_gap_seconds == pytest.approx(0.3)
    assert stats.avg_gap_seconds == pytest.approx(0.175)
    assert stats.max_process_seconds == pytest.approx(0.18)
    assert stats.avg_process_seconds == pytest.approx(0.07)
    assert stats.top_gap_samples[0]["event_gap_seconds"] == pytest.approx(0.3)
    assert stats.top_gap_samples[0]["pending_ping_count"] == 1
    assert stats.top_process_samples[0]["event_process_seconds"] == pytest.approx(0.18)


def test_soak_evaluation_accepts_stable_thirty_minute_run() -> None:
    args = _args()
    summary = _summary(
        updates=1778,
        diagnostic_samples=1778,
        max_update_gap_seconds=2.031,
        avg_update_gap_seconds=1.013,
    )

    assert evaluate_summary(summary, args) == []


def test_soak_evaluation_rejects_slow_average_ui_updates() -> None:
    args = _args()
    summary = _summary(
        updates=1778,
        diagnostic_samples=1778,
        max_update_gap_seconds=2.031,
        avg_update_gap_seconds=1.6,
    )

    failures = evaluate_summary(summary, args)

    assert any("average update gap too high" in failure for failure in failures)


def test_soak_evaluation_rejects_slow_ui_event_processing() -> None:
    args = _args()
    summary = _summary(
        updates=1778,
        diagnostic_samples=1778,
        max_update_gap_seconds=2.031,
        avg_update_gap_seconds=1.013,
        max_ui_event_process_seconds=0.35,
    )

    failures = evaluate_summary(summary, args)

    assert any("UI event processing too slow" in failure for failure in failures)


def test_soak_evaluation_rejects_missing_session_persistence() -> None:
    args = _args()
    summary = _summary(
        updates=1778,
        diagnostic_samples=1778,
        max_update_gap_seconds=2.031,
        avg_update_gap_seconds=1.013,
        ping_calls=89_000,
        ping_results=89_000,
        session_log_rows=0,
        session_log_segments=0,
    )

    failures = evaluate_summary(summary, args)

    assert "session log was not created" in failures
    assert any("session log rows too low" in failure for failure in failures)


def test_soak_evaluation_allows_in_flight_pings_at_shutdown() -> None:
    args = _args()
    summary = _summary(
        updates=1778,
        diagnostic_samples=1778,
        max_update_gap_seconds=2.031,
        avg_update_gap_seconds=1.013,
        ping_calls=89_010,
        ping_results=89_000,
        session_log_rows=89_000,
        session_log_segments=1,
    )

    assert evaluate_summary(summary, args) == []


def test_soak_summary_reports_session_log_row_delta(tmp_path) -> None:
    args = parse_args([
        "--profile",
        "release",
        "--output-dir",
        str(tmp_path),
        "--session-log-root",
        str(tmp_path / "session_logs"),
    ])

    summary = build_summary(
        args=args,
        elapsed=5.0,
        updates=[],
        diagnostics_rows=[],
        health_rows=[{"current_memory_bytes": 1000, "active_threads": 1}],
        event_loop_stats=EventLoopStats(),
        errors=[],
        session_log_paths=[],
        ping_calls={"198.51.100.1": 5},
        ping_results={"198.51.100.1": 4},
        traceroute_calls=1,
        cpu_seconds=0.1,
        current_memory_bytes=1200,
        peak_memory_bytes=1500,
        diagnostics_csv_path=tmp_path / "diagnostics.csv",
        health_csv_path=tmp_path / "health.csv",
        stopped_cleanly=True,
    )

    assert summary["session_log_min_expected_rows"] == 4
    assert summary["session_log_row_delta"] == -4


def test_simulated_ping_runner_updates_counters_under_lock() -> None:
    lock = threading.Lock()
    calls = _LockCheckedCounter(lock)
    results = _LockCheckedCounter(lock)
    runner = SimulatedPingRunner(
        timeout_ms=1000,
        timeout_start_index=1,
        timeout_delay_seconds=0,
        calls=calls,
        results=results,
        counter_lock=lock,
    )

    runner.ping("198.51.100.1")

    assert calls["198.51.100.1"] == 1
    assert results["198.51.100.1"] == 1


def test_soak_csv_writes_retry_transient_replace_error(tmp_path, monkeypatch) -> None:
    path = tmp_path / "diagnostics.csv"
    attempts = 0
    original_replace = atomic_write_module._replace_path

    def flaky_replace(source, target):
        nonlocal attempts
        attempts += 1
        if target == path and attempts < 3:
            raise OSError("sharing violation")
        return original_replace(source, target)

    monkeypatch.setattr(atomic_write_module, "EXPORT_IO_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(atomic_write_module, "_replace_path", flaky_replace)

    write_diagnostics_csv(path, [{"elapsed_seconds": 1.0, "timestamp": "2026-01-01T00:00:00"}])

    assert attempts == 3
    assert "elapsed_seconds" in path.read_text(encoding="utf-8")
    assert list(tmp_path.glob(".diagnostics.csv.*")) == []


def test_soak_summary_json_preserves_existing_file_after_replace_failure(tmp_path, monkeypatch) -> None:
    path = tmp_path / "soak_50_targets_20260101_010101.json"
    path.write_text('{"status":"existing"}', encoding="utf-8")
    original_replace = atomic_write_module._replace_path

    def locked_replace(source, target):
        if target == path:
            raise PermissionError("locked")
        return original_replace(source, target)

    monkeypatch.setattr(atomic_write_module, "EXPORT_IO_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(atomic_write_module, "_replace_path", locked_replace)

    with pytest.raises(PermissionError):
        write_summary_json(path, {"status": "new"})

    assert path.read_text(encoding="utf-8") == '{"status":"existing"}'
    assert list(tmp_path.glob(".soak_50_targets_20260101_010101.json.*")) == []


def test_soak_health_csv_writes_atomically(tmp_path) -> None:
    path = tmp_path / "health.csv"

    write_health_csv(path, [{"elapsed_seconds": 1.0, "current_memory_bytes": 1024}])

    assert "current_memory_bytes" in path.read_text(encoding="utf-8")


def test_soak_evaluation_rejects_resource_pressure() -> None:
    args = _args()
    summary = _summary(
        updates=1778,
        diagnostic_samples=1778,
        max_update_gap_seconds=2.031,
        avg_update_gap_seconds=1.013,
        max_pending_ping_count=99,
        max_log_queue_depth=999,
        max_active_threads=99,
    )

    failures = evaluate_summary(summary, args)

    assert any("pending ping count too high" in failure for failure in failures)
    assert any("log queue depth too high" in failure for failure in failures)
    assert any("active thread count too high" in failure for failure in failures)


def test_soak_evaluation_rejects_memory_and_cpu_growth() -> None:
    args = _args()
    summary = _summary(
        updates=1778,
        diagnostic_samples=1778,
        max_update_gap_seconds=2.031,
        avg_update_gap_seconds=1.013,
        memory_growth_bytes=200 * 1024 * 1024,
        cpu_percent=95.0,
    )

    failures = evaluate_summary(summary, args)

    assert any("memory growth too high" in failure for failure in failures)
    assert any("CPU usage too high" in failure for failure in failures)


def _args() -> Namespace:
    return Namespace(
        duration_seconds=1800.0,
        interval_seconds=1,
        timeout_delay_seconds=1.5,
        timeout_ratio=0.8,
        targets=50,
        max_update_gap_seconds=None,
        max_average_update_gap_seconds=None,
        max_ui_event_gap_seconds=2.0,
        max_ui_event_process_seconds=0.2,
        max_pending_pings=None,
        max_log_queue_depth=None,
        max_active_threads=40,
        max_memory_growth_mb=96.0,
        max_cpu_percent=80.0,
        no_require_backoff=False,
        session_log_root=None,
    )


def _summary(
    *,
    updates: int,
    diagnostic_samples: int,
    max_update_gap_seconds: float,
    avg_update_gap_seconds: float,
    ping_calls: int = 89_000,
    ping_results: int | None = None,
    session_log_rows: int = 89_000,
    session_log_segments: int = 1,
    max_pending_ping_count: int = 18,
    max_log_queue_depth: int = 8,
    max_active_threads: int = 24,
    memory_growth_bytes: int = 5_100_000,
    cpu_percent: float = 2.7,
    max_ui_event_process_seconds: float = 0.075,
) -> dict[str, object]:
    completed = ping_calls if ping_results is None else ping_results
    return {
        "errors": [],
        "stopped_cleanly": True,
        "updates": updates,
        "diagnostic_samples": diagnostic_samples,
        "max_update_gap_seconds": max_update_gap_seconds,
        "avg_update_gap_seconds": avg_update_gap_seconds,
        "max_ui_event_gap_seconds": 0.075,
        "max_ui_event_process_seconds": max_ui_event_process_seconds,
        "max_pending_ping_count": max_pending_ping_count,
        "max_log_queue_depth": max_log_queue_depth,
        "max_active_threads": max_active_threads,
        "memory_growth_bytes": memory_growth_bytes,
        "cpu_percent": cpu_percent,
        "max_backoff_target_count": 41,
        "traceroute_calls": 30,
        "ping_calls": ping_calls,
        "ping_results": completed,
        "session_log_rows": session_log_rows,
        "session_log_segments": session_log_segments,
    }


class _LockCheckedCounter(dict):
    def __init__(self, lock: threading.Lock) -> None:
        super().__init__()
        self._lock = lock

    def get(self, key, default=None):
        assert self._lock.locked()
        return super().get(key, default)

    def __setitem__(self, key, value) -> None:
        assert self._lock.locked()
        super().__setitem__(key, value)
