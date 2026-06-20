from __future__ import annotations

import ctypes

from app.core.models import STATUS_ERROR, STATUS_OK, STATUS_TIMEOUT, STATUS_UNREACHABLE
from app.core import ping_runner
from app.core.ping_runner import (
    CommandPingRunner,
    IcmpEchoReply,
    IcmpPingRunner,
    IP_SUCCESS,
    SubprocessPingRunner,
    TcpConnectRunner,
    _ipv4_to_ipaddr,
    parse_ping_output,
)


def test_parse_english_ping_latency() -> None:
    result = parse_ping_output("Reply from 8.8.8.8: bytes=32 time=23ms TTL=117", "8.8.8.8")
    assert result.success is True
    assert result.status == STATUS_OK
    assert result.latency_ms == 23


def test_parse_korean_ping_latency_under_one_ms() -> None:
    result = parse_ping_output("응답 192.168.0.1: 바이트=32 시간<1ms TTL=64", "192.168.0.1")
    assert result.success is True
    assert result.status == STATUS_OK
    assert result.latency_ms == 0.5


def test_parse_timeout() -> None:
    result = parse_ping_output("Request timed out.", "203.0.113.10")
    assert result.success is False
    assert result.status == STATUS_TIMEOUT


def test_parse_unexpected_name_failure_as_error() -> None:
    result = parse_ping_output("Ping request could not find host missing.example.", "missing.example")
    assert result.success is False
    assert result.status == STATUS_ERROR


def test_parse_unreachable() -> None:
    result = parse_ping_output("Destination host unreachable.", "192.0.2.1")
    assert result.success is False
    assert result.status == STATUS_UNREACHABLE


def test_ipv4_to_ipaddr_matches_inet_addr_layout() -> None:
    assert _ipv4_to_ipaddr("127.0.0.1") == 0x0100007F


def test_command_ping_runner_can_use_subprocess_fallback() -> None:
    runner = CommandPingRunner(1000, prefer_native=False)

    assert isinstance(runner._runner, SubprocessPingRunner)


def test_tcp_connect_runner_reports_open_port_reachable(monkeypatch) -> None:
    calls: list[tuple[tuple[str, int], float]] = []

    def create_connection(address, timeout):
        calls.append((address, timeout))
        return _FakeSocket()

    monkeypatch.setattr(ping_runner.socket, "create_connection", create_connection)

    result = TcpConnectRunner(timeout_ms=750, port=8443).ping("198.51.100.10")

    assert calls == [(("198.51.100.10", 8443), 0.75)]
    assert result.success is True
    assert result.status == STATUS_OK
    assert result.latency_ms is not None


def test_tcp_connect_runner_counts_refused_port_as_reachable(monkeypatch) -> None:
    def create_connection(_address, timeout):
        raise ConnectionRefusedError()

    monkeypatch.setattr(ping_runner.socket, "create_connection", create_connection)

    result = TcpConnectRunner(timeout_ms=1000, port=443).ping("198.51.100.10")

    assert result.success is True
    assert result.status == STATUS_OK
    assert result.latency_ms is not None


def test_tcp_connect_runner_maps_timeout_and_unreachable(monkeypatch) -> None:
    def timeout_connection(_address, timeout):
        raise TimeoutError()

    monkeypatch.setattr(ping_runner.socket, "create_connection", timeout_connection)
    timeout_result = TcpConnectRunner(timeout_ms=1000, port=443).ping("198.51.100.10")

    def unreachable_connection(_address, timeout):
        raise OSError()

    monkeypatch.setattr(ping_runner.socket, "create_connection", unreachable_connection)
    unreachable_result = TcpConnectRunner(timeout_ms=1000, port=443).ping("198.51.100.10")

    assert timeout_result.success is False
    assert timeout_result.status == STATUS_TIMEOUT
    assert unreachable_result.success is False
    assert unreachable_result.status == STATUS_UNREACHABLE


def test_icmp_ping_runner_reuses_handle(monkeypatch) -> None:
    fake_dll = _FakeIcmpDll()
    monkeypatch.setattr(ping_runner.platform, "system", lambda: "Windows")
    monkeypatch.setattr(ping_runner.ctypes, "WinDLL", lambda *_args, **_kwargs: fake_dll)

    runner = IcmpPingRunner(1000)

    first = runner.ping("127.0.0.1")
    second = runner.ping("127.0.0.1")
    runner.close()

    assert first.success is True
    assert second.success is True
    assert fake_dll.created == 1
    assert fake_dll.closed == 1
    assert fake_dll.send_handles == [fake_dll.handle, fake_dll.handle]


class _FakeCFunction:
    def __init__(self, func):
        self._func = func

    def __call__(self, *args):
        return self._func(*args)


class _FakeSocket:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return


class _FakeIcmpDll:
    def __init__(self) -> None:
        self.handle = 100
        self.created = 0
        self.closed = 0
        self.send_handles: list[int] = []
        self.IcmpCreateFile = _FakeCFunction(self._create)
        self.IcmpCloseHandle = _FakeCFunction(self._close)
        self.IcmpSendEcho = _FakeCFunction(self._send)

    def _create(self) -> int:
        self.created += 1
        return self.handle

    def _close(self, handle: int) -> bool:
        assert handle == self.handle
        self.closed += 1
        return True

    def _send(self, handle, _destination, _request_data, _request_size, _options, reply_buffer, _reply_size, _timeout):
        self.send_handles.append(handle)
        reply = IcmpEchoReply()
        reply.status = IP_SUCCESS
        reply.round_trip_time = 7
        ctypes.memmove(reply_buffer, ctypes.byref(reply), ctypes.sizeof(reply))
        return 1
