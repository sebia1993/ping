from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import __version__
from app.developer.build_info import CONFIG_SCHEMA_VERSION
from app.developer.models import UNKNOWN_VALUE


KST = timezone(timedelta(hours=9))


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate immutable MultiPingCheck build metadata.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--program-name", default="MultiPingCheck")
    parser.add_argument("--distribution", default="Windows Portable EXE")
    parser.add_argument("--build-id", default="")
    args = parser.parse_args()

    now = datetime.now(KST)
    build_id = args.build_id.strip() or os.environ.get("MULTIPINGCHECK_BUILD_ID", "").strip()
    if not build_id:
        build_id = now.strftime("%Y%m%d-%H%M%S")
    branch = _git_value("branch", "--show-current")
    if branch == UNKNOWN_VALUE:
        branch = os.environ.get("GITHUB_REF_NAME", "").strip() or UNKNOWN_VALUE
    source_state = _git_source_state()
    payload = {
        "program_name": args.program_name,
        "program_version": __version__,
        "build_id": build_id,
        "build_time": now.isoformat(timespec="seconds"),
        "git_commit": _git_value("rev-parse", "HEAD"),
        "git_branch": branch,
        "distribution": args.distribution,
        "config_schema_version": CONFIG_SCHEMA_VERSION,
        "source_state": source_state,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


def _git_value(*arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(ROOT), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
            check=False,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN_VALUE
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else UNKNOWN_VALUE


def _git_source_state() -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(ROOT), "status", "--porcelain"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
            check=False,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN_VALUE
    if completed.returncode != 0:
        return UNKNOWN_VALUE
    return "미커밋 변경 포함" if completed.stdout.strip() else "커밋과 일치"


if __name__ == "__main__":
    raise SystemExit(main())
