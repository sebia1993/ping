from __future__ import annotations

import csv
import json
import tempfile
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.core.models import HopObservation
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
SEGMENT_INDEX_VERSION = 1
SEGMENT_INDEX_IO_RETRY_ATTEMPTS = 5
SEGMENT_INDEX_IO_RETRY_DELAY_SECONDS = 0.05


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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.count = 0
        self._open_segment(self.path)

    @classmethod
    def create(cls, target: str, root: Path | None = None) -> "SessionLogWriter":
        base_dir = session_log_directory(target, root=root)
        path = default_export_path(target, "samples.csv", base_dir)
        return cls(path, max_rows_per_file=200_000)

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
            self._handle.flush()
            self._write_segment_index()

    def close(self) -> None:
        self._handle.close()
        self._write_segment_index()

    def __enter__(self) -> "SessionLogWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _open_segment(self, path: Path) -> None:
        self._handle = path.open("w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._handle)
        self._writer.writerow(OBSERVATION_HEADERS)
        self._segment_count = 0
        self._segment_metadata.append(SessionLogSegment(path=path, start=None, end=None, rows=0))

    def _rotate_if_needed(self) -> None:
        if self.max_rows_per_file is None or self._segment_count < self.max_rows_per_file:
            return
        self._handle.close()
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
                }
                for segment in self._segment_metadata
            ],
        }
        _write_json_atomic(session_log_segment_index_path(self.path), payload)


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
    base_dir = root or Path.cwd() / "exports" / "session_logs"
    stamp = timestamp or datetime.now()
    return base_dir / safe_target_name(target) / stamp.strftime("%Y-%m")


def read_observations(path: Path | None) -> list[HopObservation]:
    if path is None:
        return []
    return list(iter_observations(path))


def session_log_read_summary(path: Path | None) -> SessionLogReadSummary:
    if path is None:
        return SessionLogReadSummary(rows=0, skipped_rows=0, skipped_row_files=())
    rows = 0
    skipped_rows = 0
    skipped_row_files: list[Path] = []
    seen_skipped_files: set[Path] = set()
    for segment_path in session_log_segments(path):
        with segment_path.open("r", newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
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
    return SessionLogReadSummary(
        rows=rows,
        skipped_rows=skipped_rows,
        skipped_row_files=tuple(skipped_row_files),
    )


def iter_observations(path: Path | None) -> Iterator[HopObservation]:
    if path is None:
        return
    for segment_path in session_log_segments(path):
        with segment_path.open("r", newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                try:
                    yield row_to_observation(row)
                except (KeyError, TypeError, ValueError):
                    continue


def iter_observations_in_range(
    path: Path | None,
    start: datetime,
    end: datetime,
) -> Iterator[HopObservation]:
    if path is None:
        return
    if end < start:
        start, end = end, start
    for segment in session_log_segment_index(path):
        if not segment.overlaps(start, end):
            continue
        for observation in iter_observations_from_segment(segment.path):
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


def iter_observations_from_segment(path: Path) -> Iterator[HopObservation]:
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            try:
                yield row_to_observation(row)
            except (KeyError, TypeError, ValueError):
                continue


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
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
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
    return indexed


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    # 장시간 측정 중 전원 종료나 파일 잠금이 생겨도 기존 segment index가 반쯤 깨지지 않게 교체합니다.
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        delete=False,
        dir=path.parent,
        encoding="utf-8",
        newline="",
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        temp_path = Path(handle.name)
    try:
        _replace_with_retries(temp_path, path)
    except OSError:
        temp_path.unlink(missing_ok=True)
        raise


def _replace_with_retries(source: Path, target: Path) -> None:
    _run_io_with_retries(lambda: _replace_path(source, target))


def _replace_path(source: Path, target: Path) -> Path:
    return source.replace(target)


def _run_io_with_retries(operation):
    last_error: OSError | None = None
    for attempt in range(SEGMENT_INDEX_IO_RETRY_ATTEMPTS):
        try:
            return operation()
        except PermissionError as exc:
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


def row_to_observation(row: dict[str, str]) -> HopObservation:
    latency_value = row.get("latency_ms", "")
    return HopObservation(
        timestamp=datetime.fromisoformat(row["timestamp"]),
        hop_index=int(row.get("hop") or 0),
        address=row.get("address") or None,
        hostname=row.get("hostname") or None,
        success=(row.get("success") == "True"),
        latency_ms=float(latency_value) if latency_value else None,
        status=row.get("status") or "",
        is_target=(row.get("kind") == "Target"),
    )
