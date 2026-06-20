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


def build_traceroute_command(target: str, max_hops: int = 30, timeout_ms: int = 1000) -> list[str]:
    if platform.system().lower() == "windows":
        return ["tracert", "-h", str(max_hops), "-w", str(timeout_ms), target]
    timeout_seconds = max(1, int(round(timeout_ms / 1000)))
    return ["traceroute", "-m", str(max_hops), "-w", str(timeout_seconds), target]


def windows_no_window_flag() -> int:
    if platform.system().lower() == "windows":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


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

    while process.poll() is None:
        if stop_event and stop_event.is_set():
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
            return []
        time.sleep(0.1)

    stdout, stderr = process.communicate()
    return parse_tracert_output("\n".join(part for part in (stdout, stderr) if part))


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
