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


def test_soak_suite_loads_latest_summary(tmp_path) -> None:
    old = tmp_path / "soak_50_targets_20260101_010101.json"
    new = tmp_path / "soak_50_targets_20260101_020202.json"
    old.write_text('{"failures": ["old"]}', encoding="utf-8")
    new.write_text('{"failures": []}', encoding="utf-8")

    summary = suite.load_latest_summary(tmp_path)

    assert summary is not None
    assert summary["path"] == new
    assert summary["data"]["failures"] == []
