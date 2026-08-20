from __future__ import annotations

import subprocess
import threading

import pytest

from app.core import traceroute as traceroute_module
from app.core.traceroute import (
    TRACEROUTE_TOTAL_TIMEOUT_CODE,
    ensure_target_hop,
    parse_tracert_output,
    run_traceroute,
    traceroute_total_timeout_seconds,
)


def test_parse_english_tracert_output() -> None:
    output = """
Tracing route to dns.google [8.8.8.8]
  1    <1 ms    <1 ms    <1 ms  router.local [192.168.0.1]
  2     5 ms     6 ms     5 ms  10.10.0.1
  3    20 ms    19 ms    20 ms  dns.google [8.8.8.8]
"""
    hops = parse_tracert_output(output)
    assert len(hops) == 3
    assert hops[0].index == 1
    assert hops[0].address == "192.168.0.1"
    assert hops[0].hostname == "router.local"
    assert hops[2].address == "8.8.8.8"


def test_parse_timeout_hop() -> None:
    output = """
  1     *        *        *     Request timed out.
  2    10 ms    11 ms    10 ms  192.0.2.1
"""
    hops = parse_tracert_output(output)
    assert hops[0].timed_out is True
    assert hops[0].address is None
    assert hops[1].address == "192.0.2.1"


def test_ensure_target_hop_adds_missing_target() -> None:
    hops = parse_tracert_output("  1    1 ms    1 ms    1 ms  192.168.0.1")
    ensured = ensure_target_hop(hops, "8.8.8.8", "8.8.8.8")
    assert ensured[-1].is_target is True
    assert ensured[-1].address == "8.8.8.8"


def test_ensure_target_hop_marks_existing_target() -> None:
    hops = parse_tracert_output(
        """
  1    <1 ms    <1 ms    <1 ms  router.local [192.168.0.1]
  2    20 ms    19 ms    20 ms  dns.google [8.8.8.8]
"""
    )

    ensured = ensure_target_hop(hops, "dns.google", "8.8.8.8")

    assert len(ensured) == 2
    assert ensured[-1].is_target is True
    assert ensured[-1].hostname == "dns.google"


def test_traceroute_total_timeout_has_a_finite_upper_bound() -> None:
    assert traceroute_total_timeout_seconds(30, 1000) == 95.0
    assert traceroute_total_timeout_seconds(255, 10_000) == 120.0


def test_run_traceroute_drains_output_with_communicate(monkeypatch) -> None:
    class CompletedProcess:
        def communicate(self, timeout=None):
            assert timeout is not None
            return ("  1    1 ms    1 ms    1 ms  192.0.2.1", "")

    monkeypatch.setattr(traceroute_module.subprocess, "Popen", lambda *_args, **_kwargs: CompletedProcess())

    hops = run_traceroute("198.51.100.10")

    assert [hop.address for hop in hops] == ["192.0.2.1"]


def test_run_traceroute_kills_and_reaps_process_after_total_timeout(monkeypatch) -> None:
    class NeverEndingProcess:
        def __init__(self) -> None:
            self.terminated = False
            self.killed = False
            self.communicate_calls = 0

        def communicate(self, timeout=None):
            self.communicate_calls += 1
            if self.killed:
                return ("", "")
            raise subprocess.TimeoutExpired("tracert", timeout)

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True

    process = NeverEndingProcess()
    monkeypatch.setattr(traceroute_module.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(traceroute_module, "traceroute_total_timeout_seconds", lambda *_args: 0.0)

    with pytest.raises(TimeoutError, match=TRACEROUTE_TOTAL_TIMEOUT_CODE):
        run_traceroute("198.51.100.10")

    assert process.terminated is True
    assert process.killed is True
    assert process.communicate_calls == 2


def test_run_traceroute_stops_and_reaps_process_when_cancelled(monkeypatch) -> None:
    class RunningProcess:
        def __init__(self) -> None:
            self.terminated = False

        def communicate(self, timeout=None):
            return ("", "")

        def terminate(self) -> None:
            self.terminated = True

    process = RunningProcess()
    stop_event = threading.Event()
    stop_event.set()
    monkeypatch.setattr(traceroute_module.subprocess, "Popen", lambda *_args, **_kwargs: process)

    assert run_traceroute("198.51.100.10", stop_event=stop_event) == []
    assert process.terminated is True
