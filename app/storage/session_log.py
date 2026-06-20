from __future__ import annotations

import csv
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


class SessionLogWriter:
    def __init__(self, path: Path, *, max_rows_per_file: int | None = None) -> None:
        self.path = path
        self.paths = [path]
        self.max_rows_per_file = max_rows_per_file
        self._segment_index = 0
        self._segment_count = 0
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
            wrote = True
        if wrote:
            self._handle.flush()

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> "SessionLogWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _open_segment(self, path: Path) -> None:
        self._handle = path.open("w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._handle)
        self._writer.writerow(OBSERVATION_HEADERS)
        self._segment_count = 0

    def _rotate_if_needed(self) -> None:
        if self.max_rows_per_file is None or self._segment_count < self.max_rows_per_file:
            return
        self._handle.close()
        self._segment_index += 1
        rotated_path = self.path.with_name(f"{self.path.stem}.part{self._segment_index:03d}{self.path.suffix}")
        self.paths.append(rotated_path)
        self._open_segment(rotated_path)


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
