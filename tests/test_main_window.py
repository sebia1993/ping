from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication, QMessageBox

from app.core.alerts import AlertEvent
from app.core.models import STATUS_OK, STATUS_TIMEOUT, HopInfo, HopObservation, MetricSnapshot
from app.core.route_history import RouteHistory
from app.storage.alert_action_log import alert_action_log_path_for_session, read_alert_actions
from app.storage.route_log import RouteLogWriter, route_log_path_for_session
from app.storage.session_index import SESSION_STATE_ARCHIVED, SessionIndexStore
from app.storage.session_log import SessionLogWriter
from app.storage.statistics_exporter import TIMEZONE_UTC
from app.ui import main_window as main_window_module
from app.ui.graph_detail_window import VIEW_SELECTED_HOP, VIEW_VISIBLE_HOPS
from app.ui.main_window import MainWindow, STATISTICS_SCOPE_FOCUS, STATISTICS_SCOPE_VISIBLE, TABLE_HEADERS, TARGET_HEADERS
from app.ui.worker import (
    MEASUREMENT_MODE_FINAL_HOP_ONLY,
    MEASUREMENT_MODE_FULL_ROUTE,
    PROBE_ENGINE_TCP_CONNECT,
)


def test_main_window_initial_state(qt_app) -> None:
    window = MainWindow()

    try:
        assert window.session_state_label.text() == "대기"
        assert window.status_label.text() == "대기 중"
        assert QApplication.instance().font().family() == "Malgun Gothic"
        assert "Malgun Gothic" in set(QFontDatabase.families())
        assert window.table.columnCount() == len(TABLE_HEADERS)
        assert window.target_table.columnCount() == len(TARGET_HEADERS)
        assert window.csv_button.isEnabled() is False
        assert window.xlsx_button.isEnabled() is False
        assert window.report_button.isEnabled() is False
        assert window.stats_csv_button.isEnabled() is False
        assert window.stats_xlsx_button.isEnabled() is False
        assert window.alert_timeline_action_check.isChecked() is True
        assert window.alert_comment_action_check.isChecked() is True
        assert window.alert_beep_action_check.isChecked() is False
        assert window.export_session_button.isEnabled() == (window.session_combo.count() > 0)
        assert window.graph_detail_button.text() == "그래프 확대"
        assert window.sessions_box.toPlainText()
    finally:
        window.close()


def test_main_window_renders_session_index_summary(qt_app, tmp_path) -> None:
    window = MainWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    store = SessionIndexStore.create(tmp_path)
    sample_path = tmp_path / "198.51.100.10" / "2026-01" / "session.samples.csv"
    record = store.register_session(
        target="198.51.100.10",
        sample_path=sample_path,
        route_path=sample_path.with_name("session.routes.csv"),
        started_at=now,
        interval_seconds=1,
        measurement_mode="full_route",
        target_count=1,
    )
    store.add_samples(record.session_id, 12, now + timedelta(seconds=12), segments=[sample_path])
    store.finish_session(record.session_id, state=SESSION_STATE_ARCHIVED, ended_at=now + timedelta(seconds=20))

    try:
        window.session_index_store = store
        window._sync_sessions_box()

        text = window.sessions_box.toPlainText()
        assert "Archived" in text
        assert "198.51.100.10" in text
        assert "samples 12" in text
        assert "full_route" in text
        assert window.session_combo.count() == 1
        assert window.open_session_button.isEnabled() is True
        assert window.export_session_button.isEnabled() is True
    finally:
        window.close()


def test_main_window_opens_saved_session_from_session_manager(qt_app, tmp_path) -> None:
    window = MainWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    store = SessionIndexStore.create(tmp_path)
    sample_path = tmp_path / "198.51.100.10" / "2026-01" / "session.samples.csv"
    with SessionLogWriter(sample_path) as writer:
        writer.write_many([
            HopObservation(now, 0, "198.51.100.10", "Target", True, 10.0, STATUS_OK, True),
            HopObservation(now + timedelta(seconds=1), 0, "198.51.100.10", "Target", False, None, STATUS_TIMEOUT, True),
            HopObservation(now + timedelta(seconds=2), 1, "192.0.2.1", "gateway", True, 2.0, STATUS_OK),
        ])
    record = store.register_session(
        target="198.51.100.10",
        sample_path=sample_path,
        route_path=sample_path.with_name("session.routes.csv"),
        started_at=now,
        interval_seconds=1,
        measurement_mode="final_hop_only:icmp",
        target_count=1,
    )
    store.add_samples(record.session_id, 3, now + timedelta(seconds=2), segments=[sample_path])
    store.finish_session(record.session_id, state=SESSION_STATE_ARCHIVED, ended_at=now + timedelta(seconds=2))

    try:
        window.session_index_store = store
        window._sync_sessions_box()
        window.session_combo.setCurrentIndex(window.session_combo.findData(record.session_id))

        window.open_selected_session()

        assert window.current_target == "198.51.100.10"
        assert window.session_log_path == sample_path
        assert len(window.observations) == 3
        assert len(window.target_history) == 2
        assert window.target_snapshot is not None
        assert window.target_snapshot.sent == 2
        assert window.target_snapshot.loss_percent == 50.0
        assert window.graph._points == window.target_history
        assert window.csv_button.isEnabled() is True
        assert "Loaded session" in window.status_label.text()
    finally:
        window.close()


def test_main_window_prepares_saved_session_resume_controls(qt_app, tmp_path) -> None:
    window = MainWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    store = SessionIndexStore.create(tmp_path)
    sample_path = tmp_path / "198.51.100.10" / "2026-01" / "session.samples.csv"
    with SessionLogWriter(sample_path) as writer:
        writer.write_many([
            HopObservation(now, 0, "198.51.100.10", "Target", True, 10.0, STATUS_OK, True),
            HopObservation(now + timedelta(seconds=1), 0, "203.0.113.20", "Target", True, 20.0, STATUS_OK, True),
            HopObservation(now + timedelta(seconds=2), 1, "192.0.2.1", "gateway", True, 2.0, STATUS_OK),
        ])
    record = store.register_session(
        target="198.51.100.10",
        sample_path=sample_path,
        route_path=sample_path.with_name("session.routes.csv"),
        started_at=now,
        interval_seconds=7,
        measurement_mode=f"{MEASUREMENT_MODE_FINAL_HOP_ONLY}:{PROBE_ENGINE_TCP_CONNECT}:port8443",
        target_count=2,
    )
    store.add_samples(record.session_id, 3, now + timedelta(seconds=2), segments=[sample_path])
    store.finish_session(record.session_id, state=SESSION_STATE_ARCHIVED, ended_at=now + timedelta(seconds=2))

    try:
        window.session_index_store = store
        window._sync_sessions_box()
        window.session_combo.setCurrentIndex(window.session_combo.findData(record.session_id))

        window.resume_selected_session()

        assert window.worker is None
        assert window.target_input.toPlainText().splitlines() == ["198.51.100.10", "203.0.113.20"]
        assert window.trace_target_combo.currentText() == "198.51.100.10"
        assert window.interval_combo.currentText() == "7"
        assert window.measurement_mode_combo.currentData() == MEASUREMENT_MODE_FINAL_HOP_ONLY
        assert window.probe_engine_combo.currentData() == PROBE_ENGINE_TCP_CONNECT
        assert window.tcp_port_spin.value() == 8443
        assert window.tcp_port_spin.isEnabled() is True
        assert "Resume prepared: 2 target(s)" in window.status_label.text()
    finally:
        window.close()


