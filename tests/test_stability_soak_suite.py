from __future__ import annotations

import json
from pathlib import Path

from scripts import run_stability_soak_suite as suite


def test_soak_suite_defaults_cover_long_and_ui_profiles() -> None:
    args = suite.parse_args([])

    assert args.profiles == ["long4h", "long8h", "long24h", "ui10", "ui20", "ui50"]


def test_soak_suite_builds_profile_command_without_shortening_real_duration(tmp_path) -> None:
    command = suite.build_profile_command(
        "long4h",
        output_dir=tmp_path / "long4h",
        python_executable="python",
    )

    assert command == [
        "python",
        "scripts\\soak_test.py",
        "--profile",
        "long4h",
        "--output-dir",
        str(tmp_path / "long4h"),
    ]
    assert "--duration-seconds" not in command


def test_soak_suite_duration_override_is_explicit_for_smoke_only(tmp_path) -> None:
    command = suite.build_profile_command(
        "ui10",
        output_dir=tmp_path / "ui10",
        python_executable="python",
        override_duration_seconds=3.0,
    )

    assert command[-2:] == ["--duration-seconds", "3.0"]


def test_soak_suite_dry_run_writes_manifest_without_running_profiles(tmp_path) -> None:
    exit_code = suite.main([
        "--dry-run",
        "--run-id",
        "dry-run",
        "--output-dir",
        str(tmp_path),
        "--profiles",
        "long4h",
        "ui10",
    ])

    manifest_path = tmp_path / "dry-run" / "stability_soak_suite.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert payload["planned_only"] is True
    assert payload["passed"] is False
    assert payload["profiles_requested"] == ["long4h", "ui10"]
    assert [result["status"] for result in payload["results"]] == ["planned", "planned"]
    assert all(Path(result["output_dir"]).name in {"long4h", "ui10"} for result in payload["results"])


def test_soak_suite_validate_only_rejects_dry_run_manifest(tmp_path) -> None:
    suite.main([
        "--dry-run",
        "--run-id",
        "dry-run",
        "--output-dir",
        str(tmp_path),
        "--profiles",
        "long4h",
    ])

    exit_code = suite.main([
        "--validate-only",
        "--run-id",
        "dry-run",
        "--output-dir",
        str(tmp_path),
        "--profiles",
        "long4h",
    ])

    assert exit_code == 1


def test_soak_suite_validate_accepts_completed_manifest(tmp_path) -> None:
    run_root = tmp_path / "completed"
    run_root.mkdir()
    summary_path = run_root / "soak_50_targets_20260101_010101.json"
    summary_path.write_text(
        json.dumps(_summary("release", duration_seconds=5.0), ensure_ascii=False),
        encoding="utf-8",
    )
    manifest_path = run_root / "stability_soak_suite.json"
    suite.write_manifest(
        manifest_path,
        started_at="2026-01-01T01:00:00",
        profiles=["release"],
        results=[
            {
                "profile": "release",
                "status": "passed",
                "summary_json": str(summary_path),
                "failures": [],
            }
        ],
        finished_at="2026-01-01T01:00:05",
    )

    assert suite.validate_manifest(manifest_path, ["release"]) == []


def test_soak_suite_validate_rejects_short_long_run(tmp_path) -> None:
    summary = _summary("long4h", duration_seconds=10.0)

    failures = suite.validate_summary("long4h", summary)

    assert any("duration too short" in failure for failure in failures)


def test_soak_suite_validate_rejects_session_log_row_loss() -> None:
    summary = _summary("release", duration_seconds=5.0, ping_results=200, session_log_rows=10)

    failures = suite.validate_summary("release", summary)

    assert any("session log rows too low" in failure for failure in failures)


def test_soak_suite_validate_rejects_missing_stability_fields() -> None:
    failures = suite.validate_summary("release", {"profile": "release", "duration_seconds": 5.0})

    assert any("summary is missing or has invalid stability fields" in failure for failure in failures)


def test_soak_suite_resume_reuses_existing_passed_profile(tmp_path, monkeypatch) -> None:
    run_root = tmp_path / "resume-run"
    run_root.mkdir()
    summary_path = run_root / "soak_50_targets_20260101_010101.json"
    summary_path.write_text(
        json.dumps(_summary("release", duration_seconds=5.0), ensure_ascii=False),
        encoding="utf-8",
    )
    suite.write_manifest(
        run_root / "stability_soak_suite.json",
        started_at="2026-01-01T01:00:00",
        profiles=["release"],
        results=[
            {
                "profile": "release",
                "status": "passed",
                "summary_json": str(summary_path),
                "failures": [],
            }
        ],
        finished_at="2026-01-01T01:00:05",
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("passed profile should be reused")

    monkeypatch.setattr(suite, "run_profile", fail_if_called)

    exit_code = suite.main([
        "--resume",
        "--run-id",
        "resume-run",
        "--output-dir",
        str(tmp_path),
        "--profiles",
        "release",
    ])

    assert exit_code == 0


def test_soak_suite_loads_latest_summary(tmp_path) -> None:
    old = tmp_path / "soak_50_targets_20260101_010101.json"
    new = tmp_path / "soak_50_targets_20260101_020202.json"
    old.write_text('{"failures": ["old"]}', encoding="utf-8")
    new.write_text('{"failures": []}', encoding="utf-8")

    summary = suite.load_latest_summary(tmp_path)

    assert summary is not None
    assert summary["path"] == new
    assert summary["data"]["failures"] == []


def _summary(
    profile: str,
    *,
    duration_seconds: float,
    ping_results: int = 10,
    session_log_rows: int | None = None,
) -> dict[str, object]:
    profile_defaults = suite.SOAK_PROFILES[profile]
    interval_seconds = int(profile_defaults["interval_seconds"])
    expected_updates = max(int(float(profile_defaults["duration_seconds"]) // max(interval_seconds, 1)), 1)
    rows = ping_results if session_log_rows is None else session_log_rows
    return {
        "profile": profile,
        "targets": profile_defaults["targets"],
        "timeout_ratio": profile_defaults["timeout_ratio"],
        "duration_seconds": duration_seconds,
        "with_ui": profile_defaults["with_ui"],
        "updates": expected_updates,
        "errors": [],
        "failures": [],
        "stopped_cleanly": True,
        "session_log_rows": rows,
        "session_log_segments": 1,
        "ping_calls": ping_results,
        "ping_results": ping_results,
        "traceroute_calls": max(int(float(profile_defaults["duration_seconds"]) // 30.0), 1),
        "active_threads_final": 1,
        "max_active_threads": min(int(profile_defaults["max_active_threads"]), 24),
        "cpu_percent": 1.0,
        "memory_growth_bytes": 1024,
        "max_update_gap_seconds": 1.0,
        "avg_update_gap_seconds": 1.0,
        "max_ui_event_gap_seconds": 0.02,
        "diagnostic_samples": expected_updates,
        "max_pending_ping_count": 0,
        "max_log_queue_depth": 1,
        "max_backoff_target_count": 1,
    }
