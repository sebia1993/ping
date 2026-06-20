from __future__ import annotations

import threading
from typing import Callable, Protocol

from app.core.models import HopInfo, PingResult
from app.core.ping_runner import CommandPingRunner, TcpConnectRunner
from app.core.traceroute import run_traceroute


class PingProbe(Protocol):
    """Interface for one-shot reachability probes.

    Future ICMP raw socket, TCP connect, or HTTP checks should implement this
    interface so the UI and metrics layers do not depend on a specific method.
    """

    def ping(self, target: str) -> PingResult:
        ...


class TracerouteProbe(Protocol):
    """Interface for initial path discovery probes."""

    def trace(
        self,
        target: str,
        *,
        max_hops: int = 30,
        timeout_ms: int = 1000,
        stop_event: threading.Event | None = None,
    ) -> list[HopInfo]:
        ...


PingProbeFactory = Callable[[int], PingProbe]


def command_ping_probe_factory(timeout_ms: int) -> PingProbe:
    return CommandPingRunner(timeout_ms=timeout_ms)


def tcp_connect_probe_factory(timeout_ms: int, port: int = 443) -> PingProbe:
    return TcpConnectRunner(timeout_ms=timeout_ms, port=port)


class CommandTracerouteProbe:
    def trace(
        self,
        target: str,
        *,
        max_hops: int = 30,
        timeout_ms: int = 1000,
        stop_event: threading.Event | None = None,
    ) -> list[HopInfo]:
        return run_traceroute(target, max_hops=max_hops, timeout_ms=timeout_ms, stop_event=stop_event)
