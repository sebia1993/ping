from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from app.core.models import HopObservation
from app.storage.session_log import iter_observations_in_range
from app.utils.diagnostics import operation_failure


SESSION_GRAPH_LOAD_FAILED_CODE = "SESSION_GRAPH_LOAD_FAILED"
SESSION_GRAPH_MAX_POINTS_PER_TARGET = 1_200
SESSION_GRAPH_BUCKETS_PER_TARGET = SESSION_GRAPH_MAX_POINTS_PER_TARGET // 4


class SessionObservationLoader(QThread):
    loaded = Signal(int, object)
    failed = Signal(int, str)

    def __init__(
        self,
        *,
        request_id: int,
        path: Path,
        start: datetime,
        end: datetime,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.request_id = request_id
        self.path = path
        self.start_time = start
        self.end_time = end
        self._cancel_requested = threading.Event()

    def run(self) -> None:
        if self._is_cancel_requested():
            return
        accumulators: dict[str, _TargetPointAccumulator] = {}
        try:
            for observation in iter_observations_in_range(
                self.path,
                self.start_time,
                self.end_time,
            ):
                if self._is_cancel_requested():
                    return
                if not observation.address or not (observation.hop_index == 0 or observation.is_target):
                    continue
                accumulator = accumulators.setdefault(
                    observation.address,
                    _TargetPointAccumulator(self.start_time, self.end_time),
                )
                accumulator.add(observation)
        except OSError as exc:
            operation_failure(
                SESSION_GRAPH_LOAD_FAILED_CODE,
                "session_graph.read",
                exc,
                session_path=self.path,
            )
            self.failed.emit(
                self.request_id,
                f"{SESSION_GRAPH_LOAD_FAILED_CODE}: {type(exc).__name__}",
            )
            return
        except Exception as exc:
            operation_failure(
                SESSION_GRAPH_LOAD_FAILED_CODE,
                "session_graph.read",
                exc,
                session_path=self.path,
            )
            self.failed.emit(
                self.request_id,
                f"{SESSION_GRAPH_LOAD_FAILED_CODE}: {type(exc).__name__}",
            )
            return
        if not self._is_cancel_requested():
            observations = [
                observation
                for accumulator in accumulators.values()
                for observation in accumulator.points()
            ]
            observations.sort(key=lambda observation: (observation.timestamp, observation.address))
            self.loaded.emit(self.request_id, observations)

    def request_cancel(self) -> None:
        self._cancel_requested.set()
        self.requestInterruption()

    def _is_cancel_requested(self) -> bool:
        return self._cancel_requested.is_set() or self.isInterruptionRequested()


@dataclass
class _TimelineBucket:
    first: HopObservation
    last: HopObservation
    failure: HopObservation | None = None
    max_latency: HopObservation | None = None

    def add(self, observation: HopObservation) -> None:
        self.last = observation
        if not observation.success and self.failure is None:
            self.failure = observation
        if observation.latency_ms is not None and (
            self.max_latency is None
            or self.max_latency.latency_ms is None
            or observation.latency_ms > self.max_latency.latency_ms
        ):
            self.max_latency = observation

    def points(self) -> list[HopObservation]:
        unique = {
            point
            for point in (self.first, self.max_latency, self.failure, self.last)
            if point is not None
        }
        return sorted(unique, key=lambda point: point.timestamp)


class _TargetPointAccumulator:
    def __init__(self, start: datetime, end: datetime) -> None:
        self.start = start
        self.duration_seconds = max((end - start).total_seconds(), 1.0)
        self.total = 0
        self.raw: list[HopObservation] = []
        self.buckets: dict[int, _TimelineBucket] = {}

    def add(self, observation: HopObservation) -> None:
        self.total += 1
        if len(self.raw) < SESSION_GRAPH_MAX_POINTS_PER_TARGET:
            self.raw.append(observation)
        elapsed = max((observation.timestamp - self.start).total_seconds(), 0.0)
        ratio = min(elapsed / self.duration_seconds, 1.0)
        bucket_index = min(
            int(ratio * SESSION_GRAPH_BUCKETS_PER_TARGET),
            SESSION_GRAPH_BUCKETS_PER_TARGET - 1,
        )
        bucket = self.buckets.get(bucket_index)
        if bucket is None:
            self.buckets[bucket_index] = _TimelineBucket(
                first=observation,
                last=observation,
                failure=observation if not observation.success else None,
                max_latency=observation if observation.latency_ms is not None else None,
            )
        else:
            bucket.add(observation)

    def points(self) -> list[HopObservation]:
        if self.total <= SESSION_GRAPH_MAX_POINTS_PER_TARGET:
            return self.raw
        return [
            point
            for bucket_index in sorted(self.buckets)
            for point in self.buckets[bucket_index].points()
        ]
