from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor

from PySide6.QtCore import QObject, Signal

from app.core.alerts import AlertEvent


ALERT_ACTION_QUEUE_FULL_CODE = "ALERT_ACTION_QUEUE_FULL"
ALERT_ACTION_CANCELLED_CODE = "ALERT_ACTION_CANCELLED"
ALERT_ACTION_UNEXPECTED_ERROR_CODE = "ALERT_ACTION_UNEXPECTED_ERROR"
DEFAULT_MAX_PENDING_ACTIONS = 32


class AlertActionDispatcher(QObject):
    """Run bounded external alert actions away from the Qt UI thread."""

    completed = Signal(object, str, bool, str)

    def __init__(self, *, max_pending: int = DEFAULT_MAX_PENDING_ACTIONS, parent=None) -> None:
        super().__init__(parent)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="alert-action")
        self._slots = threading.BoundedSemaphore(max(max_pending, 1))
        self._state_changed = threading.Condition()
        self._closed = False
        self._pending_count = 0

    @property
    def pending_count(self) -> int:
        with self._state_changed:
            return self._pending_count

    def submit(
        self,
        event: AlertEvent,
        action: str,
        job: Callable[[], tuple[bool, str]],
    ) -> bool:
        with self._state_changed:
            closed = self._closed
        if closed:
            self.completed.emit(event, action, False, ALERT_ACTION_CANCELLED_CODE)
            return False
        if not self._slots.acquire(blocking=False):
            self.completed.emit(event, action, False, ALERT_ACTION_QUEUE_FULL_CODE)
            return False
        future: Future[tuple[bool, str]] | None = None
        rejected = False
        with self._state_changed:
            if self._closed:
                rejected = True
            else:
                self._pending_count += 1
                try:
                    future = self._executor.submit(job)
                except RuntimeError:
                    self._pending_count = max(self._pending_count - 1, 0)
                    self._state_changed.notify_all()
                    rejected = True
        if rejected or future is None:
            self._slots.release()
            self.completed.emit(event, action, False, ALERT_ACTION_CANCELLED_CODE)
            return False
        future.add_done_callback(lambda result: self._on_done(event, action, result))
        return True

    def shutdown(self) -> None:
        with self._state_changed:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)

    def wait(self, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + max(timeout_seconds, 0.0)
        with self._state_changed:
            while self._pending_count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._state_changed.wait(remaining)
            return True

    def _on_done(self, event: AlertEvent, action: str, future: Future[tuple[bool, str]]) -> None:
        try:
            success, message = future.result()
        except CancelledError:
            success = False
            message = ALERT_ACTION_CANCELLED_CODE
        except Exception as exc:
            success = False
            message = f"{ALERT_ACTION_UNEXPECTED_ERROR_CODE}: {type(exc).__name__}"
        finally:
            self._slots.release()
            with self._state_changed:
                self._pending_count = max(self._pending_count - 1, 0)
                self._state_changed.notify_all()
        self.completed.emit(event, action, bool(success), str(message))
