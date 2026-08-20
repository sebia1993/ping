from __future__ import annotations

import hashlib
import logging
from pathlib import Path


DIAGNOSTICS_LOGGER_NAME = "app.diagnostics"


def reference(value: object | None) -> str:
    """Return a short, repeatable reference without writing the original value.

    Targets, session paths, and webhook details can contain operational data.
    A stable hash still lets an operator connect related log entries without
    putting that data into the local diagnostic log.
    """

    if value is None:
        return "-"
    text = str(value)
    if not text:
        return "-"
    return hashlib.blake2s(text.encode("utf-8", errors="replace"), digest_size=6).hexdigest()


def operation_failure(
    code: str,
    stage: str,
    exc: BaseException,
    *,
    target: str | None = None,
    session_path: Path | str | None = None,
    logger: logging.Logger | None = None,
) -> None:
    """Record a sanitized failure for later field diagnosis.

    Do not include ``str(exc)`` here.  operating-system errors often contain
    absolute paths, URLs, or other values that should stay out of diagnostics.
    """

    active_logger = logger or logging.getLogger(DIAGNOSTICS_LOGGER_NAME)
    active_logger.error(
        "event=%s stage=%s target_ref=%s session_ref=%s exception=%s",
        code,
        stage,
        reference(target),
        reference(session_path),
        type(exc).__name__,
    )
