from __future__ import annotations

import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar


EXPORT_IO_RETRY_ATTEMPTS = 5
EXPORT_IO_RETRY_DELAY_SECONDS = 0.05

T = TypeVar("T")


def atomic_write_path(path: Path, write_temp: Callable[[Path], T]) -> T:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = _make_temp_path(path)
    try:
        result = write_temp(temp_path)
        _run_io_with_retries(lambda: _replace_path(temp_path, path))
        return result
    except Exception:
        _unlink_temp_path(temp_path)
        raise


def read_text_with_retries(path: Path, *, encoding: str = "utf-8") -> str:
    return _run_io_with_retries(lambda: _read_text_path(path, encoding=encoding))


def _make_temp_path(path: Path) -> Path:
    handle = tempfile.NamedTemporaryFile(
        delete=False,
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=path.suffix,
    )
    handle.close()
    return Path(handle.name)


def _replace_path(source: Path, target: Path) -> None:
    os.replace(source, target)


def _read_text_path(path: Path, *, encoding: str) -> str:
    return path.read_text(encoding=encoding)


def _unlink_temp_path(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _run_io_with_retries(action: Callable[[], T]) -> T:
    last_error: OSError | None = None
    for attempt in range(EXPORT_IO_RETRY_ATTEMPTS):
        try:
            return action()
        except OSError as exc:
            last_error = exc
            if attempt >= EXPORT_IO_RETRY_ATTEMPTS - 1:
                raise
            time.sleep(EXPORT_IO_RETRY_DELAY_SECONDS)
    assert last_error is not None
    raise last_error
