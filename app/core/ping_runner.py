from __future__ import annotations

import locale
import platform
import re
import socket
import struct
import subprocess
import ctypes
import threading
import time
from ctypes import POINTER, Structure, byref, c_ubyte, c_void_p, create_string_buffer, sizeof
from ctypes import wintypes
from datetime import datetime
from typing import Sequence

from app.core.models import (
    STATUS_ERROR,
    STATUS_OK,
    STATUS_TIMEOUT,
    STATUS_UNREACHABLE,
    PingResult,
)


LATENCY_RE = re.compile(
    r"(?:time|시간)\s*(?P<cmp>[=<])\s*(?P<value>\d+(?:\.\d+)?)\s*ms",
    re.IGNORECASE,
)

TIMEOUT_MARKERS = (
    "request timed out",
    "요청 시간이 만료",
    "100% loss",
    "100% 손실",
)

UNREACHABLE_MARKERS = (
    "destination host unreachable",
    "destination net unreachable",
    "대상 호스트에 연결할 수 없습니다",
    "일반 오류",
    "general failure",
)

IP_SUCCESS = 0
IP_DEST_NET_UNREACHABLE = 11002
IP_DEST_HOST_UNREACHABLE = 11003
IP_DEST_PROT_UNREACHABLE = 11004
IP_DEST_PORT_UNREACHABLE = 11005
IP_REQ_TIMED_OUT = 11010
IP_TTL_EXPIRED_TRANSIT = 11013
IP_GENERAL_FAILURE = 11050

ICMP_TIMEOUT_STATUSES = {IP_REQ_TIMED_OUT}
ICMP_UNREACHABLE_STATUSES = {
    IP_DEST_NET_UNREACHABLE,
    IP_DEST_HOST_UNREACHABLE,
    IP_DEST_PROT_UNREACHABLE,
    IP_DEST_PORT_UNREACHABLE,
    IP_TTL_EXPIRED_TRANSIT,
}


class IPOptionInformation(Structure):
    _fields_ = [
        ("ttl", c_ubyte),
        ("tos", c_ubyte),
        ("flags", c_ubyte),
        ("options_size", c_ubyte),
        ("options_data", c_void_p),
    ]


class IcmpEchoReply(Structure):
    _fields_ = [
        ("address", wintypes.DWORD),
        ("status", wintypes.ULONG),
        ("round_trip_time", wintypes.ULONG),
        ("data_size", wintypes.USHORT),
        ("reserved", wintypes.USHORT),
        ("data", c_void_p),
        ("options", IPOptionInformation),
    ]


def build_ping_command(target: str, timeout_ms: int) -> list[str]:
    if platform.system().lower() == "windows":
        return ["ping", "-n", "1", "-w", str(timeout_ms), target]
    timeout_seconds = max(1, int(round(timeout_ms / 1000)))
    return ["ping", "-c", "1", "-W", str(timeout_seconds), target]


def windows_no_window_flag() -> int:
    if platform.system().lower() == "windows":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def parse_ping_output(output: str, target: str, timestamp: datetime | None = None) -> PingResult:
    timestamp = timestamp or datetime.now()
    normalized = output.lower()

    if any(marker in normalized for marker in UNREACHABLE_MARKERS):
        return PingResult(target, False, None, STATUS_UNREACHABLE, timestamp)

    latency_match = LATENCY_RE.search(output)
    if latency_match:
        value = float(latency_match.group("value"))
        if latency_match.group("cmp") == "<" and value <= 1:
            value = 0.5
        return PingResult(target, True, value, STATUS_OK, timestamp)

    if "ttl=" in normalized:
        return PingResult(target, True, None, STATUS_OK, timestamp)

    if any(marker in normalized for marker in TIMEOUT_MARKERS):
        return PingResult(target, False, None, STATUS_TIMEOUT, timestamp)

    return PingResult(target, False, None, STATUS_ERROR, timestamp)