def test_main_window_exports_selected_saved_session(qt_app, tmp_path, monkeypatch) -> None:
    created_workers: list[_FakeExportWorker] = []
    export_path = tmp_path / "saved_session.csv"

    def fake_get_save_file_name(*_args, **_kwargs):
        return str(export_path), "CSV Files (*.csv)"

    def fake_export_worker(**kwargs):
        worker = _FakeExportWorker(**kwargs)
        created_workers.append(worker)
        return worker

    monkeypatch.setattr(main_window_module.QFileDialog, "getSaveFileName", fake_get_save_file_name)
    monkeypatch.setattr(main_window_module, "ExportWorker", fake_export_worker)
    window = MainWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    store = SessionIndexStore.create(tmp_path)
    sample_path = tmp_path / "198.51.100.10" / "2026-01" / "session.samples.csv"
    with SessionLogWriter(sample_path) as writer:
        writer.write_many([
            HopObservation(now, 0, "198.51.100.10", "Target", True, 10.0, STATUS_OK, True),
            HopObservation(now + timedelta(seconds=1), 0, "198.51.100.10", "Target", False, None, STATUS_TIMEOUT, True),
        ])
    record = store.register_session(
        target="198.51.100.10",
        sample_path=sample_path,
        route_path=sample_path.with_name("session.routes.csv"),
        started_at=now,
        interval_seconds=1,
        measurement_mode="final_hop_only:icmp",
        target_count=1,
    )
    store.add_samples(record.session_id, 2, now + timedelta(seconds=1), segments=[sample_path])
    store.finish_session(record.session_id, state=SESSION_STATE_ARCHIVED, ended_at=now + timedelta(seconds=1))

    try:
        window.session_index_store = store
        window._sync_sessions_box()
        window.session_combo.setCurrentIndex(window.session_combo.findData(record.session_id))

        window.export_selected_session()

        assert len(created_workers) == 1
        worker = created_workers[0]
        assert worker.kwargs["kind"] == "csv"
        assert worker.kwargs["path"] == export_path
        assert worker.kwargs["target"] == "198.51.100.10"
        assert worker.kwargs["session_log_path"] == sample_path
        assert worker.kwargs["snapshots"][0].sent == 2
        assert worker.kwargs["analysis"]
        assert worker.started is True
        assert window.export_session_button.isEnabled() is False
    finally:
        window.close()


