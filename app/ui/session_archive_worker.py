from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from app.storage.session_index import TraceSessionRecord
from app.utils.diagnostics import operation_failure


SESSION_ARCHIVE_WRITE_FAILED_CODE = "SESSION_ARCHIVE_WRITE_FAILED"
SESSION_ARCHIVE_UNEXPECTED_ERROR_CODE = "SESSION_ARCHIVE_UNEXPECTED_ERROR"

ArchiveWriter = Callable[[Path, list[TraceSessionRecord]], tuple[Path, int]]


class SessionArchiveWorker(QThread):
    """Write the visible-session ZIP away from the Qt event loop."""

    status_message = Signal(str)
    completed = Signal(str, int, int)
    error_message = Signal(str)

    def __init__(
        self,
        *,
        path: Path,
        records: list[TraceSessionRecord],
        writer: ArchiveWriter,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.path = path
        self.records = list(records)
        self.writer = writer

    def run(self) -> None:
        self.status_message.emit("SESSION_ARCHIVE_RUNNING")
        try:
            saved_path, file_count = self.writer(self.path, self.records)
        except OSError as exc:
            operation_failure(
                SESSION_ARCHIVE_WRITE_FAILED_CODE,
                "session_archive.write",
                exc,
                session_path=self.path,
            )
            self.error_message.emit(f"{SESSION_ARCHIVE_WRITE_FAILED_CODE}: {type(exc).__name__}")
            return
        except Exception as exc:
            operation_failure(
                SESSION_ARCHIVE_UNEXPECTED_ERROR_CODE,
                "session_archive.write",
                exc,
                session_path=self.path,
            )
            self.error_message.emit(f"{SESSION_ARCHIVE_UNEXPECTED_ERROR_CODE}: {type(exc).__name__}")
            return
        self.completed.emit(str(saved_path), file_count, len(self.records))
