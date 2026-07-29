from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


APP_DIRECTORY_NAME = "MultiPingCheck"
APP_DATA_DIR_ENV = "MULTIPINGCHECK_DATA_DIR"
APP_EXPORT_DIR_ENV = "MULTIPINGCHECK_EXPORT_DIR"
LEGACY_SESSION_DIR_ENV = "MULTIPINGCHECK_LEGACY_SESSION_DIR"


def application_directory() -> Path:
    """Return the packaged application folder or the repository root."""

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def app_data_directory() -> Path:
    """Return a per-user writable folder for internal application data."""

    override = _environment_path(APP_DATA_DIR_ENV)
    if override is not None:
        return override

    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data).expanduser() / APP_DIRECTORY_NAME

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIRECTORY_NAME
    return Path.home() / ".local" / "share" / APP_DIRECTORY_NAME


def session_logs_directory() -> Path:
    return app_data_directory() / "session_logs"


def diagnostic_logs_directory() -> Path:
    return app_data_directory() / "logs"


def alert_images_directory() -> Path:
    return app_data_directory() / "alert_images"


def user_exports_directory() -> Path:
    """Return the default folder for files explicitly exported by the user."""

    override = _environment_path(APP_EXPORT_DIR_ENV)
    if override is not None:
        return override
    return Path.home() / "Documents" / APP_DIRECTORY_NAME


def fallback_logs_directory() -> Path:
    return Path(tempfile.gettempdir()) / APP_DIRECTORY_NAME / "logs"


def legacy_session_log_directories() -> tuple[Path, ...]:
    """Return existing locations used by releases that stored data under cwd."""

    explicit_legacy = _environment_path(LEGACY_SESSION_DIR_ENV)
    if explicit_legacy is not None:
        return (explicit_legacy,) if explicit_legacy.exists() else ()
    if os.environ.get(APP_DATA_DIR_ENV, "").strip():
        return ()

    candidates = [
        application_directory() / "exports" / "session_logs",
        Path.cwd() / "exports" / "session_logs",
    ]
    unique: list[Path] = []
    seen: set[str] = set()
    current = _normalized_path_key(session_logs_directory())
    for candidate in candidates:
        key = _normalized_path_key(candidate)
        if key == current or key in seen or not candidate.exists():
            continue
        seen.add(key)
        unique.append(candidate)
    return tuple(unique)


def _environment_path(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    if not value:
        return None
    return Path(os.path.expandvars(value)).expanduser()


def _normalized_path_key(path: Path) -> str:
    try:
        value = str(path.resolve(strict=False))
    except OSError:
        value = str(path.absolute())
    return os.path.normcase(value)