def test_main_window_deletes_selected_saved_session(qt_app, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        main_window_module.QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    window = MainWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    store = SessionIndexStore.create(tmp_path)
    sample_path = tmp_path / "198.51.100.10" / "2026-01" / "session.samples.csv"
    route_path = sample_path.with_name("session.routes.csv")
    alert_path = alert_action_log_path_for_session(sample_path)
    with SessionLogWriter(sample_path) as writer:
        writer.write_many([
            HopObservation(now, 0, "198.51.100.10", "Target", True, 10.0, STATUS_OK, True),
        ])
    route_path.write_text("route\n", encoding="utf-8")
    assert alert_path is not None
    alert_path.write_text("alert\n", encoding="utf-8")
    record = store.register_session(
        target="198.51.100.10",
        sample_path=sample_path,
        route_path=route_path,
        started_at=now,
        interval_seconds=1,
        measurement_mode="final_hop_only:icmp",
        target_count=1,
    )
    store.add_samples(record.session_id, 1, now, segments=[sample_path])
    store.finish_session(record.session_id, state=SESSION_STATE_ARCHIVED, ended_at=now)

    try:
        window.session_index_store = store
        window._sync_sessions_box()
        window.session_combo.setCurrentIndex(window.session_combo.findData(record.session_id))

        window.delete_selected_session()

        assert store.list_sessions() == []
        assert window.session_combo.count() == 0
        assert "Deleted session" in window.status_label.text()
        assert not sample_path.exists()
        assert not route_path.exists()
        assert not alert_path.exists()
        assert window.delete_session_button.isEnabled() is False
    finally:
        window.close()


def test_main_window_status_chip_transitions(qt_app) -> None:
    window = MainWindow()

    try:
        for message, expected in [
            ("경로 탐색 중...", "탐색"),
            ("측정 중...", "측정"),
            ("중지 요청 중...", "중지"),
            ("측정이 완료되었습니다.", "완료"),
        ]:
            window.on_status_message(message)
            assert window.session_state_label.text() == expected
    finally:
        window.close()


def test_main_window_finished_preserves_completed_state(qt_app) -> None:
    window = MainWindow()

    try:
        window.on_status_message("측정이 완료되었습니다.")
        window.observations = [
            HopObservation(datetime.now(), 0, "198.51.100.10", "Target", True, 1.0, STATUS_OK, True)
        ]
        window.on_worker_finished()

        assert window.session_state_label.text() == "완료"
        assert window.worker is None
    finally:
        window.close()


def test_main_window_finished_preserves_stopped_state(qt_app) -> None:
    window = MainWindow()

    try:
        window.on_status_message("측정이 중지되었습니다.")
        window.observations = [
            HopObservation(datetime.now(), 0, "198.51.100.10", "Target", True, 1.0, STATUS_OK, True)
        ]
        window.on_worker_finished()

        assert window.session_state_label.text() == "중지"
        assert window.worker is None
    finally:
        window.close()


def test_main_window_start_stop_uses_operator_inputs(qt_app) -> None:
    created_workers: list[_FakeWorker] = []

    def worker_factory(
        target: str,
        interval_seconds: int,
        max_cycles: int | None,
        targets: list[str],
        measurement_mode: str,
        probe_engine: str = "icmp",
        tcp_port: int = 443,
    ) -> "_FakeWorker":
        worker = _FakeWorker(
            target=target,
            interval_seconds=interval_seconds,
            max_cycles=max_cycles,
            targets=targets,
            measurement_mode=measurement_mode,
            probe_engine=probe_engine,
            tcp_port=tcp_port,
        )
        created_workers.append(worker)
        return worker

    window = MainWindow(worker_factory=worker_factory)

    try:
        window.target_input.setText("8.8.8.8\n192.168.0.1\n8.8.8.8")
        window.refresh_trace_targets()
        window.trace_target_combo.setCurrentText("192.168.0.1")
        window.interval_combo.setCurrentText("2")
        window.unlimited_check.setChecked(False)
        window.count_spin.setValue(3)
        probe_index = window.probe_engine_combo.findData(PROBE_ENGINE_TCP_CONNECT)
        assert probe_index >= 0
        window.probe_engine_combo.setCurrentIndex(probe_index)
        window.tcp_port_spin.setValue(8443)

        window.start_measurement()

        assert len(created_workers) == 1
        assert created_workers[0].target == "192.168.0.1"
        assert created_workers[0].targets == ["8.8.8.8", "192.168.0.1"]
        assert created_workers[0].interval_seconds == 2
        assert created_workers[0].max_cycles == 3
        assert created_workers[0].measurement_mode == MEASUREMENT_MODE_FULL_ROUTE
        assert created_workers[0].probe_engine == PROBE_ENGINE_TCP_CONNECT
        assert created_workers[0].tcp_port == 8443
        assert created_workers[0].started is True
        assert window.start_button.isEnabled() is False
        assert window.stop_button.isEnabled() is True

        window.stop_measurement()

        assert created_workers[0].stopped is True
        assert window.stop_button.isEnabled() is False
        assert window.session_state_label.text() == "중지"
    finally:
        window.close()


def test_main_window_batch_target_controls_drive_worker(qt_app) -> None:
    created_workers: list[_FakeWorker] = []

    def worker_factory(
        target: str,
        interval_seconds: int,
        max_cycles: int | None,
        targets: list[str],
        measurement_mode: str,
        probe_engine: str = "icmp",
        tcp_port: int = 443,
    ) -> "_FakeWorker":
        worker = _FakeWorker(
            target=target,
            interval_seconds=interval_seconds,
            max_cycles=max_cycles,
            targets=targets,
            measurement_mode=measurement_mode,
            probe_engine=probe_engine,
            tcp_port=tcp_port,
        )
        created_workers.append(worker)
        return worker

    window = MainWindow(worker_factory=worker_factory)
    now = datetime.now()
    target_one = _snapshot(0, "198.51.100.10", None, latency=10.0, is_target=True)
    target_two = _snapshot(0, "203.0.113.10", None, latency=12.0, is_target=True)

    try:
        window.target_input.setText("198.51.100.10\n203.0.113.10")
        window.refresh_trace_targets()
        window.start_measurement()
        worker = created_workers[0]
        window.on_measurement_updated(
            [],
            target_one,
            [target_one, target_two],
            ["live"],
            [HopObservation(now, 0, "198.51.100.10", "Target", True, 10.0, STATUS_OK, True)],
            [HopObservation(now, 0, "198.51.100.10", "Target", True, 10.0, STATUS_OK, True)],
        )

        window.target_table.selectRow(1)
        window.pause_selected_targets()
        window.resume_selected_targets()
        window.pause_all_targets()
        window.resume_all_targets()
        window.interval_combo.setCurrentText("5")
        window.apply_runtime_interval()

        assert worker.paused_calls == [["203.0.113.10"], ["198.51.100.10", "203.0.113.10"]]
        assert worker.resumed_calls == [["203.0.113.10"], ["198.51.100.10", "203.0.113.10"]]
        assert worker.interval_updates == [5]
        assert window.apply_interval_button.isEnabled() is True
    finally:
        window.close()


def test_main_window_passes_final_hop_only_mode(qt_app) -> None:
    created_workers: list[_FakeWorker] = []

    def worker_factory(
        target: str,
        interval_seconds: int,
        max_cycles: int | None,
        targets: list[str],
        measurement_mode: str,
        probe_engine: str = "icmp",
        tcp_port: int = 443,
    ) -> "_FakeWorker":
        worker = _FakeWorker(
            target=target,
            interval_seconds=interval_seconds,
            max_cycles=max_cycles,
            targets=targets,
            measurement_mode=measurement_mode,
            probe_engine=probe_engine,
            tcp_port=tcp_port,
        )
        created_workers.append(worker)
        return worker

    window = MainWindow(worker_factory=worker_factory)

    try:
        mode_index = window.measurement_mode_combo.findData(MEASUREMENT_MODE_FINAL_HOP_ONLY)
        assert mode_index >= 0
        window.measurement_mode_combo.setCurrentIndex(mode_index)
        window.target_input.setText("8.8.8.8\n1.1.1.1")
        window.refresh_trace_targets()

        window.start_measurement()

        assert len(created_workers) == 1
        assert created_workers[0].measurement_mode == MEASUREMENT_MODE_FINAL_HOP_ONLY
        assert window.measurement_mode_combo.isEnabled() is False
    finally:
        window.close()


def test_main_window_confirms_and_limits_many_targets(qt_app, monkeypatch) -> None:
    created_workers: list[_FakeWorker] = []
    questions: list[str] = []

    def worker_factory(
        target: str,
        interval_seconds: int,
        max_cycles: int | None,
        targets: list[str],
        measurement_mode: str,
        probe_engine: str = "icmp",
        tcp_port: int = 443,
    ) -> "_FakeWorker":
        worker = _FakeWorker(
            target=target,
            interval_seconds=interval_seconds,
            max_cycles=max_cycles,
            targets=targets,
            measurement_mode=measurement_mode,
            probe_engine=probe_engine,
            tcp_port=tcp_port,
        )
        created_workers.append(worker)
        return worker

    def answer_yes(parent, title, text, buttons, default_button):
        questions.append(text)
        return QMessageBox.Yes

    monkeypatch.setattr(QMessageBox, "question", answer_yes)
    window = MainWindow(worker_factory=worker_factory)

    try:
        window.target_input.setText("\n".join(f"10.0.0.{index}" for index in range(1, 56)))
        window.start_measurement()

        assert questions
        assert len(created_workers) == 1
        assert len(created_workers[0].targets) == 50
        assert created_workers[0].targets[0] == "10.0.0.1"
        assert created_workers[0].targets[-1] == "10.0.0.50"
    finally:
        window.close()


def test_main_window_renders_trace_metrics_and_exports(qt_app) -> None:
    window = MainWindow()
    now = datetime.now()
    hops = [
        HopInfo(index=1, address="192.0.2.1", hostname="gateway"),
        HopInfo(index=2, address="198.51.100.10", hostname=None, is_target=True),
    ]
    snapshots = [
        _snapshot(1, "192.0.2.1", "gateway", latency=2.0),
        _snapshot(
            2,
            "198.51.100.10",
            "198.51.100.10",
            loss=25.0,
            latency=None,
            received=0,
            timeout_count=1,
            status=STATUS_TIMEOUT,
            is_target=True,
        ),
    ]
    target_snapshot = _snapshot(
        0,
        "198.51.100.10",
        None,
        loss=25.0,
        latency=None,
        received=0,
        timeout_count=1,
        status=STATUS_TIMEOUT,
        is_target=True,
    )
    target_snapshots = [
        target_snapshot,
        _snapshot(0, "192.168.0.1", None, latency=1.0, is_target=True),
    ]
    observations = [
        HopObservation(now, 1, "192.0.2.1", "gateway", True, 2.0, STATUS_OK),
        HopObservation(now, 2, "198.51.100.10", None, False, None, STATUS_TIMEOUT, True),
    ]

    try:
        window.on_trace_completed(hops)
        window.on_measurement_updated(
            snapshots,
            target_snapshot,
            target_snapshots,
            ["최종 대상 구간 손실 가능성이 있습니다."],
            observations,
            observations[-1:],
        )

        assert window.table.rowCount() == 2
        assert window.table.item(1, 3).text() == "CRITICAL"
        assert window.table.item(1, 8).text() == "25.0"
        assert window.target_table.rowCount() == 2
        assert window.target_table.item(1, 0).text() == "192.168.0.1"
        assert window.metric_value_labels["loss"].text() == "25.0%"
        assert window.metric_value_labels["samples"].text() == "1"
        assert "가능성" in window.analysis_box.toPlainText()
        assert window.csv_button.isEnabled() is True
        assert window.xlsx_button.isEnabled() is True
        assert window.report_button.isEnabled() is True
        assert window.stats_csv_button.isEnabled() is True
        assert window.stats_xlsx_button.isEnabled() is True
    finally:
        window.close()


def test_main_window_problem_sort_prioritizes_worst_target(qt_app) -> None:
    window = MainWindow()
    now = datetime.now()
    healthy = _snapshot(0, "198.51.100.10", None, latency=10.0, is_target=True)
    critical = _snapshot(
        0,
        "203.0.113.10",
        None,
        loss=40.0,
        latency=None,
        received=0,
        timeout_count=1,
        status=STATUS_TIMEOUT,
        is_target=True,
    )

    try:
        window.current_target = "198.51.100.10"
        window.current_targets = ["198.51.100.10", "203.0.113.10"]
        window.on_measurement_updated(
            [],
            healthy,
            [healthy, critical],
            ["live"],
            [
                HopObservation(now, 0, "198.51.100.10", "Target", True, 10.0, STATUS_OK, True),
                HopObservation(now, 0, "203.0.113.10", "Target", False, None, STATUS_TIMEOUT, True),
            ],
            [],
        )

        assert window.target_table.item(0, 0).text() == "198.51.100.10"

        window.problem_sort_check.setChecked(True)

        assert window.target_table.item(0, 0).text() == "203.0.113.10"
    finally:
        window.close()


def test_main_window_target_double_click_switches_summary_target(qt_app) -> None:
    window = MainWindow()
    now = datetime.now()
    first = _snapshot(0, "198.51.100.10", None, latency=10.0, is_target=True)
    second = _snapshot(0, "203.0.113.10", None, latency=22.0, is_target=True)

    try:
        window.target_input.setText("198.51.100.10\n203.0.113.10")
        window.refresh_trace_targets()
        window.current_target = "198.51.100.10"
        window.current_targets = ["198.51.100.10", "203.0.113.10"]
        window.on_measurement_updated(
            [],
            first,
            [first, second],
            ["live"],
            [
                HopObservation(now, 0, "198.51.100.10", "Target", True, 10.0, STATUS_OK, True),
                HopObservation(now, 0, "203.0.113.10", "Target", True, 22.0, STATUS_OK, True),
            ],
            [],
        )

        window.on_target_double_clicked(1, 0)

        assert window.current_target == "203.0.113.10"
        assert window.trace_target_combo.currentText() == "203.0.113.10"
        assert window.target_snapshot is second
        assert window.graph._points[0].address == "203.0.113.10"
        assert "Summary target selected" in window.status_label.text()
    finally:
        window.close()


def test_main_window_starts_statistics_export_with_selected_options(qt_app, tmp_path, monkeypatch) -> None:
    created_workers: list[_FakeExportWorker] = []
    export_path = tmp_path / "statistics.csv"

    def fake_get_save_file_name(*_args, **_kwargs):
        return str(export_path), "CSV Files (*.csv)"

    def fake_export_worker(**kwargs):
        worker = _FakeExportWorker(**kwargs)
        created_workers.append(worker)
        return worker

    monkeypatch.setattr(main_window_module.QFileDialog, "getSaveFileName", fake_get_save_file_name)
    monkeypatch.setattr(main_window_module, "ExportWorker", fake_export_worker)
    window = MainWindow()
    now = datetime.now()

    try:
        window.current_target = "198.51.100.10"
        window.session_log_path = tmp_path / "session.csv"
        window.timeline_range = (now - timedelta(minutes=10), now)
        window.observations = [
            HopObservation(now, 0, "198.51.100.10", "Target", True, 1.0, STATUS_OK, True)
        ]
        group_index = window.statistics_group_combo.findData(3600)
        timezone_index = window.statistics_timezone_combo.findData(TIMEZONE_UTC)
        scope_index = window.statistics_scope_combo.findData(STATISTICS_SCOPE_VISIBLE)
        assert group_index >= 0
        assert timezone_index >= 0
        assert scope_index >= 0
        window.statistics_group_combo.setCurrentIndex(group_index)
        window.statistics_timezone_combo.setCurrentIndex(timezone_index)
        window.statistics_scope_combo.setCurrentIndex(scope_index)

        window.save_statistics_csv()

        assert len(created_workers) == 1
        worker = created_workers[0]
        assert worker.kwargs["kind"] == "stats_csv"
        assert worker.kwargs["path"] == export_path
        assert worker.kwargs["target"] == "198.51.100.10"
        assert worker.kwargs["focus_range"] == window.timeline_range
        assert worker.kwargs["observations_override"] is None
        assert worker.kwargs["statistics_options"].grouping_seconds == 3600
        assert worker.kwargs["statistics_options"].timezone_mode == TIMEZONE_UTC
        assert worker.started is True
        assert window.stats_csv_button.isEnabled() is False
        assert window.statistics_scope_combo.isEnabled() is False
        assert window.statistics_group_combo.isEnabled() is False
    finally:
        window.close()


def test_main_window_statistics_focus_scope_uses_live_buffer_override(qt_app, tmp_path, monkeypatch) -> None:
    created_workers: list[_FakeExportWorker] = []
    export_path = tmp_path / "statistics.csv"

    def fake_get_save_file_name(*_args, **_kwargs):
        return str(export_path), "CSV Files (*.csv)"

    def fake_export_worker(**kwargs):
        worker = _FakeExportWorker(**kwargs)
        created_workers.append(worker)
        return worker

    monkeypatch.setattr(main_window_module.QFileDialog, "getSaveFileName", fake_get_save_file_name)
    monkeypatch.setattr(main_window_module, "ExportWorker", fake_export_worker)
    window = MainWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)

    try:
        window.current_target = "198.51.100.10"
        window.observations = [
            HopObservation(now, 0, "198.51.100.10", "Target", True, 1.0, STATUS_OK, True),
            HopObservation(now + timedelta(seconds=30), 0, "198.51.100.10", "Target", True, 2.0, STATUS_OK, True),
            HopObservation(now + timedelta(seconds=90), 0, "198.51.100.10", "Target", True, 3.0, STATUS_OK, True),
        ]
        window.apply_focus_range((now, now + timedelta(seconds=60)))
        scope_index = window.statistics_scope_combo.findData(STATISTICS_SCOPE_FOCUS)
        assert scope_index >= 0
        window.statistics_scope_combo.setCurrentIndex(scope_index)

        window.save_statistics_csv()

        assert len(created_workers) == 1
        worker = created_workers[0]
        assert worker.kwargs["focus_range"] == window.focus_range
        assert [point.latency_ms for point in worker.kwargs["observations_override"]] == [1.0, 2.0]
    finally:
        window.close()


