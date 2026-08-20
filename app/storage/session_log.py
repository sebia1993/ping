from __future__ import annotations

import csv
import json
import math
import tempfile
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.core.models import HopObservation
from app.utils.app_paths import session_logs_directory
from app.utils.diagnostics import operation_failure
from app.utils.filename import default_export_path, safe_target_name


OBSERVATION_HEADERS = [
    "timestamp",
    "address",
    "kind",
    "hop",
    "hostname",
    "success",
    "latency_ms",
    "status",
]
SEGMENT_INDEX_VERSION = 2
SEGMENT_INDEX_IO_RETRY_ATTEMPTS = 5
SEGMENT_INDEX_IO_RETRY_DELAY_SECONDS = 0.05
SESSION_LOG_CREATE_ATTEMPTS = 100
SESSION_LOG_CORRUPTED_CODE = "SESSION_LOG_CORRUPTED"
SESSION_SEGMENT_INDEX_WRITE_FAILED_CODE = "SESSION_SEGMENT_INDEX_WRITE_FAILED"


class SessionLogCorruptionError(RuntimeError):
    def __init__(self, skipped_rows: int, skipped_row_files: tuple[Path, ...]) -> None:
        self.skipped_rows = skipped_rows
        self.skipped_row_files = skipped_row_files
        super().__init__(
            f"{SESSION_LOG_CORRUPTED_CODE}: 읽을 수 없는 세션 행 {skipped_rows}개 "
            f"(파일 {len(skipped_row_files)}개)"
        )


@dataclass(frozen=True)
class SessionLogSegment:
    path: Path
    start: datetime | None
    end: datetime | None
    rows: int

    def overlaps(self, start: datetime, end: datetime) -> bool:
        if self.start is None or self.end is None:
            return True
        return self.start <= end and self.end >= start


@dataclass(frozen=True)
class SessionLogReadSummary:
    rows: int
    skipped_rows: int
    skipped_row_files: tuple[Path, ...]


class SessionLogWriter:
    def __init__(self, path: Path, *, max_rows_per_file: int | None = None) -> None:
        self.path = path
        self.paths = [path]
        self.max_rows_per_file = max_rows_per_file
        self._segment_index = 0
        self._segment_count = 0
        self._segment_metadata: list[SessionLogSegment] = []
        self._segment_index_error: OSError | None = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.count = 0
        self._open_segment(self.path)

    @classmethod
    def create(cls, target: str, root: Path | None = None) -> "SessionLogWriter":
        base_dir = session_log_directory(target, root=root)
        for _attempt in range(SESSION_LOG_CREATE_ATTEMPTS):
            path = default_export_path(target, "samples.csv", base_dir)
            try:
                return cls(path, max_rows_per_file=200_000)
            except FileExistsError:
                # exists 확인 직후 다른 실행 흐름이 같은 파일을 만들 수 있습니다.
                # exclusive create가 충돌을 발견하면 새 이름을 계산해 다시 시도합니다.
                continue
        raise FileExistsError("고유한 세션 로그 파일을 만들 수 없습니다.")

    def write_many(self, observations: Iterable[HopObservation]) -> None:
        wrote = False
        for observation in observations:
            self._rotate_if_needed()
            self._writer.writerow(observation_to_row(observation))
            self.count += 1
            self._segment_count += 1
            self._record_segment_observation(observation)
            wrote = True
        if wrote:
            _flush_with_retries(self._handle)
            self._try_write_segment_index()

    @property
    def segment_index_error(self) -> OSError | None:
        return self._segment_index_error

    def close(self) -> None:
        if self._handle.closed:
            return
        try:
            _flush_with_retries(self._handle)
            self._try_write_segment_index()
        finally:
            _close_handle_suppressing_errors(self._handle)

    def __enter__(self) -> "SessionLogWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _open_segment(self, path: Path) -> None:
        self._handle = _open_csv_write_handle(path)
        self._writer = csv.writer(self._handle)
        self._writer.writerow(OBSERVATION_HEADERS)
        self._segment_count = 0
        self._segment_metadata.append(SessionLogSegment(path=path, start=None, end=None, rows=0))

    def _rotate_if_needed(self) -> None:
        if self.max_rows_per_file is None or self._segment_count < self.max_rows_per_file:
            return
        _flush_with_retries(self._handle)
        _close_handle_suppressing_errors(self._handle)
        self._segment_index += 1
        rotated_path = self.path.with_name(f"{self.path.stem}.part{self._segment_index:03d}{self.path.suffix}")
        self.paths.append(rotated_path)
        self._open_segment(rotated_path)

    def _record_segment_observation(self, observation: HopObservation) -> None:
        current = self._segment_metadata[-1]
        start = observation.timestamp if current.start is None else min(current.start, observation.timestamp)
        end = observation.timestamp if current.end is None else max(current.end, observation.timestamp)
        self._segment_metadata[-1] = SessionLogSegment(
            path=current.path,
            start=start,
            end=end,
            rows=current.rows + 1,
        )

    def _write_segment_index(self) -> None:
        payload = {
            "version": SEGMENT_INDEX_VERSION,
            "base": self.path.name,
            "segments": [
                {
                    "path": segment.path.name,
                    "start": segment.start.isoformat(timespec="seconds") if segment.start else "",
                    "end": segment.end.isoformat(timespec="seconds") if segment.end else "",
                    "rows": segment.rows,
                    **_segment_file_state(segment.path),
                }
                for segment in self._segment_metadata
            ],
        }
        _write_json_atomic(session_log_segment_index_path(self.path), payload)

    def _try_write_segment_index(self) -> bool:
        """Persist the rebuildable segment cache without failing primary CSV writes."""

        try:
            self._write_segment_index()
        except OSError as exc:
            if self._segment_index_error is None:
                operation_failure(
                    SESSION_SEGMENT_INDEX_WRITE_FAILED_CODE,
                    "session_log.segment_index",
                    exc,
                    session_path=self.path,
                )
            self._segment_index_error = exc
            return False
        self._segment_index_error = None
        return True


