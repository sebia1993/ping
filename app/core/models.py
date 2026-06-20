from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


STATUS_OK = "OK"
STATUS_TIMEOUT = "TIMEOUT"
STATUS_UNREACHABLE = "UNREACHABLE"
STATUS_ERROR = "ERROR"
STATUS_NO_PING_TARGET = "NO_PING_TARGET"
STATUS_PAUSED = "PAUSED"


@dataclass(frozen=True)
class HopInfo:
    index: int
    address: Optional[str]
    hostname: Optional[str] = None
    timed_out: bool = False
    raw_line: str = ""
    is_target: bool = False

    @property
    def ping_target(self) -> Optional[str]:
        if self.address:
            return self.address
        if self.hostname and not self.timed_out:
            return self.hostname
        return None

    @property
    def display_name(self) -> str:
        if self.hostname and self.address:
            return f"{self.hostname} ({self.address})"
        return self.address or self.hostname or "Timeout"


@dataclass(frozen=True)
class PingResult:
    target: str
    success: bool
    latency_ms: Optional[float]
    status: str
    timestamp: datetime


@dataclass(frozen=True)
class MetricSnapshot:
    hop_index: int
    address: Optional[str]
    hostname: Optional[str]
    samples: int
    sent: int
    received: int
    timeout_count: int
    current_latency_ms: Optional[float]
    avg_latency_ms: Optional[float]
    min_latency_ms: Optional[float]
    max_latency_ms: Optional[float]
    loss_percent: float
    recent_loss_percent: float
    jitter_ms: Optional[float]
    status: str
    is_target: bool = False


@dataclass(frozen=True)
class HopObservation:
    timestamp: datetime
    hop_index: int
    address: Optional[str]
    hostname: Optional[str]
    success: bool
    latency_ms: Optional[float]
    status: str
    is_target: bool = False
