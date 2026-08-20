from __future__ import annotations

import time
from pathlib import Path

from app.ui.session_archive_worker import (
    SESSION_ARCHIVE_WRITE_FAILED_CODE,
    SessionArchiveWorker,
)


def _wait_for_worker(worker: SessionArchiveWorker, qt_app, timeout_seconds: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while worker.isRunning() and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.01)
    assert worker.wait(1000)
    qt_app.processEvents()


def test_session_archive_worker_runs_writer_off_ui_thread(qt_app, tmp_path) -> None:
    calls: list[tuple[Path, int]] = []
    completed: list[tuple[str, int, int]] = []

    def writer(path, records):
        calls.append((path, len(records)))
        return path, 3

    worker = SessionArchiveWorker(
        path=tmp_path / "sessions.zip",
        records=[],
        writer=writer,
    )
    worker.completed.connect(lambda path, files, sessions: completed.append((path, files, sessions)))
    worker.start()
    _wait_for_worker(worker, qt_app)

    assert calls == [(tmp_path / "sessions.zip", 0)]
    assert completed == [(str(tmp_path / "sessions.zip"), 3, 0)]


def test_session_archive_worker_reports_sanitized_write_failure(qt_app, tmp_path) -> None:
    errors: list[str] = []

    def writer(_path, _records):
        raise PermissionError(f"locked {tmp_path / 'private.zip'}")

    worker = SessionArchiveWorker(
        path=tmp_path / "sessions.zip",
        records=[],
        writer=writer,
    )
    worker.error_message.connect(errors.append)
    worker.start()
    _wait_for_worker(worker, qt_app)

    assert errors == [f"{SESSION_ARCHIVE_WRITE_FAILED_CODE}: PermissionError"]
    assert str(tmp_path) not in errors[0]
