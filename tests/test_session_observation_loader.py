from __future__ import annotations

from datetime import datetime, timedelta

from app.core.models import STATUS_OK, STATUS_TIMEOUT, HopObservation
from app.storage.session_log import SessionLogWriter
from app.ui import session_observation_loader as session_observation_loader_module
from app.ui.session_observation_loader import (
    SESSION_GRAPH_LOAD_FAILED_CODE,
    SESSION_GRAPH_MAX_POINTS_PER_TARGET,
    SessionObservationLoader,
)


def test_session_observation_loader_bounds_large_target_history_and_preserves_evidence(tmp_path) -> None:
    target = "198.51.100.10"
    started_at = datetime(2026, 1, 1, 12, 0, 0)
    observations = [
        HopObservation(
            started_at + timedelta(seconds=index),
            0,
            target,
            "Target",
            index != 1000,
            None if index == 1000 else (5000.0 if index == 1500 else 10.0),
            STATUS_TIMEOUT if index == 1000 else STATUS_OK,
            True,
        )
        for index in range(2_000)
    ]
    path = tmp_path / "large.samples.csv"
    with SessionLogWriter(path) as writer:
        writer.write_many(observations)
    loaded: list[list[HopObservation]] = []
    loader = SessionObservationLoader(
        request_id=1,
        path=path,
        start=observations[0].timestamp,
        end=observations[-1].timestamp,
    )
    loader.loaded.connect(lambda _request_id, points: loaded.append(list(points)))

    loader.run()

    assert len(loaded) == 1
    points = loaded[0]
    assert len(points) <= SESSION_GRAPH_MAX_POINTS_PER_TARGET
    assert observations[0] in points
    assert observations[-1] in points
    assert observations[1000] in points
    assert observations[1500] in points


def test_session_observation_loader_honors_cancel_requested_before_run(tmp_path) -> None:
    now = datetime(2026, 1, 1, 12, 0, 0)
    path = tmp_path / "empty.samples.csv"
    SessionLogWriter(path).close()
    loaded: list[object] = []
    failed: list[str] = []
    loader = SessionObservationLoader(request_id=2, path=path, start=now, end=now)
    loader.loaded.connect(lambda _request_id, points: loaded.append(points))
    loader.failed.connect(lambda _request_id, message: failed.append(message))
    loader.request_cancel()

    loader.run()

    assert loaded == []
    assert failed == []


def test_session_observation_loader_reports_unexpected_reader_failure(tmp_path, monkeypatch) -> None:
    now = datetime(2026, 1, 1, 12, 0, 0)

    def fail_reader(*_args, **_kwargs):
        raise RuntimeError("unexpected parser failure")
        yield

    monkeypatch.setattr(
        session_observation_loader_module,
        "iter_observations_in_range",
        fail_reader,
    )
    failed: list[str] = []
    loader = SessionObservationLoader(
        request_id=3,
        path=tmp_path / "session.samples.csv",
        start=now,
        end=now,
    )
    loader.failed.connect(lambda _request_id, message: failed.append(message))

    loader.run()

    assert failed == [f"{SESSION_GRAPH_LOAD_FAILED_CODE}: RuntimeError"]


def test_session_observation_loader_stays_reserved_until_owner_cleanup(qt_app, tmp_path) -> None:
    """A finished worker must not look replaceable before its GUI cleanup slot runs."""

    target = "198.51.100.20"
    observation = HopObservation(
        datetime(2026, 1, 1, 12, 0, 0),
        0,
        target,
        "Target",
        True,
        10.0,
        STATUS_OK,
        True,
    )
    path = tmp_path / "lifecycle.samples.csv"
    with SessionLogWriter(path) as writer:
        writer.write_many([observation])

    loader = SessionObservationLoader(
        request_id=2,
        path=path,
        start=observation.timestamp,
        end=observation.timestamp,
    )
    loader.start()

    assert loader.wait(2_000)
    assert loader.isRunning()

    loader.deleteLater()

    assert not loader.isRunning()
