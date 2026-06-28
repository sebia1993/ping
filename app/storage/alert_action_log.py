from __future__ import annotations

import csv
import time
from datetime import datetime
from pathlib import Path

from app.core.alerts import AlertEvent, is_route_alert_key


ALERT_ACTION_HEADERS = [
    "timestamp",
    "start",
    "end",
    "source",
    "severity",
    "title",
    "message",
    "actions",
]
ALERT_ACTION_IO_RETRY_ATTEMPTS = 5
ALERT_ACTION_IO_RETRY_DELAY_SECONDS = 0.05


def alert_action_log_path_for_session(session_log_path: Path | None) -> Path | None:
    if session_log_path is None:
        return None
    return session_log_path.with_name(f"{session_log_path.stem}.alerts.csv")


def append_alert_action(
    path: Path | None,
    event: AlertEvent,
    *,
    actions: list[str],
    source: str | None = None,
) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = _open_alert_action_append_handle(path)
    try:
        _append_alert_action_to_handle(handle, event, actions=actions, source=source)
        _flush_with_retries(handle)
    finally:
        _close_handle_suppressing_errors(handle)


def read_alert_actions(path: Path | None) -> list[dict[str, str]]:
    if path is None or not path.exists():
        return []
    try:
        return _run_io_with_retries(lambda: _read_alert_actions_once(path))
    except (OSError, csv.Error):
        return []


def _format_dt(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _append_alert_action_to_handle(
    handle,
    event: AlertEvent,
    *,
    actions: list[str],
    source: str | None,
) -> None:
    writer = csv.DictWriter(handle, fieldnames=ALERT_ACTION_HEADERS)
    if handle.tell() == 0:
        writer.writeheader()
    writer.writerow(
        {
            "timestamp": _format_dt(event.timestamp),
            "start": _format_dt(event.start),
            "end": _format_dt(event.end),
            "source": source or ("route" if is_route_alert_key(event.key) else "alert"),
            "severity": event.severity,
            "title": event.title,
            "message": event.message,
            "actions": ";".join(actions),
        }
    )


def _read_alert_actions_once(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as handle:
        return [_normalize_action_row(row) for row in csv.DictReader(handle) if _is_action_row(row)]


def _is_action_row(row: dict[str | None, str | None]) -> bool:
    values = [value.strip() for key, value in row.items() if key is not None and value]
    if not values:
        return False
    return not all(row.get(header) == header for header in ALERT_ACTION_HEADERS)


def _normalize_action_row(row: dict[str | None, str | None]) -> dict[str, str]:
    return {header: row.get(header) or "" for header in ALERT_ACTION_HEADERS}


def _open_alert_action_append_handle(path: Path):
    return _run_io_with_retries(lambda: _open_alert_action_append_path(path))


def _open_alert_action_append_path(path: Path):
    return path.open("a", newline="", encoding="utf-8")


def _flush_with_retries(handle) -> None:
    _run_io_with_retries(lambda: _flush_handle(handle))


def _flush_handle(handle) -> None:
    handle.flush()


def _close_handle_suppressing_errors(handle) -> None:
    try:
        handle.close()
    except OSError:
        pass


def _run_io_with_retries(operation):
    last_error: OSError | None = None
    for attempt in range(ALERT_ACTION_IO_RETRY_ATTEMPTS):
        try:
            return operation()
        except OSError as exc:
            last_error = exc
            if attempt == ALERT_ACTION_IO_RETRY_ATTEMPTS - 1:
                break
            time.sleep(ALERT_ACTION_IO_RETRY_DELAY_SECONDS)
    if last_error is not None:
        raise last_error
    return operation()
