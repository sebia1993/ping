from __future__ import annotations

from datetime import datetime, timedelta

from app.core.models import HopInfo
from app.core.route_history import RouteHistory, route_path
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
    assert "added Hop 2" in change.summary


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
    assert loaded[0].summary == "changed Hop 1"
    assert "H1:192.0.2.1" in route_path_for_assertion(loaded[0].previous)


def route_path_for_assertion(snapshot) -> str:
    return route_path(snapshot)