def test_main_window_graph_detail_window_receives_live_metrics(qt_app) -> None:
    window = MainWindow()
    now = datetime.now()
    target_snapshot = _snapshot(
        0,
        "198.51.100.10",
        None,
        latency=21.5,
        is_target=True,
    )
    history = [
        HopObservation(now, 0, "198.51.100.10", "Target", True, 21.5, STATUS_OK, True),
    ]

    try:
        window.current_target = "198.51.100.10"
        window.open_graph_detail()

        assert window.graph_detail_window is not None
        assert window.graph_detail_window.isVisible() is True

        window.on_measurement_updated([], target_snapshot, [target_snapshot], ["정상"], history, history)
        detail = window.graph_detail_window

        assert detail.target_label.text() == "대상: 198.51.100.10"
        assert detail.metric_value_labels["current"].text() == "21.5 ms"
        assert detail.metric_value_labels["loss"].text() == "0.0%"
        assert detail.metric_value_labels["samples"].text() == "1"
        assert len(detail.graph._points) == 1

        detail.close()
        assert detail.isVisible() is False
        window.open_graph_detail()

        assert window.graph_detail_window is detail
        assert detail.isVisible() is True
        assert detail.metric_value_labels["samples"].text() == "1"
    finally:
        window.close()


def test_main_window_hop_selection_drives_graph_detail_selected_hop(qt_app) -> None:
    window = MainWindow()
    now = datetime.now()
    hops = [
        HopInfo(index=1, address="192.0.2.1", hostname="gateway"),
        HopInfo(index=2, address="198.51.100.10", hostname="target", is_target=True),
    ]
    snapshots = [
        _snapshot(1, "192.0.2.1", "gateway", latency=2.0),
        _snapshot(2, "198.51.100.10", "target", latency=20.0, is_target=True),
    ]
    target_snapshot = _snapshot(0, "198.51.100.10", None, latency=20.0, is_target=True)
    observations = [
        HopObservation(now, 1, "192.0.2.1", "gateway", True, 2.0, STATUS_OK),
        HopObservation(now, 2, "198.51.100.10", "target", True, 20.0, STATUS_OK, True),
    ]

    try:
        window.current_target = "198.51.100.10"
        window.open_graph_detail()
        window.on_trace_completed(hops)
        window.on_measurement_updated(snapshots, target_snapshot, [target_snapshot], ["정상"], observations, observations[-1:])
        window.table.selectRow(0)
        window.on_hop_selection_changed()

        assert window.selected_hop_index == 1
        assert window.graph_detail_window.view_combo.currentData() == VIEW_SELECTED_HOP
        assert [series.key for series in window.graph_detail_window.graph._series] == ["hop-1"]
    finally:
        window.close()


