from __future__ import annotations

import logging
from pathlib import Path


def configure_logging(log_dir: Path | None = None) -> None:
    target_dir = log_dir or Path.cwd() / "logs"
    target_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=target_dir / "network_path_diagnostics.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

