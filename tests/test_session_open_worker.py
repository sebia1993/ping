from __future__ import annotations

import time
from datetime import datetime

from app.core.models import STATUS_OK, HopObservation
from app.storage.session_index import SessionIndexStore
from app.storage.session_log import SessionLogWriter
from app.ui import session_open_worker as session_open_worker_module
from app.ui.session_open_worker import SESSION_OPEN_FAILED_CODE, SessionOpenWorker


def _wait_for_worker(worker: SessionOpenWorker, qt_app, timeout_seconds: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while worker.isRunning() and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.01)
    assert worker.wait(1000)
    qt_app.processEvents()


def test_session_open_worker_loads_observations_off_ui_thread(qt_app, tmp_path) -> None:
    now = datetime(2026, 1, 1, 12, 0, 0)
    sample_path = tmp_path / "session.samples.csv"
    with SessionLogWriter(sample_path) as writer:
        writer.write_many([
            HopObservation(now, 0, "198.51.100.10", "Target", True, 10.0, STATUS_OK, True),
        ])
    store = SessionIndexStore.create(tmp_path)
    record = store.register_session(
        target="198.51.100.10",
        sample_path=sample_path,
        route_path=None,
        started_at=now,
        interval_seconds=1,
        measurement_mode="final_hop_only:icmp",
        target_count=1,
    )
    loaded: list[object] = []

    worker = SessionOpenWorker(request_id=7, record=record)
    worker.loaded.connect(lambda request_id, result: loaded.append((request_id, result)))
    worker.start()
    _wait_for_worker(worker, qt_app)

    assert loaded and loaded[0][0] == 7
    assert len(loaded[0][1].observations) == 1
    assert loaded[0][1].summary.rows == 1
    assert loaded[0][1].snapshot_set.target_snapshot.sent == 1


def test_session_open_worker_bounds_ui_memory_but_summarizes_full_session(
    qt_app,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(session_open_worker_module, "SESSION_OPEN_RECENT_OBSERVATION_LIMIT", 3)
    now = datetime(2026, 1, 1, 12, 0, 0)
    sample_path = tmp_path / "bounded.samples.csv"
    with SessionLogWriter(sample_path) as writer:
        writer.write_many([
            HopObservation(
                now.replace(second=index),
                0,
                "198.51.100.10",
                "Target",
                True,
                float(index + 1),
                STATUS_OK,
                True,
            )
            for index in range(5)
        ])
    store = SessionIndexStore.create(tmp_path)
    record = store.register_session(
        target="198.51.100.10",
        sample_path=sample_path,
        route_path=None,
        started_at=now,
        interval_seconds=1,
        measurement_mode="final_hop_only:icmp",
        target_count=1,
    )
    loaded: list[object] = []
    worker = SessionOpenWorker(request_id=9, record=record)
    worker.loaded.connect(lambda _request_id, result: loaded.append(result))

    worker.start()
    _wait_for_worker(worker, qt_app)

    result = loaded[0]
    assert result.summary.rows == 5
    assert len(result.observations) == 3
    assert [item.latency_ms for item in result.observations] == [3.0, 4.0, 5.0]
    assert result.snapshot_set.target_snapshot.sent == 5
    assert result.snapshot_set.target_snapshot.avg_latency_ms == 3.0


def test_session_open_worker_reports_read_failure(qt_app, tmp_path) -> None:
    store = SessionIndexStore.create(tmp_path)
    now = datetime(2026, 1, 1, 12, 0, 0)
    sample_path = tmp_path / "missing.samples.csv"
    sample_path.mkdir()
    record = store.register_session(
        target="198.51.100.10",
        sample_path=sample_path,
        route_path=None,
        started_at=now,
        interval_seconds=1,
        measurement_mode="final_hop_only:icmp",
        target_count=1,
    )
    errors: list[str] = []

    worker = SessionOpenWorker(request_id=8, record=record)
    worker.error_message.connect(lambda request_id, message: errors.append(f"{request_id}:{message}"))
    worker.start()
    _wait_for_worker(worker, qt_app)

    assert errors == [f"8:{SESSION_OPEN_FAILED_CODE}: PermissionError"]


def test_session_open_worker_can_cancel_before_reading(qt_app, tmp_path) -> None:
    now = datetime(2026, 1, 1, 12, 0, 0)
    sample_path = tmp_path / "cancel.samples.csv"
    with SessionLogWriter(sample_path) as writer:
        writer.write_many([
            HopObservation(now, 0, "198.51.100.10", "Target", True, 10.0, STATUS_OK, True),
        ])
    store = SessionIndexStore.create(tmp_path)
    record = store.register_session(
        target="198.51.100.10",
        sample_path=sample_path,
        route_path=None,
        started_at=now,
        interval_seconds=1,
        measurement_mode="final_hop_only:icmp",
        target_count=1,
    )
    loaded: list[object] = []
    errors: list[str] = []
    worker = SessionOpenWorker(request_id=10, record=record)
    worker.loaded.connect(lambda request_id, result: loaded.append((request_id, result)))
    worker.error_message.connect(lambda request_id, message: errors.append(f"{request_id}:{message}"))
    worker.request_cancel()

    worker.start()
    _wait_for_worker(worker, qt_app)

    assert loaded == []
    assert errors == []


def test_session_open_worker_can_cancel_an_empty_session(qt_app, tmp_path) -> None:
    now = datetime(2026, 1, 1, 12, 0, 0)
    sample_path = tmp_path / "empty.samples.csv"
    SessionLogWriter(sample_path).close()
    record = SessionIndexStore.create(tmp_path).register_session(
        target="198.51.100.10",
        sample_path=sample_path,
        route_path=None,
        started_at=now,
        interval_seconds=1,
        measurement_mode="final_hop_only:icmp",
        target_count=1,
    )
    loaded: list[object] = []
    worker = SessionOpenWorker(request_id=11, record=record)
    worker.loaded.connect(lambda request_id, result: loaded.append((request_id, result)))
    worker.request_cancel()

    worker.start()
    _wait_for_worker(worker, qt_app)

    assert loaded == []


def test_session_open_worker_keeps_loaded_data_when_index_repair_fails(
    qt_app,
    tmp_path,
    monkeypatch,
) -> None:
    now = datetime(2026, 1, 1, 12, 0, 0)
    sample_path = tmp_path / "index-repair.samples.csv"
    with SessionLogWriter(sample_path) as writer:
        writer.write_many([
            HopObservation(now, 0, "198.51.100.10", "Target", True, 10.0, STATUS_OK, True),
        ])
    store = SessionIndexStore.create(tmp_path)
    registered = store.register_session(
        target="198.51.100.10",
        sample_path=sample_path,
        route_path=None,
        started_at=now,
        interval_seconds=1,
        measurement_mode="final_hop_only:icmp",
        target_count=1,
    )
    store.finish_session(registered.session_id, state="archived", ended_at=now)
    record = store.find_session(registered.session_id)
    assert record is not None
    def fail_finish_session(*_args, **_kwargs) -> None:
        raise RuntimeError("index failure")

    monkeypatch.setattr(
        session_open_worker_module.SessionIndexStore,
        "finish_session",
        fail_finish_session,
    )
    loaded: list[object] = []
    errors: list[str] = []
    worker = SessionOpenWorker(request_id=12, record=record)
    worker.loaded.connect(lambda _request_id, result: loaded.append(result))
    worker.error_message.connect(lambda _request_id, message: errors.append(message))

    worker.start()
    _wait_for_worker(worker, qt_app)

    assert len(loaded) == 1
    assert loaded[0].summary.rows == 1
    assert errors == []
