from __future__ import annotations

import json
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from app.storage.alert_action_log import alert_action_log_path_for_session
from app.storage.route_log import route_log_path_for_session
from app.storage.session_log import (
    iter_observations,
    session_log_read_summary,
    session_log_segment_index,
    session_log_segment_index_path,
)


# Session Manager 화면은 원본 CSV를 매번 전부 읽지 않고 이 작은 JSON 인덱스를 먼저 봅니다.
# 상태값은 "측정 중", "중지/복구됨", "보관됨", "삭제 예정" 정도로 이해하면 됩니다.
SESSION_STATE_ACTIVE = "Active"
SESSION_STATE_PAUSED = "Pause"
SESSION_STATE_ARCHIVED = "Archived"
SESSION_STATE_WILL_DELETE = "Will Delete"
SESSION_INDEX_IO_RETRY_ATTEMPTS = 5
SESSION_INDEX_IO_RETRY_DELAY_SECONDS = 0.05
SESSION_DELETE_FILES_FAILED_CODE = "SESSION_DELETE_FILES_FAILED"
SESSION_INDEX_REBUILT_CODE = "SESSION_INDEX_REBUILT"
SESSION_RECOVERED_WITH_SKIPPED_ROWS_CODE = "SESSION_RECOVERED_WITH_SKIPPED_ROWS"


@dataclass(frozen=True)
class TraceSessionRecord:
    """저장된 측정 세션 하나를 Session Manager에서 다루기 위한 요약 정보입니다."""

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
    probe_engine: str = ""
    tcp_port: int | None = None
    route_probe_engine: str = ""
    resumed_from_session_id: str = ""
    target_count: int = 1
    segments: tuple[Path, ...] = ()
    last_error: str = ""


@dataclass(frozen=True)
class SessionStorageSummary:
    target_count: int
    bucket_count: int
    segment_count: int
    sample_count: int


@dataclass(frozen=True)
class SessionStorageBucket:
    target: str
    month: str
    session_count: int
    segment_count: int
    sample_count: int
    state_counts: tuple[tuple[str, int], ...]
    latest_seen: datetime