def test_main_window_hop_double_click_toggles_detail_timeline(qt_app) -> None:
    window = MainWindow()
    now = datetime.now()
    hops = [
        HopInfo(index=1, address="192.0.2.1", hostname="gateway"),
        HopInfo(index=2, address="198.51.100.10", hostname="target", is_target=True),
    ]
    snapshots = [
        _snapshot(1, "192.0.2.1", "gateway", latency=2.0),
        _snapshot(2, "198.51.100.10", "target", latency=20.0, is_target=True),
    ]
    target_snapshot = _snapshot(0, "198.51.100.10", None, latency=20.0, is_target=True)
    observations = [
        HopObservation(now, 1, "192.0.2.1", "gateway", True, 2.0, STATUS_OK),
        HopObservation(now, 2, "198.51.100.10", "target", True, 20.0, STATUS_OK, True),
    ]

    try:
        window.current_target = "198.51.100.10"
        window.on_trace_completed(hops)
        window.on_measurement_updated(snapshots, target_snapshot, [target_snapshot], ["정상"], observations, observations[-1:])

        window.on_hop_double_clicked(1, 0)

        assert window.graph_detail_window is not None
        assert window.graph_detail_window.view_combo.currentData() == VIEW_VISIBLE_HOPS
        assert [series.key for series in window.graph_detail_window.graph._series] == ["hop-2"]

        window.on_hop_double_clicked(1, 0)

        assert window.graph_detail_window.graph._series == []
    finally:
        window.close()


def test_main_window_renders_worker_diagnostics(qt_app) -> None:
    window = MainWindow()

    try:
        window.on_diagnostics_updated(
            SimpleNamespace(
                active_ping_count=7,
                pending_ping_count=3,
                timeout_target_count=2,
                backoff_target_count=1,
                log_queue_depth=4,
                average_loop_delay_ms=12.5,
                last_update_iso="2026-06-18T17:10:00",
                tracert_status="done",
                target_probe_engine="TCP Connect:8443",
                route_probe_engine="tracert/ICMP",
                tcp_port=8443,
            )
        )

        text = window.diagnostics_box.toPlainText()
        assert "target probe: TCP Connect:8443" in text
        assert "route probe: tracert/ICMP" in text
        assert "tcp port: 8443" in text
        assert "active ping: 7" in text
        assert "pending ping: 3" in text
        assert "backoff targets: 1" in text
        assert "avg loop delay: 12.5 ms" in text
        assert "tracert: done" in text
    finally:
        window.close()


def test_main_window_probe_engine_note_and_tcp_port_state(qt_app) -> None:
    window = MainWindow()

    try:
        assert window.tcp_port_spin.isEnabled() is False
        assert "ICMP uses Windows ICMP echo" in window.engine_note_label.text()

        probe_index = window.probe_engine_combo.findData(PROBE_ENGINE_TCP_CONNECT)
        window.probe_engine_combo.setCurrentIndex(probe_index)

        assert window.tcp_port_spin.isEnabled() is True
        assert "TCP Connect measures the final target service port" in window.engine_note_label.text()
        assert "tracert/ICMP" in window.engine_note_label.text()
    finally:
        window.close()


