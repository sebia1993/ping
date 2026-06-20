from __future__ import annotations

from collections import defaultdict
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
    grouped: dict[tuple[bool, int, str, str], list[HopObservation]] = defaultdict(list)
    for observation in sorted(observations, key=lambda item: item.timestamp):
        key = (
            observation.is_target,
            observation.hop_index,
            observation.address or "",
            observation.hostname or "",
        )
        grouped[key].append(observation)

    snapshots = [_snapshot_from_group(points) for points in grouped.values()]
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


def _snapshot_from_group(points: list[HopObservation]) -> MetricSnapshot:
    first = points[0]
    last = points[-1]
    sent = len(points)
    received = sum(1 for point in points if point.success)
    timeout_count = sum(1 for point in points if point.status == STATUS_TIMEOUT)
    latencies = [point.latency_ms for point in points if point.success and point.latency_ms is not None]
    avg_latency = sum(latencies) / len(latencies) if latencies else None
    jitter = _sample_stdev(latencies)
    loss_percent = ((sent - received) / sent * 100) if sent else 0.0

    return MetricSnapshot(
        hop_index=first.hop_index,
        address=first.address,
        hostname=first.hostname,
        samples=sent,
        sent=sent,
        received=received,
        timeout_count=timeout_count,
        current_latency_ms=last.latency_ms if last.success else None,
        avg_latency_ms=avg_latency,
        min_latency_ms=min(latencies) if latencies else None,
        max_latency_ms=max(latencies) if latencies else None,
        loss_percent=loss_percent,
        recent_loss_percent=loss_percent,
        jitter_ms=jitter,
        status=last.status or STATUS_OK,
        is_target=first.is_target,
    )


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


def _sample_stdev(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return sqrt(variance)
