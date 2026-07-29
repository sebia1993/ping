from __future__ import annotations

import threading
import time
from datetime import datetime

from app.core.alerts import AlertEvent
from app.ui.alert_action_dispatcher import (
    ALERT_ACTION_CANCELLED_CODE,
    ALERT_ACTION_QUEUE_FULL_CODE,
    AlertActionDispatcher,
)


def _alert_event(key: str) -> AlertEvent:
    now = datetime(2026, 1, 1, 12, 0, 0)
    return AlertEvent(key, now, now, now, "warning", "Test alert", "Test message")


def _wait_for_outcomes(qt_app, outcomes: list[tuple[str, bool, str]], count: int) -> None:
    deadline = time.monotonic() + 1.0
    while len(outcomes) < count and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.005)


def test_alert_action_dispatcher_cancels_queued_work_on_shutdown(qt_app) -> None:
    dispatcher = AlertActionDispatcher(max_pending=2)
    started = threading.Event()
    release = threading.Event()
    queued_job_ran = threading.Event()
    outcomes: list[tuple[str, bool, str]] = []
    dispatcher.completed.connect(
        lambda _event, action, success, message: outcomes.append((action, success, message))
    )

    def blocking_job() -> tuple[bool, str]:
        started.set()
        release.wait(1.0)
        return True, ""

    try:
        assert dispatcher.submit(_alert_event("first"), "first", blocking_job) is True
        assert started.wait(0.5)
        assert dispatcher.submit(
            _alert_event("second"),
            "second",
            lambda: (queued_job_ran.set() or True, ""),
        ) is True

        dispatcher.shutdown()
        release.set()

        assert dispatcher.wait(1.0) is True
        _wait_for_outcomes(qt_app, outcomes, 2)
        assert queued_job_ran.is_set() is False
        assert ("second", False, ALERT_ACTION_CANCELLED_CODE) in outcomes
        assert ("first", True, "") in outcomes
    finally:
        release.set()
        dispatcher.shutdown()
        dispatcher.wait(1.0)


def test_alert_action_dispatcher_rejects_work_when_queue_is_full(qt_app) -> None:
    dispatcher = AlertActionDispatcher(max_pending=1)
    started = threading.Event()
    release = threading.Event()
    outcomes: list[tuple[str, bool, str]] = []
    dispatcher.completed.connect(
        lambda _event, action, success, message: outcomes.append((action, success, message))
    )

    def blocking_job() -> tuple[bool, str]:
        started.set()
        release.wait(1.0)
        return True, ""

    try:
        assert dispatcher.submit(_alert_event("first"), "first", blocking_job) is True
        assert started.wait(0.5)
        assert dispatcher.submit(_alert_event("second"), "second", lambda: (True, "")) is False

        _wait_for_outcomes(qt_app, outcomes, 1)
        assert ("second", False, ALERT_ACTION_QUEUE_FULL_CODE) in outcomes
    finally:
        dispatcher.shutdown()
        release.set()
        assert dispatcher.wait(1.0) is True