def test_main_window_start_resets_open_graph_detail(qt_app) -> None:
    created_workers: list[_FakeWorker] = []

    def worker_factory(
        target: str,
        interval_seconds: int,
        max_cycles: int | None,
        targets: list[str],
        measurement_mode: str,
        probe_engine: str = "icmp",
        tcp_port: int = 443,
    ) -> "_FakeWorker":
        worker = _FakeWorker(
            target=target,
            interval_seconds=interval_seconds,
            max_cycles=max_cycles,
            targets=targets,
            measurement_mode=measurement_mode,
            probe_engine=probe_engine,
            tcp_port=tcp_port,
        )
        created_workers.append(worker)
        return worker

    window = MainWindow(worker_factory=worker_factory)
    now = datetime.now()
    target_snapshot = _snapshot(0, "198.51.100.10", None, latency=10.0, is_target=True)
    history = [HopObservation(now, 0, "198.51.100.10", "Target", True, 10.0, STATUS_OK, True)]

    try:
        window.current_target = "198.51.100.10"
        window.open_graph_detail()
        window.on_measurement_updated([], target_snapshot, [target_snapshot], ["정상"], history, history)
        assert window.graph_detail_window.metric_value_labels["samples"].text() == "1"

        window.target_input.setText("8.8.8.8")
        window.start_measurement()

        assert created_workers
        assert window.graph_detail_window.target_label.text() == "대상: 8.8.8.8"
        assert window.graph_detail_window.metric_value_labels["samples"].text() == "0"
        assert window.graph_detail_window.graph._points == []
    finally:
        window.close()


def test_main_window_focus_range_recalculates_tables_analysis_and_export(qt_app) -> None:
    window = MainWindow()
    now = datetime.now()
    hops = [HopInfo(index=1, address="192.0.2.1", hostname="gateway")]
    observations = [
        HopObservation(now, 1, "192.0.2.1", "gateway", True, 2.0, STATUS_OK),
        HopObservation(now + timedelta(seconds=1), 1, "192.0.2.1", "gateway", False, None, STATUS_TIMEOUT),
        HopObservation(now + timedelta(seconds=2), 0, "198.51.100.10", "Target", True, 20.0, STATUS_OK, True),
        HopObservation(now + timedelta(seconds=3), 0, "198.51.100.10", "Target", False, None, STATUS_TIMEOUT, True),
        HopObservation(now + timedelta(seconds=20), 1, "192.0.2.1", "gateway", True, 4.0, STATUS_OK),
    ]
    live_snapshot = _snapshot(1, "192.0.2.1", "gateway", loss=0.0, latency=4.0)
    live_target = _snapshot(0, "198.51.100.10", None, loss=0.0, latency=20.0, is_target=True)

    try:
        window.current_target = "198.51.100.10"
        window.on_trace_completed(hops)
        window.on_measurement_updated([live_snapshot], live_target, [live_target], ["live"], observations, observations[2:4])
        window.apply_focus_range((now, now + timedelta(seconds=3)))

        assert window.focus_range is not None
        assert window.table.item(0, 8).text() == "50.0"
        assert window.table.item(0, 12).text() == "2"
        assert window.target_table.item(0, 0).text() == "198.51.100.10"
        assert window.target_table.item(0, 6).text() == "50.0"
        assert window.metric_value_labels["loss"].text() == "50.0%"
        assert window.analysis_for_export()[0].startswith("Focus period:")
        assert window.snapshots_for_export()[0].loss_percent == 50.0

        window.clear_focus_range()

        assert window.focus_range is None
        assert window.focus_label.text() == "Live"
        assert window.table.item(0, 8).text() == "0.0"
    finally:
        window.close()


def test_main_window_loads_graph_timeline_from_session_log_and_focuses_it(qt_app, tmp_path) -> None:
    window = MainWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    session_path = tmp_path / "session.csv"
    writer = SessionLogWriter(session_path)
    writer.write_many([
        HopObservation(now, 1, "192.0.2.1", "gateway", True, 2.0, STATUS_OK),
        HopObservation(now + timedelta(seconds=1), 0, "198.51.100.10", "Target", True, 20.0, STATUS_OK, True),
        HopObservation(now + timedelta(seconds=2), 1, "192.0.2.1", "gateway", False, None, STATUS_TIMEOUT),
        HopObservation(now + timedelta(seconds=3), 0, "198.51.100.10", "Target", False, None, STATUS_TIMEOUT, True),
    ])
    writer.close()

    try:
        window.current_target = "198.51.100.10"
        window.session_log_path = session_path
        window.on_trace_completed([HopInfo(index=1, address="192.0.2.1", hostname="gateway")])
        window.open_graph_detail()
        window.load_timeline_range(600)

        assert window.timeline_range is not None
        assert len(window.timeline_observations) == 4
        assert len(window.graph_detail_window.graph._series[0].points) == 2
        assert "10m" in window.graph_detail_window.timeline_status_label.text()

        window.apply_focus_range((now, now + timedelta(seconds=3)))

        assert window.table.item(0, 8).text() == "50.0"
        assert window.target_table.item(0, 6).text() == "50.0"
        assert window.analysis_for_export()[0].startswith("Focus period:")
    finally:
        window.close()


def test_main_window_displays_route_change_history_and_graph_marker(qt_app) -> None:
    window = MainWindow()
    history = RouteHistory()
    now = datetime(2026, 1, 1, 12, 0, 0)
    history.record(
        [
            HopInfo(index=1, address="192.0.2.1", hostname="gateway"),
            HopInfo(index=2, address="198.51.100.10", hostname="target", is_target=True),
        ],
        now,
    )
    change = history.record(
        [
            HopInfo(index=1, address="192.0.2.254", hostname="backup"),
            HopInfo(index=2, address="198.51.100.10", hostname="target", is_target=True),
        ],
        now + timedelta(seconds=60),
    )
    target_snapshot = _snapshot(0, "198.51.100.10", None, latency=10.0, is_target=True)
    target_history = [
        HopObservation(now, 0, "198.51.100.10", "Target", True, 10.0, STATUS_OK, True),
        HopObservation(now + timedelta(seconds=60), 0, "198.51.100.10", "Target", True, 12.0, STATUS_OK, True),
    ]

    try:
        assert change is not None
        window.current_target = "198.51.100.10"
        window.on_measurement_updated([], target_snapshot, [target_snapshot], ["live"], target_history, target_history)
        window.open_graph_detail()
        window.on_route_changed(change)

        assert "Hop 1" in window.route_changes_box.toPlainText()
        assert "Impact: before loss 0.0% avg 10.0 ms" in window.route_changes_box.toPlainText()
        assert "after loss 0.0% avg 12.0 ms" in window.route_changes_box.toPlainText()
        assert "Route changed" in window.alerts_box.toPlainText()
        assert len(window.graph._annotations) == 1
        assert window.graph._annotations[0].label == "Route changed"
        assert len(window.graph_detail_window.graph._annotations) == 1
    finally:
        window.close()


