from __future__ import annotations

from datetime import datetime, timedelta

from app.core.models import STATUS_OK, STATUS_TIMEOUT, HopObservation
from app.storage.session_log import SessionLogWriter
from app.ui.session_observation_loader import (
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
