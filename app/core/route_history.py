from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.core.models import HopInfo


@dataclass(frozen=True)
class RouteSnapshot:
    timestamp: datetime
    hops: tuple[HopInfo, ...]
    signature: tuple[str, ...]
    labels: tuple[str, ...]


@dataclass(frozen=True)
class RouteChange:
    timestamp: datetime
    previous: RouteSnapshot
    current: RouteSnapshot
    changed_hops: tuple[int, ...]
    added_hops: tuple[int, ...]
    removed_hops: tuple[int, ...]
    summary: str


@dataclass
class RouteHistory:
    snapshots: list[RouteSnapshot] = field(default_factory=list)
    changes: list[RouteChange] = field(default_factory=list)

    def record(self, hops: list[HopInfo], timestamp: datetime | None = None) -> RouteChange | None:
        snapshot = route_snapshot(hops, timestamp or datetime.now())
        previous = self.snapshots[-1] if self.snapshots else None
        self.snapshots.append(snapshot)
        if previous is None or previous.signature == snapshot.signature:
            return None

        change = route_change(previous, snapshot)
        self.changes.append(change)
        return change


def route_snapshot(hops: list[HopInfo], timestamp: datetime) -> RouteSnapshot:
    return RouteSnapshot(
        timestamp=timestamp,
        hops=tuple(hops),
        signature=tuple(_hop_signature(hop) for hop in hops),
        labels=tuple(_hop_label(hop) for hop in hops),
    )


def route_change(previous: RouteSnapshot, current: RouteSnapshot) -> RouteChange:
    previous_by_index = {_index_from_signature(value): value for value in previous.signature}
    current_by_index = {_index_from_signature(value): value for value in current.signature}
    previous_indexes = set(previous_by_index)
    current_indexes = set(current_by_index)
    changed_hops = tuple(
        sorted(index for index in previous_indexes & current_indexes if previous_by_index[index] != current_by_index[index])
    )
    added_hops = tuple(sorted(current_indexes - previous_indexes))
    removed_hops = tuple(sorted(previous_indexes - current_indexes))
    parts: list[str] = []
    if changed_hops:
        parts.append("changed " + ", ".join(f"Hop {index}" for index in changed_hops[:6]))
    if added_hops:
        parts.append("added " + ", ".join(f"Hop {index}" for index in added_hops[:6]))
    if removed_hops:
        parts.append("removed " + ", ".join(f"Hop {index}" for index in removed_hops[:6]))
    summary = "; ".join(parts) if parts else "route changed"
    return RouteChange(
        timestamp=current.timestamp,
        previous=previous,
        current=current,
        changed_hops=changed_hops,
        added_hops=added_hops,
        removed_hops=removed_hops,
        summary=summary,
    )


def route_path(snapshot: RouteSnapshot, limit: int = 8) -> str:
    labels = list(snapshot.labels)
    if len(labels) > limit:
        labels = [*labels[:limit], f"... +{len(snapshot.labels) - limit} more"]
    return " > ".join(labels)


def _hop_signature(hop: HopInfo) -> str:
    node = hop.address or hop.hostname or "Timeout"
    timeout = "timeout" if hop.timed_out else "reply"
    target = "target" if hop.is_target else "hop"
    return f"{hop.index}|{node}|{timeout}|{target}"


def _hop_label(hop: HopInfo) -> str:
    node = hop.address or hop.hostname or "Timeout"
    return f"H{hop.index}:{node}"


def _index_from_signature(value: str) -> int:
    return int(value.split("|", 1)[0])