def test_main_window_loads_persisted_route_changes_with_timeline_range(qt_app, tmp_path) -> None:
    window = MainWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    session_path = tmp_path / "session.samples.csv"
    writer = SessionLogWriter(session_path)
    writer.write_many([
        HopObservation(now, 0, "198.51.100.10", "Target", True, 10.0, STATUS_OK, True),
        HopObservation(now + timedelta(seconds=60), 0, "198.51.100.10", "Target", True, 12.0, STATUS_OK, True),
    ])
    writer.close()

    history = RouteHistory()
    history.record(
        [
            HopInfo(index=1, address="192.0.2.1", hostname="gateway"),
            HopInfo(index=2, address="198.51.100.10", hostname="target", is_target=True),
        ],
        now,
    )
    change = history.record(
        [
            HopInfo(index=1, address="192.0.2.254", hostname="backup"),
            HopInfo(index=2, address="198.51.100.10", hostname="target", is_target=True),
        ],
        now + timedelta(seconds=60),
    )
    assert change is not None
    with RouteLogWriter(route_log_path_for_session(session_path)) as route_writer:
        route_writer.write_snapshot(history.snapshots[0])
        route_writer.write_snapshot(history.snapshots[1], change)

    try:
        window.current_target = "198.51.100.10"
        window.session_log_path = session_path
        window.route_log_path = route_log_path_for_session(session_path)
        window.open_graph_detail()

        window.load_timeline_range(600)

        assert "Hop 1" in window.route_changes_box.toPlainText()
        assert "Impact: before loss 0.0% avg 10.0 ms" in window.route_changes_box.toPlainText()
        assert "after loss 0.0% avg 12.0 ms" in window.route_changes_box.toPlainText()
        assert "Route changed" in window.alerts_box.toPlainText()
        assert [annotation.label for annotation in window.graph_detail_window.graph._annotations] == [
            "Route changed"
        ]
        assert window.annotations_for_export()[0].source == "route"
    finally:
        window.close()


def test_main_window_records_metric_alerts_and_graph_annotations(qt_app) -> None:
    window = MainWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    history = [
        HopObservation(now, 0, "198.51.100.10", "Target", True, 20.0, STATUS_OK, True),
        HopObservation(now + timedelta(seconds=45), 0, "198.51.100.10", "Target", False, None, STATUS_TIMEOUT, True),
        HopObservation(now + timedelta(seconds=90), 0, "198.51.100.10", "Target", True, 30.0, STATUS_OK, True),
        HopObservation(now + timedelta(seconds=135), 0, "198.51.100.10", "Target", True, 40.0, STATUS_OK, True),
        HopObservation(now + timedelta(seconds=180), 0, "198.51.100.10", "Target", True, 125.0, STATUS_OK, True),
    ]
    target_snapshot = _snapshot(0, "198.51.100.10", None, latency=125.0, is_target=True)

    try:
        window.current_target = "198.51.100.10"
        window.open_graph_detail()
        window.on_measurement_updated([], target_snapshot, [target_snapshot], ["live"], history, history)

        text = window.alerts_box.toPlainText()
        assert "Loss alert" in text
        assert "Latency alert" in text
        assert [annotation.label for annotation in window.graph._annotations] == ["Loss alert", "Latency alert"]
        assert [annotation.label for annotation in window.graph_detail_window.graph._annotations] == [
            "Loss alert",
            "Latency alert",
        ]

        window.on_measurement_updated([], target_snapshot, [target_snapshot], ["live"], history, history)

        assert len(window.alert_events) == 2
    finally:
        window.close()


def test_main_window_custom_alert_rules_write_action_log(qt_app, tmp_path) -> None:
    window = MainWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    history = [
        HopObservation(now, 0, "198.51.100.10", "Target", True, 20.0, STATUS_OK, True),
        HopObservation(now + timedelta(seconds=30), 0, "198.51.100.10", "Target", False, None, STATUS_TIMEOUT, True),
        HopObservation(now + timedelta(seconds=60), 0, "198.51.100.10", "Target", True, 90.0, STATUS_OK, True),
    ]
    target_snapshot = _snapshot(0, "198.51.100.10", None, latency=90.0, is_target=True)

    try:
        window.current_target = "198.51.100.10"
        window.alert_action_log_path = tmp_path / "session.alerts.csv"
        window.loss_threshold_spin.setValue(30)
        window.loss_window_spin.setValue(1)
        window.latency_threshold_spin.setValue(80)

        window.on_measurement_updated([], target_snapshot, [target_snapshot], ["live"], history, history)

        text = window.alerts_box.toPlainText()
        rows = read_alert_actions(window.alert_action_log_path)
        assert "Loss alert" in text
        assert "Latency alert" in text
        assert [row["title"] for row in rows] == ["Loss alert", "Latency alert"]
        assert all(row["actions"] == "timeline_annotation;comment" for row in rows)
    finally:
        window.close()


def test_main_window_alert_action_selection_controls_log_beep_and_timeline(qt_app, tmp_path, monkeypatch) -> None:
    beep_calls: list[bool] = []
    monkeypatch.setattr(main_window_module.QApplication, "beep", lambda: beep_calls.append(True))
    window = MainWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    history = [
        HopObservation(now, 0, "198.51.100.10", "Target", True, 90.0, STATUS_OK, True),
    ]
    target_snapshot = _snapshot(0, "198.51.100.10", None, latency=90.0, is_target=True)

    try:
        window.current_target = "198.51.100.10"
        window.alert_action_log_path = tmp_path / "session.alerts.csv"
        window.loss_threshold_spin.setValue(100)
        window.latency_threshold_spin.setValue(80)
        window.alert_timeline_action_check.setChecked(False)
        window.alert_comment_action_check.setChecked(False)
        window.alert_beep_action_check.setChecked(True)

        window.on_measurement_updated([], target_snapshot, [target_snapshot], ["live"], history, history)

        rows = read_alert_actions(window.alert_action_log_path)
        assert beep_calls == [True]
        assert rows[0]["title"] == "Latency alert"
        assert rows[0]["actions"] == "beep"
        assert "Latency alert" in window.alerts_box.toPlainText()
        assert window.graph._annotations == []
        assert window.annotations_for_export() == []
    finally:
        window.close()


