from __future__ import annotations

import csv
import json
import re
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from app.core.models import HopInfo
from app.core.route_history import RouteChange, RouteSnapshot, route_change, route_snapshot


ROUTE_LOG_HEADERS = [
    "record_type",
    "timestamp",
    "previous_timestamp",
    "current_timestamp",
    "previous_signature",
    "current_signature",
    "changed_hops",
    "added_hops",
    "removed_hops",
    "summary",
]
ROUTE_LOG_IO_RETRY_ATTEMPTS = 5
ROUTE_LOG_IO_RETRY_DELAY_SECONDS = 0.05


class RouteLogWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = _open_csv_write_handle(self.path)
        self._writer = csv.DictWriter(self._handle, fieldnames=ROUTE_LOG_HEADERS)
        self._writer.writeheader()

    @classmethod
    def create_for_session(cls, session_log_path: Path) -> "RouteLogWriter":
        return cls(route_log_path_for_session(session_log_path))

    def write_snapshot(self, snapshot: RouteSnapshot, change: RouteChange | None = None) -> None:
        self._writer.writerow(_snapshot_row(snapshot))
        if change is not None:
            self._writer.writerow(_change_row(change))
        _flush_with_retries(self._handle)

    def close(self) -> None:
        if self._handle.closed:
            return
        try:
            _flush_with_retries(self._handle)
        finally:
            _close_handle_suppressing_errors(self._handle)

    def __enter__(self) -> "RouteLogWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def route_log_path_for_session(session_log_path: Path | None) -> Path | None:
    if session_log_path is None:
        return None
    stem = _base_session_stem(session_log_path.stem)
    return session_log_path.with_name(f"{stem}.routes.csv")


def iter_route_changes(path: Path | None) -> Iterator[RouteChange]:
    try:
        if path is None or not path.exists():
            return
        with _open_csv_read_handle(path) as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                if row.get("record_type") != "change":
                    continue
                try:
                    yield row_to_route_change(row)
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
    except (OSError, csv.Error):
        return


def route_changes_in_range(path: Path | None, start: datetime, end: datetime) -> list[RouteChange]:
    if end < start:
        start, end = end, start
    return [change for change in iter_route_changes(path) if start <= change.timestamp <= end]


def row_to_route_change(row: dict[str, str]) -> RouteChange:
    previous_time = datetime.fromisoformat(row["previous_timestamp"])
    current_time = datetime.fromisoformat(row["current_timestamp"])
    previous = _snapshot_from_signature(previous_time, _load_string_tuple(row["previous_signature"]))
    current = _snapshot_from_signature(current_time, _load_string_tuple(row["current_signature"]))
    reconstructed = route_change(previous, current)
    return RouteChange(
        timestamp=current.timestamp,
        previous=previous,
        current=current,
        changed_hops=_load_int_tuple(row.get("changed_hops", "")) or reconstructed.changed_hops,
        added_hops=_load_int_tuple(row.get("added_hops", "")) or reconstructed.added_hops,
        removed_hops=_load_int_tuple(row.get("removed_hops", "")) or reconstructed.removed_hops,
        summary=row.get("summary") or reconstructed.summary,
    )


def _snapshot_row(snapshot: RouteSnapshot) -> dict[str, str]:
    return {
        "record_type": "snapshot",
        "timestamp": snapshot.timestamp.isoformat(timespec="seconds"),
        "previous_timestamp": "",
        "current_timestamp": snapshot.timestamp.isoformat(timespec="seconds"),
        "previous_signature": "",
        "current_signature": _dump_tuple(snapshot.signature),
        "changed_hops": "",
        "added_hops": "",
        "removed_hops": "",
        "summary": "snapshot",
    }


def _change_row(change: RouteChange) -> dict[str, str]:
    return {
        "record_type": "change",
        "timestamp": change.timestamp.isoformat(timespec="seconds"),
        "previous_timestamp": change.previous.timestamp.isoformat(timespec="seconds"),
        "current_timestamp": change.current.timestamp.isoformat(timespec="seconds"),
        "previous_signature": _dump_tuple(change.previous.signature),
        "current_signature": _dump_tuple(change.current.signature),
        "changed_hops": _dump_tuple(change.changed_hops),
        "added_hops": _dump_tuple(change.added_hops),
        "removed_hops": _dump_tuple(change.removed_hops),
        "summary": change.summary,
    }


def _snapshot_from_signature(timestamp: datetime, signature: tuple[str, ...]) -> RouteSnapshot:
    hops: list[HopInfo] = []
    for value in signature:
        index_value, node, timeout, target = value.split("|", 3)
        timed_out = timeout == "timeout"
        address = None if timed_out or node == "Timeout" else node
        hops.append(
            HopInfo(
                index=int(index_value),
                address=address,
                hostname=None,
                timed_out=timed_out,
                is_target=(target == "target"),
            )
        )
    return route_snapshot(hops, timestamp)


def _dump_tuple(values: tuple[object, ...]) -> str:
    return json.dumps(list(values), ensure_ascii=False, separators=(",", ":"))


def _load_string_tuple(value: str) -> tuple[str, ...]:
    return tuple(str(item) for item in json.loads(value or "[]"))


def _load_int_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(item) for item in json.loads(value or "[]"))


def _open_csv_read_handle(path: Path):
    return _run_io_with_retries(lambda: _open_csv_read_path(path))


def _open_csv_read_path(path: Path):
    return path.open("r", newline="", encoding="utf-8")


def _open_csv_write_handle(path: Path):
    return _run_io_with_retries(lambda: _open_csv_write_path(path))


def _open_csv_write_path(path: Path):
    return path.open("w", newline="", encoding="utf-8")


def _flush_with_retries(handle) -> None:
    _run_io_with_retries(lambda: _flush_handle(handle))


def _flush_handle(handle) -> None:
    handle.flush()


def _close_handle_suppressing_errors(handle) -> None:
    try:
        _close_handle(handle)
    except OSError:
        pass


def _close_handle(handle) -> None:
    handle.close()


def _run_io_with_retries(operation):
    last_error: OSError | None = None
    for attempt in range(ROUTE_LOG_IO_RETRY_ATTEMPTS):
        try:
            return operation()
        except OSError as exc:
            last_error = exc
            if attempt == ROUTE_LOG_IO_RETRY_ATTEMPTS - 1:
                break
            time.sleep(ROUTE_LOG_IO_RETRY_DELAY_SECONDS)
    if last_error is not None:
        raise last_error
    return operation()


def _base_session_stem(stem: str) -> str:
    return re.sub(r"\.part\d{3}$", "", stem)