class SessionIndexStore:
    """CSV 세션 파일 옆에 `session_index.json`을 두고 빠르게 목록/복구/삭제를 처리합니다.

    실제 측정 샘플은 CSV에 있고, 이 클래스는 그 CSV들이 어디에 있는지와 현재 상태를 기록합니다.
    인덱스가 깨지거나 없어도 `_recover_records_from_logs()`가 CSV를 다시 훑어 복구할 수 있습니다.
    """

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
        probe_engine: str | None = None,
        tcp_port: int | None = None,
        route_probe_engine: str | None = None,
        resumed_from_session_id: str = "",
    ) -> TraceSessionRecord:
        """새 측정이 시작될 때 세션 인덱스에 Active 레코드를 등록합니다."""

        fallback_probe_engine, fallback_tcp_port, fallback_route_engine = _probe_fields_from_measurement_mode(
            measurement_mode
        )
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
            probe_engine=probe_engine or fallback_probe_engine,
            tcp_port=tcp_port if tcp_port is not None else fallback_tcp_port,
            route_probe_engine=route_probe_engine or fallback_route_engine,
            resumed_from_session_id=resumed_from_session_id,
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
        """백그라운드 저장 스레드가 CSV에 쓴 샘플 수와 마지막 시간을 인덱스에 반영합니다."""

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
        """정상 종료, 중지, 오류 같은 최종 상태를 세션 인덱스에 남깁니다."""

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
        recover_missing: bool = False,
    ) -> list[TraceSessionRecord]:
        records = self._read_records()
        if recover_missing:
            records = self._merge_recovered_records(records)
        if target:
            records = [record for record in records if record.target == target]
        if state:
            records = [record for record in records if record.state == state]
        return sorted(records, key=lambda record: record.start, reverse=True)

    def recover_missing_sessions(self) -> list[TraceSessionRecord]:
        with self._lock:
            return self._merge_recovered_records(self._read_records())

    def recover_stale_active_sessions(
        self,
        *,
        stale_after: timedelta,
        now: datetime | None = None,
    ) -> list[TraceSessionRecord]:
        """프로그램이 강제 종료되어 Active로 남은 오래된 세션을 Pause 상태로 돌립니다."""

        now = now or datetime.now()
        cutoff = now - stale_after
        recovered: list[TraceSessionRecord] = []
        with self._lock:
            records = self._read_records()
            updated: list[TraceSessionRecord] = []
            for record in records:
                if record.state == SESSION_STATE_ACTIVE and _session_last_seen(record) < cutoff:
                    refreshed = _record_with_reconciled_log_metadata(record)
                    paused = _replace_record(
                        refreshed,
                        state=SESSION_STATE_PAUSED,
                        end=refreshed.end or refreshed.start,
                        last_error="Recovered stale active session after restart",
                    )
                    recovered.append(paused)
                    updated.append(paused)
                else:
                    updated.append(record)
            if recovered:
                self._write_records(updated)
        return recovered

    def reconcile_missing_session_files(self) -> list[TraceSessionRecord]:
        """인덱스에는 있지만 CSV가 사라진 세션을 삭제 예정 상태로 표시합니다."""

        updated_records: list[TraceSessionRecord] = []
        with self._lock:
            records = self._read_records()
            updated: list[TraceSessionRecord] = []
            for record in records:
                if record.sample_path.exists() or record.state == SESSION_STATE_WILL_DELETE:
                    updated.append(record)
                    continue
                marked = _replace_record(
                    record,
                    state=SESSION_STATE_WILL_DELETE,
                    end=record.end or record.start,
                    last_error=f"Session log missing: {record.sample_path}",
                )
                updated_records.append(marked)
                updated.append(marked)
            if updated_records:
                self._write_records(updated)
        return updated_records

    def retry_pending_deletions(self) -> list[TraceSessionRecord]:
        """파일 잠금 때문에 삭제 예정으로 남은 세션을 다시 정리합니다."""

        removed_records: list[TraceSessionRecord] = []
        with self._lock:
            records = self._read_records()
            kept: list[TraceSessionRecord] = []
            changed = False
            for record in records:
                if record.state != SESSION_STATE_WILL_DELETE:
                    kept.append(record)
                    continue
                if SESSION_DELETE_FILES_FAILED_CODE not in record.last_error:
                    kept.append(record)
                    continue
                _deleted, failures = _delete_session_files_with_failures(record, root=self.path.parent)
                if failures:
                    first_path, first_error = failures[0]
                    refreshed = _replace_record(
                        record,
                        end=record.end or datetime.now(),
                        last_error=(
                            f"{SESSION_DELETE_FILES_FAILED_CODE}: "
                            f"{type(first_error).__name__}: {first_path}"
                        ),
                    )
                    kept.append(refreshed)
                    changed = changed or refreshed != record
                    continue
                removed_records.append(record)
                changed = True
            if changed:
                self._write_records(kept)
        return removed_records

    def reconcile_session_log_metadata(self, session_id: str | None = None) -> list[TraceSessionRecord]:
        """이미 등록된 세션의 요약 정보를 실제 CSV 세그먼트 기준으로 다시 맞춥니다.

        강제 종료나 일시적인 인덱스 쓰기 실패가 있으면 `session_index.json`에는
        세션이 남아 있어도 샘플 수/세그먼트 목록이 실제 CSV보다 뒤처질 수 있습니다.
        수동 새로고침에서 이 보정을 수행하면 Session Manager가 저장된 로그를 더
        정확하게 보여줄 수 있습니다. `session_id`가 있으면 선택한 세션만 확인해
        세션 열기 같은 짧은 흐름에서 전체 로그를 다시 훑지 않게 합니다.
        """

        reconciled: list[TraceSessionRecord] = []
        with self._lock:
            records = self._read_records()
            updated: list[TraceSessionRecord] = []
            for record in records:
                if session_id is not None and record.session_id != session_id:
                    updated.append(record)
                    continue
                refreshed = _record_with_reconciled_log_metadata(record)
                if refreshed != record:
                    reconciled.append(refreshed)
                updated.append(refreshed)
            if reconciled:
                self._write_records(updated)
        return reconciled

    def find_session(self, session_id: str) -> TraceSessionRecord | None:
        return next((record for record in self._read_records() if record.session_id == session_id), None)

    def storage_buckets(self) -> list[SessionStorageBucket]:
        return session_storage_buckets(self.list_sessions())

    def delete_session(self, session_id: str, *, delete_files: bool = True) -> TraceSessionRecord | None:
        """세션 인덱스에서 항목을 제거하고 필요하면 관련 CSV/알림 파일도 삭제합니다."""

        with self._lock:
            records = self._read_records()
            record = next((item for item in records if item.session_id == session_id), None)
            if record is None:
                return None
            if not delete_files:
                self._write_records([item for item in records if item.session_id != session_id])
                return record

        _deleted, failures = _delete_session_files_with_failures(record, root=self.path.parent)
        with self._lock:
            records = self._read_records()
            if failures:
                first_path, first_error = failures[0]
                marked = _replace_record(
                    record,
                    state=SESSION_STATE_WILL_DELETE,
                    end=record.end or datetime.now(),
                    last_error=(
                        f"{SESSION_DELETE_FILES_FAILED_CODE}: "
                        f"{type(first_error).__name__}: {first_path}"
                    ),
                )
                self._write_records([
                    marked if item.session_id == session_id else item
                    for item in records
                ])
                return marked
            self._write_records([item for item in records if item.session_id != session_id])
        return record

    def prune_sessions_older_than(
        self,
        *,
        older_than: timedelta,
        now: datetime | None = None,
        delete_files: bool = True,
    ) -> list[TraceSessionRecord]:
        cutoff = (now or datetime.now()) - older_than
        pruned: list[TraceSessionRecord] = []
        failed_deletions: list[TraceSessionRecord] = []
        with self._lock:
            records = self._read_records()
            kept: list[TraceSessionRecord] = []
            for record in records:
                if record.state != SESSION_STATE_ACTIVE and _session_last_seen(record) < cutoff:
                    pruned.append(record)
                    continue
                kept.append(record)
            if pruned:
                self._write_records(kept)
        if delete_files:
            for record in pruned:
                _deleted, failures = _delete_session_files_with_failures(record, root=self.path.parent)
                if failures:
                    first_path, first_error = failures[0]
                    failed_deletions.append(
                        _replace_record(
                            record,
                            state=SESSION_STATE_WILL_DELETE,
                            end=record.end or datetime.now(),
                            last_error=(
                                f"{SESSION_DELETE_FILES_FAILED_CODE}: "
                                f"{type(first_error).__name__}: {first_path}"
                            ),
                        )
                    )
            if failed_deletions:
                with self._lock:
                    records = self._read_records()
                    existing_ids = {record.session_id for record in records}
                    records.extend(record for record in failed_deletions if record.session_id not in existing_ids)
                    self._write_records(records)
        return pruned

    def _read_records(self) -> list[TraceSessionRecord]:
        if not self.path.exists():
            return self._recover_records_from_logs()
        try:
            data = json.loads(_read_text_with_retries(self.path))
        except (OSError, json.JSONDecodeError):
            return self._recover_records_from_logs()
        rows = data.get("sessions", []) if isinstance(data, dict) else []
        records: list[TraceSessionRecord] = []
        for row in rows:
            try:
                records.append(_record_from_row(row))
            except (KeyError, TypeError, ValueError):
                continue
        if rows and not records:
            return self._recover_records_from_logs()
        return records

    def _recover_records_from_logs(self) -> list[TraceSessionRecord]:
        records = _recover_records_from_logs(self.path.parent)
        if records:
            try:
                self._write_records(records)
            except OSError:
                pass
        return records

    def _merge_recovered_records(self, records: list[TraceSessionRecord]) -> list[TraceSessionRecord]:
        recovered = _recover_records_from_logs(self.path.parent)
        if not recovered:
            return records
        existing_ids = {record.session_id for record in records}
        missing = [record for record in recovered if record.session_id not in existing_ids]
        if not missing:
            return records
        merged = [*records, *missing]
        try:
            self._write_records(merged)
        except OSError:
            pass
        return merged

    def _write_records(self, records: list[TraceSessionRecord]) -> None:
        # 임시 파일에 먼저 쓰고 replace로 교체합니다. 쓰는 도중 앱이 꺼져도
        # 기존 session_index.json이 반쯤 깨지는 상황을 줄이기 위한 방식입니다.
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


