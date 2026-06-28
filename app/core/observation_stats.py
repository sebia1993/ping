from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from math import sqrt

from app.core.models import STATUS_OK, STATUS_TIMEOUT, HopObservation, MetricSnapshot


@dataclass(frozen=True)
class FocusSnapshotSet:
    hop_snapshots: list[MetricSnapshot]
    target_snapshots: list[MetricSnapshot]
    target_snapshot: MetricSnapshot | None


@dataclass
class _SnapshotAccumulator:
    first: HopObservation
    sent: int = 0
    received: int = 0
    timeout_count: int = 0
    min_latency_ms: float | None = None
    max_latency_ms: float | None = None
    _latency_count: int = 0
    _latency_mean: float = 0.0
    _latency_m2: float = 0.0
    _last: HopObservation | None = None
    _last_order: int = -1

    def add(self, observation: HopObservation, order: int) -> None:
        self.sent += 1
        if observation.success:
            self.received += 1
        if observation.status == STATUS_TIMEOUT:
            self.timeout_count += 1
        if observation.success and observation.latency_ms is not None:
            latency = observation.latency_ms
            self.min_latency_ms = latency if self.min_latency_ms is None else min(self.min_latency_ms, latency)
            self.max_latency_ms = latency if self.max_latency_ms is None else max(self.max_latency_ms, latency)
            self._add_latency(latency)
        if self._last is None or (observation.timestamp, order) >= (self._last.timestamp, self._last_order):
            self._last = observation
            self._last_order = order

    def snapshot(self) -> MetricSnapshot:
        last = self._last or self.first
        failed = self.sent - self.received
        jitter = sqrt(self._latency_m2 / (self._latency_count - 1)) if self._latency_count >= 2 else None
        return MetricSnapshot(
            hop_index=self.first.hop_index,
            address=self.first.address,
            hostname=self.first.hostname,
            samples=self.sent,
            sent=self.sent,
            received=self.received,
            timeout_count=self.timeout_count,
            current_latency_ms=last.latency_ms if last.success else None,
            avg_latency_ms=self._latency_mean if self._latency_count else None,
            min_latency_ms=self.min_latency_ms,
            max_latency_ms=self.max_latency_ms,
            loss_percent=(failed / self.sent * 100) if self.sent else 0.0,
            recent_loss_percent=(failed / self.sent * 100) if self.sent else 0.0,
            jitter_ms=jitter,
            status=last.status or STATUS_OK,
            is_target=self.first.is_target,
        )

    def _add_latency(self, latency_ms: float) -> None:
        self._latency_count += 1
        delta = latency_ms - self._latency_mean
        self._latency_mean += delta / self._latency_count
        delta2 = latency_ms - self._latency_mean
        self._latency_m2 += delta * delta2


def observations_in_range(
    observations: Iterable[HopObservation],
    start: datetime,
    end: datetime,
) -> list[HopObservation]:
    if end < start:
        start, end = end, start
    return [observation for observation in observations if start <= observation.timestamp <= end]


def build_focus_snapshots(
    observations: Iterable[HopObservation],
    *,
    current_target: str = "",
) -> FocusSnapshotSet:
    grouped: dict[tuple[bool, int, str, str], _SnapshotAccumulator] = {}
    for order, observation in enumerate(observations):
        key = (
            observation.is_target,
            observation.hop_index,
            observation.address or "",
            observation.hostname or "",
        )
        if key not in grouped:
            grouped[key] = _SnapshotAccumulator(first=observation)
        grouped[key].add(observation, order)

    snapshots = [accumulator.snapshot() for accumulator in grouped.values()]
    hop_snapshots = sorted(
        [snapshot for snapshot in snapshots if snapshot.hop_index > 0],
        key=lambda item: (item.hop_index, item.address or ""),
    )
    target_snapshots = sorted(
        [snapshot for snapshot in snapshots if snapshot.hop_index == 0],
        key=lambda item: (item.address != current_target, item.address or "", item.hop_index),
    )
    target_snapshot = _select_target_snapshot(target_snapshots, current_target)
    return FocusSnapshotSet(hop_snapshots, target_snapshots, target_snapshot)


def _select_target_snapshot(
    target_snapshots: list[MetricSnapshot],
    current_target: str,
) -> MetricSnapshot | None:
    if not target_snapshots:
        return None
    if current_target:
        for snapshot in target_snapshots:
            if snapshot.address == current_target:
                return snapshot
    return target_snapshots[0]
