from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path


SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_target_name(target: str) -> str:
    cleaned = SAFE_NAME_RE.sub("_", target.strip())
    return cleaned.strip("._") or "target"


def default_export_path(target: str, extension: str, base_dir: Path | None = None) -> Path:
    base = base_dir or Path.cwd() / "exports"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return base / f"network_trace_{safe_target_name(target)}_{stamp}.{extension.lstrip('.')}"