def _recover_records_from_logs(root: Path) -> list[TraceSessionRecord]:
    """session_index.json이 없거나 깨졌을 때 CSV 파일을 스캔해 세션 목록을 다시 만듭니다."""

    if not root.exists():
        return []
    recovered: list[TraceSessionRecord] = []
    for sample_path in sorted(root.rglob("*.samples.csv")):
        record = _record_from_sample_log(sample_path)
        if record is not None:
            recovered.append(record)
    return recovered


def _record_from_sample_log(sample_path: Path) -> TraceSessionRecord | None:
    try:
        segments = tuple(segment for segment in session_log_segment_index(sample_path) if segment.rows > 0)
    except OSError:
        return None
    if not segments:
        return None
    try:
        read_summary = session_log_read_summary(sample_path)
    except OSError:
        return None
    starts = [segment.start for segment in segments if segment.start is not None]
    ends = [segment.end for segment in segments if segment.end is not None]
    if not starts or not ends:
        return None
    target_addresses = _target_addresses_from_log(sample_path)
    target = target_addresses[0] if target_addresses else _target_from_sample_path(sample_path)
    route_path = route_log_path_for_session(sample_path)
    return TraceSessionRecord(
        session_id=_session_id(sample_path),
        target=target,
        sample_path=sample_path,
        route_path=route_path if route_path is not None and route_path.exists() else None,
        start=min(starts),
        end=max(ends),
        samples=sum(segment.rows for segment in segments),
        state=SESSION_STATE_ARCHIVED,
        target_count=max(1, len(target_addresses)),
        segments=tuple(segment.path for segment in segments),
        last_error=_session_recovery_last_error(read_summary),
    )


