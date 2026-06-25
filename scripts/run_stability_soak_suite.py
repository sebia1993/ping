from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).absolute().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.soak_test import SOAK_PROFILES


DEFAULT_PROFILES = ("long4h", "long8h", "long24h", "ui10", "ui20", "ui50")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_root = args.output_dir / args.run_id
    run_root.mkdir(parents=True, exist_ok=True)
    manifest_path = run_root / "stability_soak_suite.json"
    previous_payload = load_manifest(manifest_path) if args.resume or args.validate_only else None

    if args.validate_only:
        failures = validate_manifest(manifest_path, args.profiles)
        if failures:
            print(json.dumps({"manifest": str(manifest_path), "validation_failures": failures}, ensure_ascii=False, indent=2))
            return 1
        print(manifest_path)
        return 0

    started_at = (
        str(previous_payload.get("suite_started_at"))
        if args.resume and previous_payload and previous_payload.get("suite_started_at")
        else datetime.now().isoformat(timespec="seconds")
    )
    results: list[dict[str, Any]] = list(previous_payload.get("results", [])) if args.resume and previous_payload else []

    for profile in args.profiles:
        if args.resume and latest_passed_result(profile, results, manifest_path=manifest_path) is not None:
            continue
        profile_result = run_profile(profile, args=args, run_root=run_root)
        results.append(profile_result)
        write_manifest(
            manifest_path,
            started_at=started_at,
            profiles=args.profiles,
            results=results,
            finished_at=None,
        )
        if profile_result["status"] == "failed" and not args.continue_on_failure:
            break

    finished_at = datetime.now().isoformat(timespec="seconds")
    write_manifest(
        manifest_path,
        started_at=started_at,
        profiles=args.profiles,
        results=results,
        finished_at=finished_at,
    )
    print(manifest_path)
    if args.dry_run:
        return 0
    validation_failures = validate_manifest(manifest_path, args.profiles)
    if validation_failures:
        print(json.dumps({"manifest": str(manifest_path), "validation_failures": validation_failures}, ensure_ascii=False, indent=2))
        return 1
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run long-duration simulated soak profiles and write one suite manifest."
    )
    parser.add_argument(
        "--profiles",
        nargs="+",
        choices=sorted(SOAK_PROFILES),
        default=list(DEFAULT_PROFILES),
        help="Profiles to run in order. Defaults to 4h/8h/24h plus UI 10/20/50 checks.",
    )
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts" / "stability_soak_suite")
    parser.add_argument("--run-id", default=datetime.now().strftime("%Y%m%d_%H%M%S"))
    parser.add_argument("--python", dest="python_executable", default=sys.executable)
    parser.add_argument("--continue-on-failure", action="store_true")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Reuse already passed profile results from the same run-id manifest and continue missing profiles.",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Do not run profiles. Validate the existing run-id manifest and referenced summary JSON files.",
    )
    parser.add_argument(
        "--override-duration-seconds",
        type=float,
        help="Use only for local smoke checks. Omit it for real 4h/8h/24h evidence.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Write the suite plan without starting any long-running profile.",
    )
    return parser.parse_args(argv)


def run_profile(profile: str, *, args: argparse.Namespace, run_root: Path) -> dict[str, Any]:
    profile_output_dir = run_root / profile
    profile_output_dir.mkdir(parents=True, exist_ok=True)
    command = build_profile_command(
        profile,
        output_dir=profile_output_dir,
        python_executable=args.python_executable,
        override_duration_seconds=args.override_duration_seconds,
    )
    started_at = datetime.now().isoformat(timespec="seconds")
    result: dict[str, Any] = {
        "profile": profile,
        "started_at": started_at,
        "finished_at": None,
        "command": command,
        "output_dir": str(profile_output_dir),
        "summary_json": None,
        "returncode": None,
        "status": "planned" if args.dry_run else "running",
        "failures": [],
    }
    if args.dry_run:
        result["finished_at"] = datetime.now().isoformat(timespec="seconds")
        return result

    completed = subprocess.run(command, cwd=ROOT)
    result["returncode"] = completed.returncode
    result["finished_at"] = datetime.now().isoformat(timespec="seconds")
    summary = load_latest_summary(profile_output_dir)
    if summary is not None:
        result["summary_json"] = str(summary["path"])
        result["failures"] = summary["data"].get("failures", [])
        result["duration_seconds"] = summary["data"].get("duration_seconds")
        result["max_ui_event_gap_seconds"] = summary["data"].get("max_ui_event_gap_seconds")
        result["memory_growth_bytes"] = summary["data"].get("memory_growth_bytes")
        result["active_threads_final"] = summary["data"].get("active_threads_final")
        result["session_log_rows"] = summary["data"].get("session_log_rows")
    if completed.returncode == 0 and not result["failures"]:
        result["status"] = "passed"
    else:
        result["status"] = "failed"
        if summary is None:
            result["failures"] = ["summary JSON was not created"]
    return result


