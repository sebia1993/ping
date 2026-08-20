from __future__ import annotations

import csv
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.core.alerts import AlertEvent, is_route_alert_key


LEGACY_ALERT_ACTION_HEADERS = [
    "timestamp",
    "start",
    "end",
    "source",
    "severity",
    "title",
    "message",
    "actions",
]
ALERT_ACTION_HEADERS = [*LEGACY_ALERT_ACTION_HEADERS, "key"]
ALERT_ACTION_IO_RETRY_ATTEMPTS = 5
ALERT_ACTION_IO_RETRY_DELAY_SECONDS = 0.05
ALERT_ACTION_LOG_READ_FAILED_CODE = "ALERT_ACTION_LOG_READ_FAILED"
ALERT_ACTION_LOG_CORRUPTED_CODE = "ALERT_ACTION_LOG_CORRUPTED"


@dataclass(frozen=True)
class AlertActionLogReadSummary:
    """Loaded alert actions plus a visible read warning, if any."""

    rows: list[dict[str, str]]
    skipped_rows: int = 0
    error_code: str | None = None


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
    append_alert_actions(path, [(event, actions, source)])


def append_alert_actions(
    path: Path | None,
    entries: Iterable[tuple[AlertEvent, list[str], str | None]],
) -> None:
    if path is None:
        return
    rows = list(entries)
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = _alert_action_headers_for_path(path)
    handle = _open_alert_action_append_handle(path)
    try:
        for event, actions, source in rows:
            _append_alert_action_to_handle(
                handle,
                event,
                actions=actions,
                source=source,
                fieldnames=fieldnames,
            )
        _flush_with_retries(handle)
    finally:
        _close_handle_suppressing_errors(handle)


def read_alert_actions(path: Path | None) -> list[dict[str, str]]:
    return read_alert_actions_with_summary(path).rows


def read_alert_actions_with_summary(path: Path | None) -> AlertActionLogReadSummary:
    if path is None or not path.exists():
        return AlertActionLogReadSummary([])
    try:
        rows, skipped_rows = _run_io_with_retries(lambda: _read_alert_actions_once(path))
    except (OSError, UnicodeError, csv.Error):
        return AlertActionLogReadSummary([], error_code=ALERT_ACTION_LOG_READ_FAILED_CODE)
    return AlertActionLogReadSummary(
        rows,
        skipped_rows=skipped_rows,
        error_code=ALERT_ACTION_LOG_CORRUPTED_CODE if skipped_rows else None,
    )


def _format_dt(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _append_alert_action_to_handle(
    handle,
    event: AlertEvent,
    *,
    actions: list[str],
    source: str | None,
    fieldnames: list[str],
) -> None:
    writer = csv.DictWriter(handle, fieldnames=fieldnames)
    if handle.tell() == 0:
        writer.writeheader()
    row = {
        "timestamp": _format_dt(event.timestamp),
        "start": _format_dt(event.start),
        "end": _format_dt(event.end),
        "source": source or ("route" if is_route_alert_key(event.key) else "alert"),
        "severity": event.severity,
        "title": event.title,
        "message": event.message,
        "actions": ";".join(actions),
        "key": event.key,
    }
    writer.writerow({field: row[field] for field in fieldnames})


def _read_alert_actions_once(path: Path) -> tuple[list[dict[str, str]], int]:
    rows: list[dict[str, str]] = []
    skipped_rows = 0
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not _valid_alert_action_headers(reader.fieldnames):
            raise csv.Error("unsupported alert action log headers")
        for row in reader:
            if not _is_action_row(row):
                continue
            normalized = _normalize_action_row(row)
            if not _valid_alert_action_row(normalized, row):
                skipped_rows += 1
                continue
            rows.append(normalized)
    return rows, skipped_rows


def _valid_alert_action_headers(fieldnames: list[str] | None) -> bool:
    if fieldnames is None:
        return False
    return all(header in fieldnames for header in LEGACY_ALERT_ACTION_HEADERS)


def _valid_alert_action_row(
    normalized: dict[str, str],
    raw: dict[str | None, str | None],
) -> bool:
    if None in raw:
        return False
    for field in ("timestamp", "start", "end"):
        try:
            datetime.fromisoformat(normalized[field])
        except (TypeError, ValueError):
            return False
    return bool(normalized["source"] and normalized["severity"] and normalized["title"])


def _is_action_row(row: dict[str | None, str | None]) -> bool:
    values = [value.strip() for key, value in row.items() if key is not None and value]
    if not values:
        return False
    return not all(row.get(header) == header for header in LEGACY_ALERT_ACTION_HEADERS)


def _normalize_action_row(row: dict[str | None, str | None]) -> dict[str, str]:
    return {header: row.get(header) or "" for header in ALERT_ACTION_HEADERS}


def _alert_action_headers_for_path(path: Path) -> list[str]:
    """Keep appending old sessions with their original eight-column schema."""

    if not path.exists() or path.stat().st_size == 0:
        return ALERT_ACTION_HEADERS
    try:
        with path.open("r", newline="", encoding="utf-8") as handle:
            existing = next(csv.reader(handle), [])
    except (OSError, csv.Error):
        return ALERT_ACTION_HEADERS
    if "key" not in existing:
        return LEGACY_ALERT_ACTION_HEADERS
    return ALERT_ACTION_HEADERS


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