def _session_recovery_last_error(read_summary) -> str:
    if read_summary.skipped_rows > 0:
        files = ", ".join(path.name for path in read_summary.skipped_row_files[:3])
        suffix = f"; files={files}" if files else ""
        return f"{SESSION_RECOVERED_WITH_SKIPPED_ROWS_CODE}: skipped_rows={read_summary.skipped_rows}{suffix}"
    return f"{SESSION_INDEX_REBUILT_CODE}: recovered_rows={read_summary.rows}"


def _record_with_reconciled_log_metadata(record: TraceSessionRecord) -> TraceSessionRecord:
    if record.state == SESSION_STATE_WILL_DELETE or not record.sample_path.exists():
        return record
    recovered = _record_from_sample_log(record.sample_path)
    if recovered is None:
        return record
    route_path = record.route_path
    if route_path is None and recovered.route_path is not None:
        route_path = recovered.route_path
    last_error = record.last_error
    if recovered.last_error.startswith(SESSION_RECOVERED_WITH_SKIPPED_ROWS_CODE):
        last_error = recovered.last_error
    return _replace_record(
        record,
        target=record.target or recovered.target,
        route_path=route_path,
        start=min(record.start, recovered.start),
        end=recovered.end or record.end,
        samples=recovered.samples,
        target_count=max(record.target_count, recovered.target_count),
        segments=recovered.segments,
        last_error=last_error,
    )


def _target_addresses_from_log(sample_path: Path) -> list[str]:
    targets: list[str] = []
    seen: set[str] = set()
    try:
        for observation in iter_observations(sample_path):
            if observation.hop_index != 0 and not observation.is_target:
                continue
            if not observation.address or observation.address in seen:
                continue
            targets.append(observation.address)
            seen.add(observation.address)
    except OSError:
        return []
    return targets


def _target_from_sample_path(sample_path: Path) -> str:
    if _is_year_month_folder(sample_path.parent.name):
        return sample_path.parent.parent.name
    return sample_path.parent.name or "target"


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
        "probe_engine": record.probe_engine,
        "tcp_port": record.tcp_port,
        "route_probe_engine": record.route_probe_engine,
        "resumed_from_session_id": record.resumed_from_session_id,
        "target_count": record.target_count,
        "segments": [str(path) for path in record.segments],
        "last_error": record.last_error,
    }


def _record_from_row(row: dict[str, object]) -> TraceSessionRecord:
    end_value = str(row.get("end") or "")
    route_value = str(row.get("route_path") or "")
    measurement_mode = str(row.get("measurement_mode") or "")
    fallback_probe_engine, fallback_tcp_port, fallback_route_engine = _probe_fields_from_measurement_mode(
        measurement_mode
    )
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
        measurement_mode=measurement_mode,
        probe_engine=str(row.get("probe_engine") or fallback_probe_engine),
        tcp_port=_optional_int_or_default(row.get("tcp_port"), fallback_tcp_port),
        route_probe_engine=str(row.get("route_probe_engine") or fallback_route_engine),
        resumed_from_session_id=str(row.get("resumed_from_session_id") or ""),
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
        "probe_engine": record.probe_engine,
        "tcp_port": record.tcp_port,
        "route_probe_engine": record.route_probe_engine,
        "resumed_from_session_id": record.resumed_from_session_id,
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


def _optional_int_or_default(value: object, default: int | None) -> int | None:
    if value in (None, ""):
        return default
    return int(value)


def _probe_fields_from_measurement_mode(value: str) -> tuple[str, int | None, str]:
    parts = [part for part in value.split(":") if part]
    mode = parts[0] if parts else ""
    probe_engine = ""
    tcp_port: int | None = None
    for part in parts[1:]:
        if part in {"icmp", "tcp_connect"}:
            probe_engine = part
        elif part.startswith("port"):
            try:
                tcp_port = int(part.removeprefix("port"))
            except ValueError:
                tcp_port = None
    if not probe_engine and mode:
        probe_engine = "icmp"
    if mode == "final_hop_only":
        route_probe_engine = "disabled"
    elif mode == "full_route":
        route_probe_engine = "tracert/ICMP"
    else:
        route_probe_engine = ""
    return probe_engine, tcp_port, route_probe_engine


