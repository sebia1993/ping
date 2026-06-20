from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from math import sqrt

from app.core.models import (
    STATUS_NO_PING_TARGET,
    STATUS_TIMEOUT,
    HopInfo,
    HopObservation,
    MetricSnapshot,
    PingResult,
)


@dataclass
class HopMetricTracker:
    hop: HopInfo
    recent_window: int = 20
    sent: int = 0
    received: int = 0
    timeout_count: int = 0
    current_latency_ms: float | None = None
    min_latency_ms: float | None = None
    max_latency_ms: float | None = None
    status: str = "WAITING"
    _latency_count: int = 0
    _latency_mean: float = 0.0
    _latency_m2: float = 0.0
    recent_successes: deque[bool] = field(default_factory=deque)

    def __post_init__(self) -> None:
        self.recent_successes = deque(maxlen=self.recent_window)

    def add_result(self, result: PingResult) -> HopObservation:
        self.sent += 1
        self.status = result.status
        self.recent_successes.append(result.success)
        if result.status == STATUS_TIMEOUT:
            self.timeout_count += 1

        if result.success:
            self.received += 1
            self.current_latency_ms = result.latency_ms
            if result.latency_ms is not None:
                self._add_latency(result.latency_ms)
                self.min_latency_ms = (
                    result.latency_ms
                    if self.min_latency_ms is None
                    else min(self.min_latency_ms, result.latency_ms)
                )
                self.max_latency_ms = (
                    result.latency_ms
                    if self.max_latency_ms is None
                    else max(self.max_latency_ms, result.latency_ms)
                )
        else:
            self.current_latency_ms = None

        return HopObservation(
            timestamp=result.timestamp,
            hop_index=self.hop.index,
            address=self.hop.address,
            hostname=self.hop.hostname,
            success=result.success,
            latency_ms=result.latency_ms,
            status=result.status,
            is_target=self.hop.is_target,
        )

    def snapshot(self) -> MetricSnapshot:
        sent = self.sent
        received = self.received
        loss_percent = ((sent - received) / sent * 100) if sent else 0.0
        recent_loss_percent = (
            ((len(self.recent_successes) - sum(self.recent_successes)) / len(self.recent_successes) * 100)
            if self.recent_successes
            else 0.0
        )

        jitter = sqrt(self._latency_m2 / (self._latency_count - 1)) if self._latency_count >= 2 else None
        status = self.status if sent else (STATUS_NO_PING_TARGET if self.hop.timed_out else "WAITING")

        return MetricSnapshot(
            hop_index=self.hop.index,
            address=self.hop.address,
            hostname=self.hop.hostname,
            samples=sent,
            sent=sent,
            received=received,
            timeout_count=self.timeout_count,
            current_latency_ms=self.current_latency_ms,
            avg_latency_ms=self._latency_mean if self._latency_count else None,
            min_latency_ms=self.min_latency_ms,
            max_latency_ms=self.max_latency_ms,
            loss_percent=loss_percent,
            recent_loss_percent=recent_loss_percent,
            jitter_ms=jitter,
            status=status,
            is_target=self.hop.is_target,
        )

    def _add_latency(self, latency_ms: float) -> None:
        self._latency_count += 1
        delta = latency_ms - self._latency_mean
        self._latency_mean += delta / self._latency_count
        delta2 = latency_ms - self._latency_mean
        self._latency_m2 += delta * delta2


class MetricsSession:
    def __init__(self, hops: list[HopInfo], recent_window: int = 20, recent_observation_limit: int = 300) -> None:
        self.trackers = {hop.index: HopMetricTracker(hop=hop, recent_window=recent_window) for hop in hops}
        self._recent_observations: deque[HopObservation] = deque(maxlen=recent_observation_limit)

    def add_result(self, hop_index: int, result: PingResult) -> HopObservation:
        observation = self.trackers[hop_index].add_result(result)
        self._recent_observations.append(observation)
        return observation

    @property
    def observations(self) -> list[HopObservation]:
        return list(self._recent_observations)

    def snapshots(self) -> list[MetricSnapshot]:
        return [tracker.snapshot() for _, tracker in sorted(self.trackers.items())]


class TargetMetricTracker:
    def __init__(self, target: str, recent_window: int = 20, recent_observation_limit: int = 300) -> None:
        self._hop = HopInfo(index=0, address=target, hostname="Target", is_target=True)
        self._tracker = HopMetricTracker(hop=self._hop, recent_window=recent_window)
        self._recent_observations: deque[HopObservation] = deque(maxlen=recent_observation_limit)

    def add_result(self, result: PingResult) -> HopObservation:
        observation = self._tracker.add_result(result)
        self._recent_observations.append(observation)
        return observation

    @property
    def observations(self) -> list[HopObservation]:
        return list(self._recent_observations)

    def snapshot(self) -> MetricSnapshot:
        return self._tracker.snapshot()
