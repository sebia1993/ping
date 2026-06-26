from __future__ import annotations

from argparse import Namespace

import pytest

from scripts.soak_test import EventLoopStats, evaluate_summary, parse_args


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