def _max_datetime(current: datetime | None, candidate: datetime) -> datetime:
    return candidate if current is None or candidate > current else current


def _session_last_seen(record: TraceSessionRecord) -> datetime:
    return record.end or record.start


def session_storage_summary(sessions: list[TraceSessionRecord]) -> SessionStorageSummary:
    targets = {session.target for session in sessions if session.target}
    return SessionStorageSummary(
        target_count=len(targets),
        bucket_count=len(session_storage_buckets(sessions)),
        segment_count=sum(len(_session_storage_segment_paths(session)) for session in sessions),
        sample_count=sum(max(session.samples, 0) for session in sessions),
    )


def session_storage_buckets(sessions: list[TraceSessionRecord]) -> list[SessionStorageBucket]:
    bucket_sessions: dict[tuple[str, str], dict[str, TraceSessionRecord]] = {}
    bucket_segments: dict[tuple[str, str], set[str]] = {}
    for session in sessions:
        target = session.target or "target"
        segments = _session_storage_segment_paths(session)
        months = _session_storage_months(session, segments)
        for month in months:
            key = (target, month)
            bucket_sessions.setdefault(key, {})[session.session_id] = session
            bucket_segments.setdefault(key, set())
        for path in segments:
            month = path.parent.name if _is_year_month_folder(path.parent.name) else session.start.strftime("%Y-%m")
            key = (target, month)
            bucket_sessions.setdefault(key, {})[session.session_id] = session
            bucket_segments.setdefault(key, set()).add(str(path))

    buckets: list[SessionStorageBucket] = []
    for (target, month), session_map in bucket_sessions.items():
        bucket_records = list(session_map.values())
        latest_seen = max(_session_last_seen(session) for session in bucket_records)
        buckets.append(
            SessionStorageBucket(
                target=target,
                month=month,
                session_count=len(session_map),
                segment_count=len(bucket_segments.get((target, month), set())),
                sample_count=sum(max(session.samples, 0) for session in bucket_records),
                state_counts=_session_state_counts(bucket_records),
                latest_seen=latest_seen,
            )
        )
    return sorted(buckets, key=lambda bucket: (bucket.latest_seen, bucket.target, bucket.month), reverse=True)


def _session_state_counts(sessions: list[TraceSessionRecord]) -> tuple[tuple[str, int], ...]:
    counts: dict[str, int] = {}
    for session in sessions:
        state = session.state or "Unknown"
        counts[state] = counts.get(state, 0) + 1
    order = {
        SESSION_STATE_ACTIVE: 0,
        SESSION_STATE_PAUSED: 1,
        SESSION_STATE_ARCHIVED: 2,
        SESSION_STATE_WILL_DELETE: 3,
    }
    return tuple(
        sorted(
            counts.items(),
            key=lambda item: (order.get(item[0], 99), item[0]),
        )
    )


def _session_storage_months(session: TraceSessionRecord, segments: tuple[Path, ...]) -> set[str]:
    months: set[str] = {
        path.parent.name
        for path in segments
        if _is_year_month_folder(path.parent.name)
    }
    if not months:
        months.add(session.start.strftime("%Y-%m"))
    return months


def _session_storage_segment_paths(session: TraceSessionRecord) -> tuple[Path, ...]:
    return _dedupe_paths((session.sample_path, *session.segments))


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
        session_log_segment_index_path(record.sample_path),
    ]
    if record.sample_path.parent.exists():
        paths.extend(sorted(record.sample_path.parent.glob(f"{record.sample_path.stem}.part*{record.sample_path.suffix}")))
    return _dedupe_paths(path for path in paths if path is not None)


def delete_session_files(record: TraceSessionRecord, *, root: Path) -> tuple[Path, ...]:
    deleted, _failures = _delete_session_files_with_failures(record, root=root)
    return deleted


def _delete_session_files_with_failures(
    record: TraceSessionRecord,
    *,
    root: Path,
) -> tuple[tuple[Path, ...], tuple[tuple[Path, OSError], ...]]:
    deleted: list[Path] = []
    failures: list[tuple[Path, OSError]] = []
    for path in session_data_paths(record):
        if not _is_managed_file(path, root):
            continue
        try:
            _unlink_path(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            failures.append((path, exc))
            continue
        deleted.append(path)
    return tuple(deleted), tuple(failures)


def _unlink_path(path: Path) -> None:
    path.unlink()


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
