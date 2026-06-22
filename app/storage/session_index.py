from __future__ import annotations

import json
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.storage.alert_action_log import alert_action_log_path_for_session
from app.storage.route_log import route_log_path_for_session


SESSION_STATE_ACTIVE = "Active"
SESSION_STATE_PAUSED = "Pause"
SESSION_STATE_ARCHIVED = "Archived"
SESSION_STATE_WILL_DELETE = "Will Delete"
SESSION_INDEX_IO_RETRY_ATTEMPTS = 5
SESSION_INDEX_IO_RETRY_DELAY_SECONDS = 0.05


@dataclass(frozen=True)
class TraceSessionRecord:
    session_id: str
    target: str
    sample_path: Path
    route_path: Path | None
    start: datetime
    end: datetime | None
    samples: int
    state: str
    interval_seconds: int | None = None
    measurement_mode: str = ""
    target_count: int = 1
    segments: tuple[Path, ...] = ()
    last_error: str = ""


class SessionIndexStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @classmethod
    def create(cls, root: Path | None = None) -> "SessionIndexStore":
        base_dir = root or Path.cwd() / "exports" / "session_logs"
        return cls(base_dir / "session_index.json")

    def register_session(
        self,
        *,
        target: str,
        sample_path: Path,
        route_path: Path | None,
        started_at: datetime,
        interval_seconds: int | None,
        measurement_mode: str,
        target_count: int,
    ) -> TraceSessionRecord:
        record = TraceSessionRecord(
            session_id=_session_id(sample_path),
            target=target,
            sample_path=sample_path,
            route_path=route_path,
            start=started_at,
            end=None,
            samples=0,
            state=SESSION_STATE_ACTIVE,
            interval_seconds=interval_seconds,
            measurement_mode=measurement_mode,
            target_count=target_count,
            segments=(sample_path,),
        )
        with self._lock:
            records = [item for item in self._read_records() if item.session_id != record.session_id]
            records.append(record)
            self._write_records(records)
        return record

    def add_samples(
        self,
        session_id: str,
        count: int,
        last_timestamp: datetime,
        *,
        segments: list[Path] | tuple[Path, ...] | None = None,
    ) -> None:
        if count <= 0:
            return
        with self._lock:
            records = self._read_records()
            updated = [
                _replace_record(
                    record,
                    samples=record.samples + count,
                    end=_max_datetime(record.end, last_timestamp),
                    segments=_merge_segments(record.segments, segments),
                )
                if record.session_id == session_id
                else record
                for record in records
            ]
            self._write_records(updated)

    def finish_session(
        self,
        session_id: str,
        *,
        state: str,
        ended_at: datetime | None = None,
        segments: list[Path] | tuple[Path, ...] | None = None,
        last_error: str = "",
    ) -> None:
        with self._lock:
            records = self._read_records()
            updated = [
                _replace_record(
                    record,
                    state=state,
                    end=ended_at or record.end or datetime.now(),
                    segments=_merge_segments(record.segments, segments),
                    last_error=last_error,
                )
                if record.session_id == session_id
                else record
                for record in records
            ]
            self._write_records(updated)

    def list_sessions(
        self,
        *,
        target: str | None = None,
        state: str | None = None,
    ) -> list[TraceSessionRecord]:
        records = self._read_records()
        if target:
            records = [record for record in records if record.target == target]
        if state:
            records = [record for record in records if record.state == state]
        return sorted(records, key=lambda record: record.start, reverse=True)

    def find_session(self, session_id: str) -> TraceSessionRecord | None:
        return next((record for record in self._read_records() if record.session_id == session_id), None)

    def delete_session(self, session_id: str, *, delete_files: bool = True) -> TraceSessionRecord | None:
        with self._lock:
            records = self._read_records()
            record = next((item for item in records if item.session_id == session_id), None)
            if record is None:
                return None
            self._write_records([item for item in records if item.session_id != session_id])
        if delete_files:
            delete_session_files(record, root=self.path.parent)
        return record

    def _read_records(self) -> list[TraceSessionRecord]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(_read_text_with_retries(self.path))
        except (OSError, json.JSONDecodeError):
            return []
        rows = data.get("sessions", []) if isinstance(data, dict) else []
        records: list[TraceSessionRecord] = []
        for row in rows:
            try:
                records.append(_record_from_row(row))
            except (KeyError, TypeError, ValueError):
                continue
        return records

    def _write_records(self, records: list[TraceSessionRecord]) -> None:
        payload = {
            "version": 1,
            "sessions": [_record_to_row(record) for record in sorted(records, key=lambda item: item.start)],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            delete=False,
            dir=self.path.parent,
            encoding="utf-8",
            newline="",
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            temp_path = Path(handle.name)
        try:
            _replace_with_retries(temp_path, self.path)
        except OSError:
            temp_path.unlink(missing_ok=True)
            raise


def _session_id(sample_path: Path) -> str:
    return sample_path.stem


def _read_text_with_retries(path: Path) -> str:
    return _run_io_with_retries(lambda: _read_text_path(path))


def _read_text_path(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _replace_with_retries(source: Path, target: Path) -> None:
    _run_io_with_retries(lambda: _replace_path(source, target))


def _replace_path(source: Path, target: Path) -> Path:
    return source.replace(target)


def _run_io_with_retries(operation):
    last_error: OSError | None = None
    for attempt in range(SESSION_INDEX_IO_RETRY_ATTEMPTS):
        try:
            return operation()
        except PermissionError as exc:
            last_error = exc
            if attempt == SESSION_INDEX_IO_RETRY_ATTEMPTS - 1:
                break
            time.sleep(SESSION_INDEX_IO_RETRY_DELAY_SECONDS)
    if last_error is not None:
        raise last_error
    return operation()


def session_index_root_for_sample_path(sample_path: Path) -> Path:
    if _is_year_month_folder(sample_path.parent.name):
        return sample_path.parent.parent.parent
    return sample_path.parent


def _record_to_row(record: TraceSessionRecord) -> dict[str, object]:
    return {
        "session_id": record.session_id,
        "target": record.target,
        "sample_path": str(record.sample_path),
        "route_path": str(record.route_path) if record.route_path is not None else "",
        "start": record.start.isoformat(timespec="seconds"),
        "end": record.end.isoformat(timespec="seconds") if record.end is not None else "",
        "samples": record.samples,
        "state": record.state,
        "interval_seconds": record.interval_seconds,
        "measurement_mode": record.measurement_mode,
        "target_count": record.target_count,
        "segments": [str(path) for path in record.segments],
        "last_error": record.last_error,
    }


def _record_from_row(row: dict[str, object]) -> TraceSessionRecord:
    end_value = str(row.get("end") or "")
    route_value = str(row.get("route_path") or "")
    return TraceSessionRecord(
        session_id=str(row["session_id"]),
        target=str(row["target"]),
        sample_path=Path(str(row["sample_path"])),
        route_path=Path(route_value) if route_value else None,
        start=datetime.fromisoformat(str(row["start"])),
        end=datetime.fromisoformat(end_value) if end_value else None,
        samples=int(row.get("samples") or 0),
        state=str(row.get("state") or SESSION_STATE_ARCHIVED),
        interval_seconds=_optional_int(row.get("interval_seconds")),
        measurement_mode=str(row.get("measurement_mode") or ""),
        target_count=int(row.get("target_count") or 1),
        segments=tuple(Path(str(path)) for path in row.get("segments", []) or []),
        last_error=str(row.get("last_error") or ""),
    )


def _replace_record(record: TraceSessionRecord, **updates) -> TraceSessionRecord:
    values = {
        "session_id": record.session_id,
        "target": record.target,
        "sample_path": record.sample_path,
        "route_path": record.route_path,
        "start": record.start,
        "end": record.end,
        "samples": record.samples,
        "state": record.state,
        "interval_seconds": record.interval_seconds,
        "measurement_mode": record.measurement_mode,
        "target_count": record.target_count,
        "segments": record.segments,
        "last_error": record.last_error,
    }
    values.update(updates)
    return TraceSessionRecord(**values)


def _optional_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _max_datetime(current: datetime | None, candidate: datetime) -> datetime:
    return candidate if current is None or candidate > current else current


def _merge_segments(
    current: tuple[Path, ...],
    extra: list[Path] | tuple[Path, ...] | None,
) -> tuple[Path, ...]:
    if not extra:
        return current
    seen = {str(path) for path in current}
    merged = list(current)
    for path in extra:
        if str(path) in seen:
            continue
        merged.append(path)
        seen.add(str(path))
    return tuple(merged)


def session_data_paths(record: TraceSessionRecord) -> tuple[Path, ...]:
    paths: list[Path | None] = [
        record.sample_path,
        *record.segments,
        record.route_path,
        route_log_path_for_session(record.sample_path),
        alert_action_log_path_for_session(record.sample_path),
    ]
    if record.sample_path.parent.exists():
        paths.extend(sorted(record.sample_path.parent.glob(f"{record.sample_path.stem}.part*{record.sample_path.suffix}")))
    return _dedupe_paths(path for path in paths if path is not None)


def delete_session_files(record: TraceSessionRecord, *, root: Path) -> tuple[Path, ...]:
    deleted: list[Path] = []
    for path in session_data_paths(record):
        if not _is_managed_file(path, root):
            continue
        try:
            path.unlink()
        except (FileNotFoundError, OSError):
            continue
        deleted.append(path)
    return tuple(deleted)


def _dedupe_paths(paths) -> tuple[Path, ...]:
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        unique.append(path)
        seen.add(key)
    return tuple(unique)


def _is_managed_file(path: Path, root: Path) -> bool:
    if not path.is_file():
        return False
    resolved_root = root.resolve(strict=False)
    resolved_path = path.resolve(strict=False)
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError:
        return False
    return True


def _is_year_month_folder(value: str) -> bool:
    if len(value) != 7 or value[4] != "-":
        return False
    year, month = value.split("-", 1)
    return year.isdigit() and month.isdigit()
