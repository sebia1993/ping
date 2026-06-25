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
    started_at = datetime.now().isoformat(timespec="seconds")
    results: list[dict[str, Any]] = []

    for profile in args.profiles:
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
    return 0 if all(result["status"] in {"passed", "planned"} for result in results) else 1


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
        "planned_only": bool(results) and all(result["status"] == "planned" for result in results),
        "passed": bool(results) and all(result["status"] == "passed" for result in results),
        "results": results,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
