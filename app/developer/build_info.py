from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from app import __version__
from app.developer.models import BuildInfo, UNKNOWN_VALUE


BUILD_INFO_FILE_NAME = "multipingcheck_build_info.json"
BUILD_INFO_PATH_ENV = "MULTIPINGCHECK_BUILD_INFO_PATH"
CONFIG_SCHEMA_VERSION = "target-group=2; alert-rule=2; session-index=2"


def load_build_info(path: Path | None = None) -> BuildInfo:
    """Load immutable build metadata, falling back to source-checkout facts.

    Packaged applications read only the JSON embedded by the build script. This
    avoids requiring Git or a source checkout on the company test PC.
    """

    for candidate in _build_info_candidates(path):
        if not candidate.is_file():
            continue
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            return BuildInfo.from_mapping(payload)
    return _source_checkout_build_info()


def _build_info_candidates(path: Path | None) -> tuple[Path, ...]:
    candidates: list[Path] = []
    if path is not None:
        candidates.append(path)
    configured_path = os.environ.get(BUILD_INFO_PATH_ENV, "").strip()
    if configured_path:
        candidates.append(Path(configured_path).expanduser())
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        candidates.append(Path(bundle_root) / BUILD_INFO_FILE_NAME)
    candidates.append(Path(__file__).resolve().parents[2] / BUILD_INFO_FILE_NAME)
    return tuple(dict.fromkeys(candidates))


def _source_checkout_build_info() -> BuildInfo:
    if getattr(sys, "frozen", False):
        return BuildInfo(program_version=__version__)

    root = Path(__file__).resolve().parents[2]
    commit = _git_value(root, "rev-parse", "HEAD")
    branch = _git_value(root, "branch", "--show-current")
    source_state = _git_source_state(root)
    return BuildInfo(
        program_version=__version__,
        git_commit=commit,
        git_branch=branch,
        distribution="소스 코드 실행",
        config_schema_version=CONFIG_SCHEMA_VERSION,
        source_state=source_state,
    )


def _git_value(root: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2,
            check=False,
            creationflags=_no_window_creation_flags(),
        )
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN_VALUE
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else UNKNOWN_VALUE


def _git_source_state(root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2,
            check=False,
            creationflags=_no_window_creation_flags(),
        )
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN_VALUE
    if completed.returncode != 0:
        return UNKNOWN_VALUE
    return "미커밋 변경 포함" if completed.stdout.strip() else "커밋과 일치"


def _no_window_creation_flags() -> int:
    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