def observation_to_row(observation: HopObservation) -> list[object]:
    return [
        observation.timestamp.isoformat(timespec="seconds"),
        observation.address or "",
        "Target" if observation.is_target else "Hop",
        observation.hop_index,
        observation.hostname or "",
        str(observation.success),
        "" if observation.latency_ms is None else f"{observation.latency_ms:.3f}",
        observation.status,
    ]


def session_log_directory(
    target: str,
    *,
    root: Path | None = None,
    timestamp: datetime | None = None,
) -> Path:
    base_dir = root or session_logs_directory()
    stamp = timestamp or datetime.now()
    return base_dir / safe_target_name(target) / stamp.strftime("%Y-%m")


def read_observations(path: Path | None) -> list[HopObservation]:
    if path is None:
        return []
    return list(iter_observations(path))


def read_observations_with_summary(
    path: Path | None,
    *,
    retained_limit: int | None = None,
    on_observation: Callable[[HopObservation], None] | None = None,
) -> tuple[list[HopObservation], SessionLogReadSummary]:
    if path is None:
        return [], SessionLogReadSummary(rows=0, skipped_rows=0, skipped_row_files=())
    observations: list[HopObservation] | deque[HopObservation]
    if retained_limit is None:
        observations = []
    else:
        observations = deque(maxlen=max(int(retained_limit), 0))
    rows = 0
    skipped_rows = 0
    skipped_row_files: list[Path] = []
    for segment_path in session_log_segments(path):
        segment_skipped = False
        try:
            with _open_csv_read_handle(segment_path) as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames != OBSERVATION_HEADERS:
                    skipped_rows += 1
                    skipped_row_files.append(segment_path)
                    continue
                for row in reader:
                    try:
                        observation = row_to_observation(row)
                    except (KeyError, TypeError, ValueError):
                        skipped_rows += 1
                        segment_skipped = True
                        continue
                    rows += 1
                    if on_observation is not None:
                        on_observation(observation)
                    observations.append(observation)
        except (UnicodeError, csv.Error):
            # decode/parser 오류 뒤의 행 경계를 신뢰할 수 없으므로 해당 segment의
            # 나머지를 버리고 손상 파일 한 건으로 기록합니다.
            skipped_rows += 1
            segment_skipped = True
        if segment_skipped:
            skipped_row_files.append(segment_path)
    return list(observations), SessionLogReadSummary(
        rows=rows,
        skipped_rows=skipped_rows,
        skipped_row_files=tuple(skipped_row_files),
    )


