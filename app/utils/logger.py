from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.utils.app_paths import diagnostic_logs_directory, fallback_logs_directory


LOG_FILE_NAME = "multipingcheck.log"
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUP_COUNT = 3


def configure_logging(log_dir: Path | None = None) -> Path | None:
    """Configure a rotating local diagnostic log without blocking startup."""

    candidates = [log_dir] if log_dir is not None else [diagnostic_logs_directory(), fallback_logs_directory()]
    root_logger = logging.getLogger()
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            log_path = candidate / LOG_FILE_NAME
            handler = RotatingFileHandler(
                log_path,
                maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
        except OSError:
            continue
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        _replace_managed_handlers(root_logger, handler)
        root_logger.setLevel(logging.INFO)
        return log_path

    if not root_logger.handlers:
        root_logger.addHandler(logging.NullHandler())
    return None


def _replace_managed_handlers(root_logger: logging.Logger, new_handler: logging.Handler) -> None:
    for handler in list(root_logger.handlers):
        if getattr(handler, "_multipingcheck_handler", False):
            root_logger.removeHandler(handler)
            handler.close()
    setattr(new_handler, "_multipingcheck_handler", True)
    root_logger.addHandler(new_handler)
