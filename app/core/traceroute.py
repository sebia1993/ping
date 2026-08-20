from __future__ import annotations

import locale
import platform
import re
import subprocess
import threading
import time
from dataclasses import replace
from typing import Iterable

from app.core.models import HopInfo


HOP_LINE_RE = re.compile(r"^\s*(?P<index>\d+)\s+(?P<body>.+?)\s*$")
IPV4_RE = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")
BRACKET_ADDR_RE = re.compile(r"\[(?P<address>[0-9A-Fa-f:.]+)\]")
TIMEOUT_TEXT_RE = re.compile(r"(request timed out|요청 시간이 만료|시간이 초과|timeout)", re.IGNORECASE)
TRACEROUTE_PROBES_PER_HOP = 3
TRACEROUTE_TOTAL_TIMEOUT_GRACE_SECONDS = 5.0
TRACEROUTE_MAX_TOTAL_TIMEOUT_SECONDS = 120.0
TRACEROUTE_PROCESS_STOP_TIMEOUT_SECONDS = 1.0
TRACEROUTE_TOTAL_TIMEOUT_CODE = "TRACEROUTE_TOTAL_TIMEOUT"
TRACEROUTE_PROCESS_STOP_FAILED_CODE = "TRACEROUTE_PROCESS_STOP_FAILED"


def build_traceroute_command(target: str, max_hops: int = 30, timeout_ms: int = 1000) -> list[str]:
    if platform.system().lower() == "windows":
        return ["tracert", "-h", str(max_hops), "-w", str(timeout_ms), target]
    timeout_seconds = max(1, int(round(timeout_ms / 1000)))
    return ["traceroute", "-m", str(max_hops), "-w", str(timeout_seconds), target]


def windows_no_window_flag() -> int:
    if platform.system().lower() == "windows":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def traceroute_total_timeout_seconds(max_hops: int, timeout_ms: int) -> float:
    """hop별 대기 시간을 합산하되 비정상 명령이 무한 실행되지 않게 상한을 둡니다."""

    estimated = (
        max(int(max_hops), 1)
        * TRACEROUTE_PROBES_PER_HOP
        * max(int(timeout_ms), 1)
        / 1000
        + TRACEROUTE_TOTAL_TIMEOUT_GRACE_SECONDS
    )
    return min(max(estimated, TRACEROUTE_TOTAL_TIMEOUT_GRACE_SECONDS), TRACEROUTE_MAX_TOTAL_TIMEOUT_SECONDS)


def _hostname_before_bracket(body: str, bracket_start: int) -> str | None:
    before = body[:bracket_start].strip()
    if not before:
        return None
    parts = [part.strip() for part in re.split(r"\s{2,}", before) if part.strip()]
    candidate = parts[-1] if parts else before
    if candidate == "*" or candidate.lower().endswith("ms"):
        return None
    return candidate or None


def parse_tracert_output(output: str) -> list[HopInfo]:
    hops: list[HopInfo] = []
    for raw_line in output.splitlines():
        match = HOP_LINE_RE.match(raw_line)
        if not match:
            continue

        index = int(match.group("index"))
        body = match.group("body")
        address = None
        hostname = None

        bracket_match = BRACKET_ADDR_RE.search(body)
        if bracket_match:
            address = bracket_match.group("address")
            hostname = _hostname_before_bracket(body, bracket_match.start())
        else:
            ipv4_matches = list(IPV4_RE.finditer(body))
            if ipv4_matches:
                address = ipv4_matches[-1].group(0)

        timed_out = address is None and ("*" in body or TIMEOUT_TEXT_RE.search(body) is not None)
        hops.append(HopInfo(index=index, address=address, hostname=hostname, timed_out=timed_out, raw_line=raw_line))
    return hops


def run_traceroute(
    target: str,
    max_hops: int = 30,
    timeout_ms: int = 1000,
    stop_event: threading.Event | None = None,
) -> list[HopInfo]:
    command = build_traceroute_command(target, max_hops=max_hops, timeout_ms=timeout_ms)
    encoding = locale.getpreferredencoding(False)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding=encoding,
        errors="replace",
        creationflags=windows_no_window_flag(),
    )

    deadline = time.monotonic() + traceroute_total_timeout_seconds(max_hops, timeout_ms)
    while True:
        if stop_event and stop_event.is_set():
            _terminate_and_collect(process)
            return []
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate_and_collect(process)
            raise TimeoutError(f"{TRACEROUTE_TOTAL_TIMEOUT_CODE}: target={target}")
        try:
            # communicate(timeout)는 child가 실행되는 동안에도 PIPE를 비워 과다 출력 교착을 막습니다.
            stdout, stderr = process.communicate(timeout=min(0.1, remaining))
        except subprocess.TimeoutExpired:
            continue
        return parse_tracert_output("\n".join(part for part in (stdout, stderr) if part))


def _terminate_and_collect(process: subprocess.Popen) -> tuple[str, str]:
    """terminate/kill 뒤 communicate까지 완료해 child process handle을 회수합니다."""

    try:
        process.terminate()
    except OSError:
        pass
    try:
        return process.communicate(timeout=TRACEROUTE_PROCESS_STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
    try:
        return process.communicate(timeout=TRACEROUTE_PROCESS_STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(TRACEROUTE_PROCESS_STOP_FAILED_CODE) from exc


def ensure_target_hop(hops: Iterable[HopInfo], target: str, resolved_address: str | None = None) -> list[HopInfo]:
    hop_list = list(hops)
    target_values = {target}
    if resolved_address:
        target_values.add(resolved_address)
    for index, hop in enumerate(hop_list):
        if hop.address in target_values or hop.hostname in target_values:
            if not hop.is_target:
                hop_list[index] = replace(hop, is_target=True)
            return hop_list

    next_index = (max((hop.index for hop in hop_list), default=0) + 1)
    hop_list.append(
        HopInfo(
            index=next_index,
            address=resolved_address or target,
            hostname="Target",
            timed_out=False,
            is_target=True,
        )
    )
    return hop_list