def test_main_window_records_sample_count_alert_and_recovery(qt_app, tmp_path) -> None:
    window = MainWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    bad_history = [
        HopObservation(now, 0, "198.51.100.10", "Target", True, 10.0, STATUS_OK, True),
        HopObservation(now + timedelta(seconds=1), 0, "198.51.100.10", "Target", False, None, STATUS_TIMEOUT, True),
        HopObservation(now + timedelta(seconds=2), 0, "198.51.100.10", "Target", False, None, STATUS_TIMEOUT, True),
    ]
    good_history = [
        HopObservation(now + timedelta(seconds=3), 0, "198.51.100.10", "Target", True, 10.0, STATUS_OK, True),
        HopObservation(now + timedelta(seconds=4), 0, "198.51.100.10", "Target", True, 11.0, STATUS_OK, True),
        HopObservation(now + timedelta(seconds=5), 0, "198.51.100.10", "Target", True, 12.0, STATUS_OK, True),
    ]
    target_snapshot = _snapshot(0, "198.51.100.10", None, latency=12.0, is_target=True)

    try:
        window.current_target = "198.51.100.10"
        window.alert_action_log_path = tmp_path / "session.alerts.csv"
        window.loss_threshold_spin.setValue(100)
        window.loss_window_spin.setValue(1)
        window.latency_threshold_spin.setValue(1000)
        window.sample_window_spin.setValue(3)
        window.sample_bad_spin.setValue(2)

        window.on_measurement_updated([], target_snapshot, [target_snapshot], ["live"], bad_history, bad_history)
        window.on_measurement_updated([], target_snapshot, [target_snapshot], ["live"], good_history, good_history)

        rows = read_alert_actions(window.alert_action_log_path)
        assert [event.title for event in window.alert_events] == ["Sample count alert", "Alert ended"]
        assert "Alert ended" in window.alerts_box.toPlainText()
        assert [row["title"] for row in rows] == ["Sample count alert", "Alert ended"]
        assert rows[1]["message"] == "Sample count alert recovered"
    finally:
        window.close()


def test_main_window_exports_alert_route_and_manual_annotations(qt_app) -> None:
    window = MainWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    target_snapshot = _snapshot(0, "198.51.100.10", None, latency=10.0, is_target=True)
    history = [
        HopObservation(now, 0, "198.51.100.10", "Target", True, 10.0, STATUS_OK, True),
        HopObservation(now + timedelta(seconds=10), 0, "198.51.100.10", "Target", True, 11.0, STATUS_OK, True),
    ]

    try:
        window.current_target = "198.51.100.10"
        window.on_measurement_updated([], target_snapshot, [target_snapshot], ["live"], history, history)
        window.alert_events = [
            AlertEvent(
                key="target_latency_100ms",
                timestamp=now + timedelta(seconds=10),
                start=now + timedelta(seconds=10),
                end=now + timedelta(seconds=10),
                severity="warning",
                title="Latency alert",
                message="Target latency 125.0 ms >= 100 ms",
            ),
            AlertEvent(
                key="route_changed:2026-01-01T12:00:20",
                timestamp=now + timedelta(seconds=20),
                start=now + timedelta(seconds=20),
                end=now + timedelta(seconds=20),
                severity="warning",
                title="Route changed",
                message="changed Hop 1",
                series_key=None,
            ),
            AlertEvent(
                key="outside",
                timestamp=now + timedelta(minutes=10),
                start=now + timedelta(minutes=10),
                end=now + timedelta(minutes=10),
                severity="warning",
                title="Outside",
                message="outside focus",
            ),
        ]
        window.open_graph_detail()
        window.graph_detail_window.graph.select_time_range(now, now + timedelta(seconds=30))
        window.graph_detail_window.annotation_input.setText("operator note")
        window.graph_detail_window.add_annotation_from_selection()
        window.apply_focus_range((now, now + timedelta(seconds=30)))

        annotations = window.annotations_for_export()

        assert [annotation.source for annotation in annotations] == ["alert", "route", "manual"]
        assert [annotation.title for annotation in annotations] == [
            "Latency alert",
            "Route changed",
            "operator note",
        ]
    finally:
        window.close()


def _snapshot(
    hop_index: int,
    address: str,
    hostname: str | None,
    *,
    loss: float = 0.0,
    latency: float | None = 10.0,
    received: int = 1,
    timeout_count: int = 0,
    status: str = STATUS_OK,
    is_target: bool = False,
) -> MetricSnapshot:
    return MetricSnapshot(
        hop_index=hop_index,
        address=address,
        hostname=hostname,
        samples=1,
        sent=1,
        received=received,
        timeout_count=timeout_count,
        current_latency_ms=latency,
        avg_latency_ms=latency,
        min_latency_ms=latency,
        max_latency_ms=latency,
        loss_percent=loss,
        recent_loss_percent=loss,
        jitter_ms=0.0 if latency is not None else None,
        status=status,
        is_target=is_target,
    )


class _FakeWorker(QObject):
    trace_completed = Signal(object)
    measurement_updated = Signal(object, object, object, object, object, object)
    status_message = Signal(str)
    error_message = Signal(str)
    finished = Signal()

    def __init__(
        self,
        target: str,
        interval_seconds: int,
        max_cycles: int | None,
        targets: list[str],
        measurement_mode: str = MEASUREMENT_MODE_FULL_ROUTE,
        probe_engine: str = "icmp",
        tcp_port: int = 443,
    ) -> None:
        super().__init__()
        self.target = target
        self.interval_seconds = interval_seconds
        self.max_cycles = max_cycles
        self.targets = targets
        self.measurement_mode = measurement_mode
        self.probe_engine = probe_engine
        self.tcp_port = tcp_port
        self.started = False
        self.stopped = False
        self.paused_calls: list[list[str]] = []
        self.resumed_calls: list[list[str]] = []
        self.interval_updates: list[int] = []

    def start(self) -> None:
        self.started = True

    def request_stop(self) -> None:
        self.stopped = True

    def pause_targets(self, targets: list[str]) -> None:
        self.paused_calls.append(list(targets))

    def resume_targets(self, targets: list[str]) -> None:
        self.resumed_calls.append(list(targets))

    def set_interval_seconds(self, interval_seconds: int) -> None:
        self.interval_updates.append(interval_seconds)
        self.interval_seconds = interval_seconds

    def isRunning(self) -> bool:
        return self.started and not self.stopped

    def wait(self, timeout_ms: int) -> bool:
        return True


class _FakeSignal:
    def __init__(self) -> None:
        self.callbacks: list[object] = []

    def connect(self, callback) -> None:
        self.callbacks.append(callback)


class _FakeExportWorker:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.started = False
        self.status_message = _FakeSignal()
        self.export_completed = _FakeSignal()
        self.error_message = _FakeSignal()
        self.finished = _FakeSignal()

    def isRunning(self) -> bool:
        return self.started

    def start(self) -> None:
        self.started = True

    def wait(self, timeout_ms: int) -> bool:
        return True