class IcmpPingRunner:
    def __init__(self, timeout_ms: int = 1000) -> None:
        if platform.system().lower() != "windows":
            raise OSError("Native ICMP runner is only available on Windows.")
        self.timeout_ms = timeout_ms
        win_dll = getattr(ctypes, "WinDLL", None)
        if win_dll is None:
            raise OSError("ctypes.WinDLL is unavailable.")
        self._iphlpapi = win_dll("iphlpapi", use_last_error=True)
        self._iphlpapi.IcmpCreateFile.restype = wintypes.HANDLE
        self._iphlpapi.IcmpCloseHandle.argtypes = [wintypes.HANDLE]
        self._iphlpapi.IcmpCloseHandle.restype = wintypes.BOOL
        self._iphlpapi.IcmpSendEcho.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.WORD,
            POINTER(IPOptionInformation),
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        self._iphlpapi.IcmpSendEcho.restype = wintypes.DWORD
        self._handle = self._iphlpapi.IcmpCreateFile()
        invalid_handle = c_void_p(-1).value
        if not self._handle or self._handle == invalid_handle:
            raise OSError("IcmpCreateFile failed.")
        self._closed = False
        self._lock = threading.Lock()

    def ping(self, target: str) -> PingResult:
        timestamp = datetime.now()
        if self._closed:
            return PingResult(target, False, None, STATUS_ERROR, timestamp)

        request_data = b"npd"
        reply_size = sizeof(IcmpEchoReply) + len(request_data) + 8
        reply_buffer = create_string_buffer(reply_size)
        options = IPOptionInformation(ttl=128, tos=0, flags=0, options_size=0, options_data=None)
        try:
            destination = _ipv4_to_ipaddr(target)
            with self._lock:
                if self._closed:
                    return PingResult(target, False, None, STATUS_ERROR, timestamp)
                reply_count = self._iphlpapi.IcmpSendEcho(
                    self._handle,
                    destination,
                    request_data,
                    len(request_data),
                    byref(options),
                    reply_buffer,
                    reply_size,
                    self.timeout_ms,
                )
            if reply_count == 0:
                return PingResult(target, False, None, STATUS_TIMEOUT, timestamp)

            reply = IcmpEchoReply.from_buffer_copy(reply_buffer)
            if reply.status == IP_SUCCESS:
                return PingResult(target, True, float(reply.round_trip_time), STATUS_OK, timestamp)
            return PingResult(target, False, None, _icmp_status_to_result_status(reply.status), timestamp)
        except OSError:
            return PingResult(target, False, None, STATUS_ERROR, timestamp)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._iphlpapi.IcmpCloseHandle(self._handle)
            self._closed = True

    def __enter__(self) -> "IcmpPingRunner":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


class SubprocessPingRunner:
    def __init__(self, timeout_ms: int = 1000) -> None:
        self.timeout_ms = timeout_ms

    def ping(self, target: str) -> PingResult:
        command = build_ping_command(target, self.timeout_ms)
        encoding = locale.getpreferredencoding(False)
        timestamp = datetime.now()
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding=encoding,
                errors="replace",
                timeout=(self.timeout_ms / 1000) + 2,
                check=False,
                creationflags=windows_no_window_flag(),
            )
        except subprocess.TimeoutExpired:
            return PingResult(target, False, None, STATUS_TIMEOUT, timestamp)
        except OSError:
            return PingResult(target, False, None, STATUS_ERROR, timestamp)

        output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
        return parse_ping_output(output, target, timestamp)


class CommandPingRunner:
    def __init__(self, timeout_ms: int = 1000, *, prefer_native: bool = True) -> None:
        if prefer_native and platform.system().lower() == "windows":
            try:
                self._runner = IcmpPingRunner(timeout_ms)
                return
            except OSError:
                pass
        self._runner = SubprocessPingRunner(timeout_ms)

    def ping(self, target: str) -> PingResult:
        return self._runner.ping(target)

    def close(self) -> None:
        close = getattr(self._runner, "close", None)
        if close:
            try:
                close()
            except Exception:
                pass

    def __enter__(self) -> "CommandPingRunner":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


class TcpConnectRunner:
    def __init__(self, timeout_ms: int = 1000, port: int = 443) -> None:
        self.timeout_ms = timeout_ms
        self.port = port

    def ping(self, target: str) -> PingResult:
        timestamp = datetime.now()
        started = time.perf_counter()
        try:
            with socket.create_connection((target, self.port), timeout=max(self.timeout_ms / 1000, 0.001)):
                pass
            return PingResult(target, True, _elapsed_ms(started), STATUS_OK, timestamp)
        except ConnectionRefusedError:
            return PingResult(target, True, _elapsed_ms(started), STATUS_OK, timestamp)
        except TimeoutError:
            return PingResult(target, False, None, STATUS_TIMEOUT, timestamp)
        except OSError:
            return PingResult(target, False, None, STATUS_UNREACHABLE, timestamp)

    def close(self) -> None:
        return

    def __enter__(self) -> "TcpConnectRunner":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


class FakePingRunner:
    def __init__(self, results: Sequence[PingResult]) -> None:
        self._results = list(results)
        self._index = 0

    def ping(self, target: str) -> PingResult:
        if not self._results:
            return PingResult(target, False, None, STATUS_TIMEOUT, datetime.now())
        result = self._results[min(self._index, len(self._results) - 1)]
        self._index += 1
        return PingResult(target, result.success, result.latency_ms, result.status, datetime.now())


def _ipv4_to_ipaddr(target: str) -> int:
    # IcmpSendEcho expects the same little-endian DWORD representation returned by inet_addr.
    return struct.unpack("=L", socket.inet_aton(target))[0]


def _icmp_status_to_result_status(status: int) -> str:
    if status in ICMP_TIMEOUT_STATUSES:
        return STATUS_TIMEOUT
    if status in ICMP_UNREACHABLE_STATUSES:
        return STATUS_UNREACHABLE
    if status == IP_GENERAL_FAILURE:
        return STATUS_ERROR
    return STATUS_ERROR


def _elapsed_ms(started: float) -> float:
    return max((time.perf_counter() - started) * 1000, 0.0)