def session_log_read_summary(path: Path | None) -> SessionLogReadSummary:
    if path is None:
        return SessionLogReadSummary(rows=0, skipped_rows=0, skipped_row_files=())
    rows = 0
    skipped_rows = 0
    skipped_row_files: list[Path] = []
    seen_skipped_files: set[Path] = set()
    for segment_path in session_log_segments(path):
        try:
            with _open_csv_read_handle(segment_path) as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames != OBSERVATION_HEADERS:
                    skipped_rows += 1
                    skipped_row_files.append(segment_path)
                    seen_skipped_files.add(segment_path)
                    continue
                for row in reader:
                    try:
                        row_to_observation(row)
                    except (KeyError, TypeError, ValueError):
                        skipped_rows += 1
                        if segment_path not in seen_skipped_files:
                            skipped_row_files.append(segment_path)
                            seen_skipped_files.add(segment_path)
                        continue
                    rows += 1
        except (UnicodeError, csv.Error):
            skipped_rows += 1
            if segment_path not in seen_skipped_files:
                skipped_row_files.append(segment_path)
                seen_skipped_files.add(segment_path)
    return SessionLogReadSummary(
        rows=rows,
        skipped_rows=skipped_rows,
        skipped_row_files=tuple(skipped_row_files),
    )


def iter_observations(path: Path | None, *, strict: bool = False) -> Iterator[HopObservation]:
    if path is None:
        return
    skipped_rows = 0
    skipped_row_files: list[Path] = []
    for segment_path in session_log_segments(path):
        segment_skipped = False
        try:
            with _open_csv_read_handle(segment_path) as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames != OBSERVATION_HEADERS:
                    skipped_rows += 1
                    segment_skipped = True
                    continue
                for row in reader:
                    try:
                        yield row_to_observation(row)
                    except (KeyError, TypeError, ValueError):
                        skipped_rows += 1
                        segment_skipped = True
                        continue
        except (UnicodeError, csv.Error):
            skipped_rows += 1
            segment_skipped = True
        if segment_skipped:
            skipped_row_files.append(segment_path)
    if strict and skipped_rows:
        raise SessionLogCorruptionError(skipped_rows, tuple(skipped_row_files))


def iter_observations_in_range(
    path: Path | None,
    start: datetime,
    end: datetime,
    *,
    strict: bool = False,
) -> Iterator[HopObservation]:
    if path is None:
        return
    if end < start:
        start, end = end, start
    for segment in session_log_segment_index(path):
        if not segment.overlaps(start, end):
            continue
        for observation in iter_observations_from_segment(segment.path, strict=strict):
            if start <= observation.timestamp <= end:
                yield observation


def session_log_segment_index(path: Path | None) -> list[SessionLogSegment]:
    if path is None:
        return []
    indexed = _read_segment_index_file(path)
    if indexed is not None:
        return indexed
    return [_index_segment(segment_path) for segment_path in session_log_segments(path)]


def session_log_bounds(path: Path | None) -> tuple[datetime, datetime] | None:
    segments = [segment for segment in session_log_segment_index(path) if segment.start and segment.end]
    if not segments:
        return None
    return min(segment.start for segment in segments if segment.start), max(segment.end for segment in segments if segment.end)


def iter_observations_from_segment(path: Path, *, strict: bool = False) -> Iterator[HopObservation]:
    skipped_rows = 0
    try:
        with _open_csv_read_handle(path) as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != OBSERVATION_HEADERS:
                skipped_rows += 1
            else:
                for row in reader:
                    try:
                        yield row_to_observation(row)
                    except (KeyError, TypeError, ValueError):
                        skipped_rows += 1
    except (UnicodeError, csv.Error):
        skipped_rows += 1
    if strict and skipped_rows:
        raise SessionLogCorruptionError(skipped_rows, (path,))


def _index_segment(path: Path) -> SessionLogSegment:
    start: datetime | None = None
    end: datetime | None = None
    rows = 0
    for observation in iter_observations_from_segment(path):
        rows += 1
        if start is None or observation.timestamp < start:
            start = observation.timestamp
        if end is None or observation.timestamp > end:
            end = observation.timestamp
    return SessionLogSegment(path=path, start=start, end=end, rows=rows)


def session_log_segments(path: Path) -> list[Path]:
    if not path.exists():
        return []
    segments = [path]
    segments.extend(sorted(path.parent.glob(f"{path.stem}.part*{path.suffix}")))
    return segments


def session_log_segment_index_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}.segments.json")


