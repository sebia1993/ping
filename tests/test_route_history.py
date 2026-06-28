from __future__ import annotations

from datetime import datetime, timedelta

from app.core.models import HopInfo
from app.core.route_history import RouteHistory, route_path
from app.storage import route_log as route_log_module
from app.storage.route_log import RouteLogWriter, route_changes_in_range, route_log_path_for_session


def test_route_history_records_baseline_and_detects_changed_hops() -> None:
    history = RouteHistory()
    now = datetime(2026, 1, 1, 12, 0, 0)
    first = [
        HopInfo(index=1, address="192.0.2.1", hostname="gw"),
        HopInfo(index=2, address="198.51.100.10", is_target=True),
    ]
    second = [
        HopInfo(index=1, address="192.0.2.254", hostname="backup-gw"),
        HopInfo(index=2, address="198.51.100.10", is_target=True),
    ]

    assert history.record(first, now) is None
    assert history.record(first, now + timedelta(seconds=60)) is None
    change = history.record(second, now + timedelta(seconds=120))

    assert change is not None
    assert change.changed_hops == (1,)
    assert change.added_hops == ()
    assert "Hop 1" in change.summary
    assert "H1:192.0.2.1" in route_path(change.previous)
    assert "H1:192.0.2.254" in route_path(change.current)
    assert len(history.snapshots) == 3
    assert len(history.changes) == 1


def test_route_history_detects_added_and_removed_hops() -> None:
    history = RouteHistory()
    now = datetime(2026, 1, 1, 12, 0, 0)
    history.record([HopInfo(index=1, address="192.0.2.1")], now)

    change = history.record(
        [
            HopInfo(index=1, address="192.0.2.1"),
            HopInfo(index=2, address="198.51.100.10", is_target=True),
        ],
        now + timedelta(seconds=60),
    )

    assert change is not None
    assert change.added_hops == (2,)
    assert "추가 Hop 2" in change.summary


def test_route_log_persists_snapshots_and_reads_changes_in_range(tmp_path) -> None:
    session_path = tmp_path / "network_trace_198_51_100_10_20260101_120000.samples.csv"
    route_path = route_log_path_for_session(session_path)
    history = RouteHistory()
    now = datetime(2026, 1, 1, 12, 0, 0)

    history.record(
        [
            HopInfo(index=1, address="192.0.2.1", hostname="gateway"),
            HopInfo(index=2, address="198.51.100.10", is_target=True),
        ],
        now,
    )
    change = history.record(
        [
            HopInfo(index=1, address="192.0.2.254", hostname="backup"),
            HopInfo(index=2, address="198.51.100.10", is_target=True),
        ],
        now + timedelta(seconds=60),
    )
    assert route_path == tmp_path / "network_trace_198_51_100_10_20260101_120000.samples.routes.csv"
    assert change is not None

    with RouteLogWriter(route_path) as writer:
        writer.write_snapshot(history.snapshots[0])
        writer.write_snapshot(history.snapshots[1], change)

    loaded = route_changes_in_range(route_path, now + timedelta(seconds=30), now + timedelta(seconds=90))

    assert len(loaded) == 1
    assert loaded[0].changed_hops == (1,)
    assert loaded[0].summary == "변경 Hop 1"
    assert "H1:192.0.2.1" in route_path_for_assertion(loaded[0].previous)


