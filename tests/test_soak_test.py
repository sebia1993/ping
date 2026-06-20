from __future__ import annotations

from argparse import Namespace

from scripts.soak_test import evaluate_summary


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
    )


def _summary(
    *,
    updates: int,
    diagnostic_samples: int,
    max_update_gap_seconds: float,
    avg_update_gap_seconds: float,
) -> dict[str, object]:
    return {
        "errors": [],
        "stopped_cleanly": True,
        "updates": updates,
        "diagnostic_samples": diagnostic_samples,
        "max_update_gap_seconds": max_update_gap_seconds,
        "avg_update_gap_seconds": avg_update_gap_seconds,
        "max_ui_event_gap_seconds": 0.075,
        "max_pending_ping_count": 18,
        "max_log_queue_depth": 8,
        "max_active_threads": 24,
        "memory_growth_bytes": 5_100_000,
        "cpu_percent": 2.7,
        "max_backoff_target_count": 41,
        "traceroute_calls": 30,
    }