def _read_segment_index_file(path: Path) -> list[SessionLogSegment] | None:
    index_path = session_log_segment_index_path(path)
    if not index_path.exists():
        return None
    try:
        payload = json.loads(_read_text_with_retries(index_path))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != SEGMENT_INDEX_VERSION:
        return None
    rows = payload.get("segments")
    if not isinstance(rows, list):
        return None
    try:
        indexed = [_segment_from_index_row(path.parent, row) for row in rows]
    except (KeyError, TypeError, ValueError):
        return None
    current_paths = session_log_segments(path)
    if [segment.path for segment in indexed] != current_paths:
        return None
    if not _segment_file_states_match(path.parent, rows):
        return None
    return indexed


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    # 장시간 측정 중 전원 종료나 파일 잠금이 생겨도 기존 segment index가 반쯤 깨지지 않게 교체합니다.
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            delete=False,
            dir=path.parent,
            encoding="utf-8",
            newline="",
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        _replace_with_retries(temp_path, path)
    except Exception:
        if temp_path is not None:
            _unlink_temp_path(temp_path)
        raise


def _replace_with_retries(source: Path, target: Path) -> None:
    _run_io_with_retries(lambda: _replace_path(source, target))


def _replace_path(source: Path, target: Path) -> Path:
    return source.replace(target)


def _unlink_temp_path(path: Path) -> None:
    try:
        _unlink_path(path)
    except OSError:
        pass


def _unlink_path(path: Path) -> None:
    path.unlink()


def _read_text_with_retries(path: Path) -> str:
    return _run_io_with_retries(lambda: _read_text_path(path))


def _read_text_path(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _open_csv_read_handle(path: Path):
    return _run_io_with_retries(lambda: _open_csv_read_path(path))


def _open_csv_read_path(path: Path):
    return path.open("r", newline="", encoding="utf-8")


def _open_csv_write_handle(path: Path):
    return _run_io_with_retries(lambda: _open_csv_write_path(path))


def _open_csv_write_path(path: Path):
    # 기존 세션을 실수로 truncate하지 않도록 파일이 이미 있으면 반드시 실패합니다.
    return path.open("x", newline="", encoding="utf-8")


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
    for attempt in range(SEGMENT_INDEX_IO_RETRY_ATTEMPTS):
        try:
            return operation()
        except FileExistsError:
            raise
        except OSError as exc:
            last_error = exc
            if attempt == SEGMENT_INDEX_IO_RETRY_ATTEMPTS - 1:
                break
            time.sleep(SEGMENT_INDEX_IO_RETRY_DELAY_SECONDS)
    if last_error is not None:
        raise last_error
    return operation()


def _segment_from_index_row(root: Path, row: object) -> SessionLogSegment:
    if not isinstance(row, dict):
        raise TypeError("segment row must be a dict")
    segment_path = root / str(row["path"])
    start_value = str(row.get("start") or "")
    end_value = str(row.get("end") or "")
    return SessionLogSegment(
        path=segment_path,
        start=datetime.fromisoformat(start_value) if start_value else None,
        end=datetime.fromisoformat(end_value) if end_value else None,
        rows=int(row.get("rows") or 0),
    )


def _segment_file_state(path: Path) -> dict[str, int]:
    stat_result = path.stat()
    return {"size": stat_result.st_size, "mtime_ns": stat_result.st_mtime_ns}


def _segment_file_states_match(root: Path, rows: list[object]) -> bool:
    for row in rows:
        if not isinstance(row, dict):
            return False
        try:
            path = root / str(row["path"])
            state = _segment_file_state(path)
            if int(row["size"]) != state["size"] or int(row["mtime_ns"]) != state["mtime_ns"]:
                return False
        except (KeyError, TypeError, ValueError, OSError):
            return False
    return True


def row_to_observation(row: dict[str, str]) -> HopObservation:
    if None in row:
        raise ValueError("unexpected session log columns")
    kind = row.get("kind")
    if kind not in {"Target", "Hop"}:
        raise ValueError("invalid observation kind")
    success_value = row.get("success")
    if success_value not in {"True", "False"}:
        raise ValueError("invalid observation success value")
    status = row.get("status") or ""
    if not status:
        raise ValueError("missing observation status")
    hop_index = int(row.get("hop") or 0)
    if hop_index < 0:
        raise ValueError("negative hop index")
    latency_value = row.get("latency_ms", "")
    latency_ms = float(latency_value) if latency_value else None
    if latency_ms is not None and (not math.isfinite(latency_ms) or latency_ms < 0):
        raise ValueError("invalid observation latency")
    if success_value == "True" and latency_ms is None:
        raise ValueError("successful observation is missing latency")
    return HopObservation(
        timestamp=datetime.fromisoformat(row["timestamp"]),
        hop_index=hop_index,
        address=row.get("address") or None,
        hostname=row.get("hostname") or None,
        success=(success_value == "True"),
        latency_ms=latency_ms,
        status=status,
        is_target=(kind == "Target"),
    )