def test_route_log_writer_retries_transient_open_error(tmp_path, monkeypatch) -> None:
    route_path = tmp_path / "session.routes.csv"
    attempts = 0
    original_open = route_log_module._open_csv_write_path

    def flaky_open(write_path):
        nonlocal attempts
        attempts += 1
        if write_path == route_path and attempts < 3:
            raise PermissionError("temporarily locked")
        return original_open(write_path)

    monkeypatch.setattr(route_log_module, "ROUTE_LOG_IO_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(route_log_module, "_open_csv_write_path", flaky_open)
    history = RouteHistory()
    history.record([HopInfo(index=1, address="192.0.2.1")], datetime(2026, 1, 1, 12, 0, 0))

    with RouteLogWriter(route_path) as writer:
        writer.write_snapshot(history.snapshots[-1])

    assert attempts == 3
    assert route_path.exists()


def test_route_log_writer_retries_transient_flush_error(tmp_path, monkeypatch) -> None:
    route_path = tmp_path / "session.routes.csv"
    attempts = 0
    original_flush = route_log_module._flush_handle

    def flaky_flush(handle):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("temporarily locked")
        return original_flush(handle)

    monkeypatch.setattr(route_log_module, "ROUTE_LOG_IO_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(route_log_module, "_flush_handle", flaky_flush)
    history = RouteHistory()
    now = datetime(2026, 1, 1, 12, 0, 0)
    history.record([HopInfo(index=1, address="192.0.2.1")], now)
    change = history.record([HopInfo(index=1, address="192.0.2.254")], now + timedelta(seconds=60))
    assert change is not None

    with RouteLogWriter(route_path) as writer:
        writer.write_snapshot(history.snapshots[1], change)

    changes = route_changes_in_range(route_path, now, now + timedelta(seconds=120))

    assert attempts >= 3
    assert len(changes) == 1
    assert changes[0].changed_hops == (1,)


def test_route_log_writer_close_retries_transient_flush_error(tmp_path, monkeypatch) -> None:
    route_path = tmp_path / "session.routes.csv"
    attempts = 0
    original_flush = route_log_module._flush_handle

    def flaky_flush(handle):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("temporarily locked")
        return original_flush(handle)

    monkeypatch.setattr(route_log_module, "ROUTE_LOG_IO_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(route_log_module, "_flush_handle", flaky_flush)

    writer = RouteLogWriter(route_path)
    writer.close()

    assert attempts == 3
    assert route_path.read_text(encoding="utf-8").startswith("record_type,timestamp")


def test_route_log_writer_close_is_idempotent(tmp_path) -> None:
    route_path = tmp_path / "session.routes.csv"
    writer = RouteLogWriter(route_path)

    writer.close()
    writer.close()

    assert route_path.read_text(encoding="utf-8").startswith("record_type,timestamp")


def test_route_log_writer_close_suppresses_os_error_after_flush(tmp_path, monkeypatch) -> None:
    route_path = tmp_path / "session.routes.csv"
    original_close = route_log_module._close_handle

    def close_then_fail(handle):
        original_close(handle)
        raise PermissionError("locked during close")

    monkeypatch.setattr(route_log_module, "_close_handle", close_then_fail)
    history = RouteHistory()
    now = datetime(2026, 1, 1, 12, 0, 0)
    history.record([HopInfo(index=1, address="192.0.2.1")], now)
    change = history.record([HopInfo(index=1, address="192.0.2.254")], now + timedelta(seconds=60))
    assert change is not None

    writer = RouteLogWriter(route_path)
    writer.write_snapshot(history.snapshots[1], change)
    writer.close()

    changes = route_changes_in_range(route_path, now, now + timedelta(seconds=120))

    assert len(changes) == 1
    assert changes[0].changed_hops == (1,)


def test_route_log_read_returns_empty_when_file_is_locked(tmp_path, monkeypatch) -> None:
    route_path = tmp_path / "session.routes.csv"
    route_path.write_text("record_type,timestamp\n", encoding="utf-8")
    original_open = type(route_path).open

    def locked_open(self, *args, **kwargs):
        if self == route_path:
            raise PermissionError("temporarily locked")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(type(route_path), "open", locked_open)

    changes = route_changes_in_range(
        route_path,
        datetime(2026, 1, 1, 12, 0, 0),
        datetime(2026, 1, 1, 12, 1, 0),
    )

    assert changes == []


def test_route_log_read_retries_transient_open_error(tmp_path, monkeypatch) -> None:
    session_path = tmp_path / "network_trace_198_51_100_10_20260101_120000.samples.csv"
    route_path = route_log_path_for_session(session_path)
    history = RouteHistory()
    now = datetime(2026, 1, 1, 12, 0, 0)
    history.record([HopInfo(index=1, address="192.0.2.1")], now)
    change = history.record([HopInfo(index=1, address="192.0.2.254")], now + timedelta(seconds=60))
    assert change is not None
    with RouteLogWriter(route_path) as writer:
        writer.write_snapshot(history.snapshots[0])
        writer.write_snapshot(history.snapshots[1], change)
    attempts = 0
    original_open = route_log_module._open_csv_read_path

    def flaky_open(read_path):
        nonlocal attempts
        attempts += 1
        if read_path == route_path and attempts < 3:
            raise PermissionError("temporarily locked")
        return original_open(read_path)

    monkeypatch.setattr(route_log_module, "ROUTE_LOG_IO_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(route_log_module, "_open_csv_read_path", flaky_open)

    changes = route_changes_in_range(
        route_path,
        now,
        now + timedelta(seconds=120),
    )

    assert attempts == 3
    assert len(changes) == 1
    assert changes[0].changed_hops == (1,)


def route_path_for_assertion(snapshot) -> str:
    return route_path(snapshot)