def build_profile_command(
    profile: str,
    *,
    output_dir: Path,
    python_executable: str,
    override_duration_seconds: float | None = None,
) -> list[str]:
    command = [
        python_executable,
        "scripts\\soak_test.py",
        "--profile",
        profile,
        "--output-dir",
        str(output_dir),
    ]
    if override_duration_seconds is not None:
        command.extend(["--duration-seconds", str(override_duration_seconds)])
    return command


def load_latest_summary(output_dir: Path) -> dict[str, Any] | None:
    candidates = sorted(
        output_dir.glob("soak_*_targets_*.json"),
        key=lambda path: (path.stat().st_mtime, path.name),
    )
    if not candidates:
        return None
    path = candidates[-1]
    return {"path": path, "data": json.loads(path.read_text(encoding="utf-8"))}


def load_manifest(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def latest_result(profile: str, results: list[dict[str, Any]]) -> dict[str, Any] | None:
    matches = [result for result in results if result.get("profile") == profile]
    return matches[-1] if matches else None


def latest_passed_result(
    profile: str,
    results: list[dict[str, Any]],
    *,
    manifest_path: Path,
) -> dict[str, Any] | None:
    result = latest_result(profile, results)
    if result is None or result.get("status") != "passed":
        return None
    summary_path = resolve_recorded_path(result.get("summary_json"), manifest_path=manifest_path)
    if summary_path is None or not summary_path.exists():
        return None
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if validate_summary(profile, summary):
        return None
    return result


def validate_manifest(path: Path, profiles: list[str]) -> list[str]:
    payload = load_manifest(path)
    if payload is None:
        return [f"manifest was not found: {path}"]
    return validate_manifest_payload(payload, profiles, manifest_path=path)


def validate_manifest_payload(
    payload: dict[str, Any],
    profiles: list[str],
    *,
    manifest_path: Path,
) -> list[str]:
    failures: list[str] = []
    results = list(payload.get("results", []))
    if payload.get("planned_only"):
        failures.append("manifest is dry-run only")
    if payload.get("profiles_requested") != profiles:
        failures.append(f"profiles mismatch: {payload.get('profiles_requested')} != {profiles}")
    for profile in profiles:
        result = latest_result(profile, results)
        if result is None:
            failures.append(f"{profile}: result missing")
            continue
        if result.get("status") != "passed":
            failures.append(f"{profile}: status is {result.get('status')!r}")
            continue
        summary_path = resolve_recorded_path(result.get("summary_json"), manifest_path=manifest_path)
        if summary_path is None or not summary_path.exists():
            failures.append(f"{profile}: summary JSON missing")
            continue
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        failures.extend(validate_summary(profile, summary))
    return failures


def validate_summary(profile: str, summary: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    profile_defaults = SOAK_PROFILES[profile]
    expected_duration = float(profile_defaults["duration_seconds"])
    minimum_duration = expected_duration * 0.95
    actual_duration = float(summary.get("duration_seconds", 0.0) or 0.0)
    if summary.get("failures"):
        failures.append(f"{profile}: summary failures: {summary['failures']}")
    if actual_duration < minimum_duration:
        failures.append(f"{profile}: duration too short: {actual_duration:.1f}s < {minimum_duration:.1f}s")
    if not summary.get("stopped_cleanly"):
        failures.append(f"{profile}: worker did not stop cleanly")
    if int(summary.get("session_log_segments", 0) or 0) < 1:
        failures.append(f"{profile}: session log was not created")
    if int(summary.get("session_log_rows", 0) or 0) < 1:
        failures.append(f"{profile}: session log rows missing")
    max_ui_event_gap = float(summary.get("max_ui_event_gap_seconds", 0.0) or 0.0)
    allowed_ui_event_gap = float(profile_defaults["max_ui_event_gap_seconds"])
    if max_ui_event_gap > allowed_ui_event_gap:
        failures.append(f"{profile}: UI event gap too high: {max_ui_event_gap:.3f}s > {allowed_ui_event_gap:.3f}s")
    return failures


def resolve_recorded_path(value: object, *, manifest_path: Path) -> Path | None:
    if not value:
        return None
    path = Path(str(value))
    if path.is_absolute():
        return path
    root_relative = ROOT / path
    if root_relative.exists():
        return root_relative
    manifest_relative = manifest_path.parent / path
    if manifest_relative.exists():
        return manifest_relative
    return root_relative


def write_manifest(
    path: Path,
    *,
    started_at: str,
    profiles: list[str],
    results: list[dict[str, Any]],
    finished_at: str | None,
) -> None:
    payload = {
        "suite_started_at": started_at,
        "suite_finished_at": finished_at,
        "root": str(ROOT),
        "profiles_requested": profiles,
        "planned_only": bool(results) and all(
            (latest_result(profile, results) or {}).get("status") == "planned" for profile in profiles
        ),
        "passed": bool(results) and all(
            (latest_result(profile, results) or {}).get("status") == "passed" for profile in profiles
        ),
        "results": results,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
