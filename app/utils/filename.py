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
    path = base / f"network_trace_{safe_target_name(target)}_{stamp}.{extension.lstrip('.')}"
    return available_path(path)


def available_path(path: Path) -> Path:
    if not path.exists():
        return path
    for suffix in range(2, 10_000):
        candidate = path.with_name(f"{path.stem}_{suffix}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"사용 가능한 파일명을 찾을 수 없습니다: {path}")
