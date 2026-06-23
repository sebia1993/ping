from __future__ import annotations

import csv
import json
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication, QMessageBox

from app.core.alerts import AlertEvent
from app.core.models import STATUS_OK, STATUS_TIMEOUT, HopInfo, HopObservation, MetricSnapshot
from app.core.route_history import RouteHistory
from app.storage.alert_action_log import append_alert_action, alert_action_log_path_for_session, read_alert_actions
from app.storage.route_log import RouteLogWriter, route_log_path_for_session
from app.storage.session_index import (
    SESSION_STATE_ARCHIVED,
    SESSION_STATE_PAUSED,
    SESSION_STATE_WILL_DELETE,
    SessionIndexStore,
)
from app.storage.session_log import SessionLogWriter
from app.storage.statistics_exporter import TIMEZONE_UTC
from app.ui import main_window as main_window_module
from app.ui.graph_detail_window import VIEW_SELECTED_HOP, VIEW_VISIBLE_HOPS
from app.ui.main_window import (
    ALERT_RULE_PRESET_VERSION,
    ALERT_HEADERS,
    GRAPH_PNG_SCOPE_BOTH,
    GRAPH_PNG_SCOPE_TIMELINE,
    GRAPH_PNG_SCOPE_TRACE,
    MainWindow,
    SESSION_HEADERS,
    SESSION_ID_ROLE,
    STATISTICS_SCOPE_CUSTOM,
    STATISTICS_SCOPE_FOCUS,
    STATISTICS_SCOPE_VISIBLE,
    TABLE_HEADERS,
    TARGET_GROUP_PRESET_VERSION,
    TARGET_HEADERS,
)
from app.ui.worker import (
    MEASUREMENT_MODE_FINAL_HOP_ONLY,
    MEASUREMENT_MODE_FULL_ROUTE,
    PROBE_ENGINE_ICMP,
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
        assert window.session_table.columnCount() == len(SESSION_HEADERS)
        assert window.alert_table.columnCount() == len(ALERT_HEADERS)
        assert window.alert_table.rowCount() == 0
        assert window.windowTitle() == "네트워크 경로 진단"
        view_actions = [
            action.text()
            for menu_action in window.menuBar().actions()
            if menu_action.menu() is not None
            for action in menu_action.menu().actions()
        ]
        assert "고급 기능 표시" not in view_actions
        assert "그래프 확대" in view_actions
        assert window.timeline_label.text() == "Timeline: Live"
        assert window.target_summary_status_label.text() == "IP: 0"
        assert window.target_filter_edit.text() == ""
        assert window.target_status_filter_combo.currentData() == ""
        assert window.target_panel_expanded is False
        assert window.target_table.isHidden() is True
        assert window.toggle_target_panel_button.text() == "IP 현황 보기"
        assert window.advanced_features_visible is False
        assert window.advanced_controls_panel.isHidden() is True
        assert window.target_advanced_controls_panel.isHidden() is True
        assert window.hop_table_panel.isHidden() is True
        assert window.right_panel.isHidden() is True
        assert window.graph_advanced_controls.isHidden() is True
        assert window.footer_panel.isHidden() is True
        assert window.target_table.isColumnHidden(TARGET_HEADERS.index("평균")) is True
        assert window.target_table.isColumnHidden(TARGET_HEADERS.index("샘플")) is False
        assert window.start_button.text() == "시작"
        assert window.stop_button.text() == "중지"
        assert window.save_target_group_button.text() == "그룹 저장"
        assert window.save_selected_target_group_button.text() == "선택 저장"
        assert window.load_target_group_button.text() == "그룹 불러오기"
        assert window.csv_button.isEnabled() is False
        assert window.xlsx_button.isEnabled() is False
        assert window.report_button.isEnabled() is False
        assert window.report_format_combo.currentData() == "txt"
        assert window.report_format_combo.isEnabled() is False
        assert window.graph_png_button.isEnabled() is False
        assert window.stats_csv_button.isEnabled() is False
        assert window.stats_xlsx_button.isEnabled() is False
        assert window.export_target_summary_button.isEnabled() is False
        assert window.graph_png_scope_combo.currentData() == GRAPH_PNG_SCOPE_TIMELINE
        assert window.graph_png_scope_combo.isEnabled() is False
        assert window.loss_alert_check.isChecked() is True
        assert window.latency_alert_check.isChecked() is True
        assert window.jitter_alert_check.isChecked() is True
        assert window.sample_alert_check.isChecked() is True
        assert window.timer_alert_check.isChecked() is True
        assert window.alert_start_action_check.isChecked() is True
        assert window.alert_end_action_check.isChecked() is True
        assert window.alert_route_adjust_action_check.isChecked() is False
        assert window.alert_timeline_action_check.isChecked() is True
        assert window.alert_comment_action_check.isChecked() is True
        assert window.alert_log_action_check.isChecked() is False
        assert window.alert_beep_action_check.isChecked() is False
        assert window.alert_image_action_check.isChecked() is False
        assert window.alert_email_action_check.isChecked() is False
        assert window.alert_email_server_edit.text() == ""
        assert window.alert_email_to_edit.text() == ""
        assert window.alert_email_from_edit.text() == ""
        assert window.alert_email_security_combo.currentData() == main_window_module.ALERT_EMAIL_SECURITY_PLAIN
        assert window.alert_email_user_edit.text() == ""
        assert window.alert_email_password_env_edit.text() == ""
        assert window.alert_rest_action_check.isChecked() is False
        assert window.alert_rest_url_edit.text() == ""
        assert window.alert_executable_action_check.isChecked() is False
        assert window.alert_executable_path_edit.text() == ""
        assert window.save_alert_preset_button.text() == "프리셋 저장"
        assert window.load_alert_preset_button.text() == "프리셋 불러오기"
        assert window.jitter_threshold_spin.value() == 30
        assert window.mos_alert_check.isChecked() is False
        assert window.mos_threshold_spin.value() == 3.5
        assert window.mos_window_spin.value() == 5
        assert window.route_ip_alert_check.isChecked() is False
        assert window.route_ip_alert_edit.text() == ""
        assert window.timer_window_spin.value() == 5
        assert window.session_filter_edit.text() == ""
        assert window.session_retention_days_spin.value() == 90
        assert window.statistics_start_edit.isEnabled() is False
        assert window.statistics_end_edit.isEnabled() is False
        assert window.export_session_button.isEnabled() == (window.session_combo.count() > 0)
        assert window.graph_detail_button.text() == "그래프 확대"
        window.set_advanced_features_visible(True)
        assert window.advanced_features_visible is False
        assert window.right_panel.isHidden() is True
        assert window.target_table.isHidden() is True
        assert window.target_advanced_controls_panel.isHidden() is True
        window.toggle_target_panel()
        assert window.target_panel_expanded is True
        assert window.target_table.isHidden() is False
        assert window.target_advanced_controls_panel.isHidden() is True
        assert window.toggle_target_panel_button.text() == "IP 현황 접기"
        assert window.target_table.isColumnHidden(TARGET_HEADERS.index("평균")) is True
        assert window.sessions_box.toPlainText()
    finally:
        window.close()


def test_main_window_renders_session_index_summary(qt_app, tmp_path) -> None:
    window = MainWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    store = SessionIndexStore.create(tmp_path)
    sample_path = tmp_path / "198.51.100.10" / "2026-01" / "session.samples.csv"
    sample_path.parent.mkdir(parents=True)
    sample_path.write_text("header\n", encoding="utf-8")
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
        assert "Storage: targets 1 | target-month buckets 1 | segments 1" in text
        assert "198.51.100.10" in text
        assert "samples 12" in text
        assert "Full Route / ICMP" in text
        assert window.session_combo.count() == 1
        assert window.open_session_button.isEnabled() is True
        assert window.export_session_button.isEnabled() is True
    finally:
        window.close()


def test_main_window_renders_and_selects_session_table_rows(qt_app, tmp_path) -> None:
    window = MainWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    store = SessionIndexStore.create(tmp_path)
    first_path = tmp_path / "198.51.100.10" / "2026-01" / "first.samples.csv"
    second_path = tmp_path / "203.0.113.10" / "2026-01" / "second.samples.csv"
    for path in (first_path, second_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("header\n", encoding="utf-8")
    first = store.register_session(
        target="198.51.100.10",
        sample_path=first_path,
        route_path=first_path.with_name("first.routes.csv"),
        started_at=now,
        interval_seconds=1,
        measurement_mode="full_route",
        target_count=1,
    )
    store.add_samples(first.session_id, 4, now + timedelta(seconds=4), segments=[first_path])
    store.finish_session(first.session_id, state=SESSION_STATE_ARCHIVED, ended_at=now + timedelta(seconds=4))
    second = store.register_session(
        target="203.0.113.10",
        sample_path=second_path,
        route_path=second_path.with_name("second.routes.csv"),
        started_at=now + timedelta(minutes=1),
        interval_seconds=5,
        measurement_mode="final_hop_only:tcp_connect:port443",
        target_count=2,
    )
    store.add_samples(second.session_id, 7, now + timedelta(minutes=1, seconds=7), segments=[second_path])
    store.finish_session(second.session_id, state=SESSION_STATE_ARCHIVED, ended_at=now + timedelta(minutes=1, seconds=7))

    try:
        window.session_index_store = store
        window._sync_sessions_box()

        assert window.session_table.rowCount() == 2
        assert window.session_table.item(0, 1).text() == "203.0.113.10"
        assert window.session_table.item(0, 4).text() == "7"
        assert window.session_table.item(0, 6).text() == "Final Hop Only"
        assert window.session_table.item(0, 7).text() == "TCP Connect"
        assert window.session_table.item(0, 8).text() == "443"
        assert window.session_table.item(0, 0).data(SESSION_ID_ROLE) == second.session_id

        first_row = next(
            row
            for row in range(window.session_table.rowCount())
            if window.session_table.item(row, 0).data(SESSION_ID_ROLE) == first.session_id
        )
        window.session_table.selectRow(first_row)

        assert window.session_combo.currentData() == first.session_id
    finally:
        window.close()


def test_main_window_session_manager_reports_storage_buckets(qt_app, tmp_path) -> None:
    window = MainWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    store = SessionIndexStore.create(tmp_path)
    first_path = tmp_path / "198.51.100.10" / "2026-01" / "multi.samples.csv"
    first_segment = tmp_path / "198.51.100.10" / "2026-02" / "multi.part1.samples.csv"
    second_path = tmp_path / "203.0.113.10" / "2026-02" / "session.samples.csv"
    for path in (first_path, first_segment, second_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("header\n", encoding="utf-8")
    first = store.register_session(
        target="198.51.100.10",
        sample_path=first_path,
        route_path=first_path.with_name("multi.routes.csv"),
        started_at=now,
        interval_seconds=1,
        measurement_mode="full_route",
        target_count=1,
    )
    store.add_samples(first.session_id, 10, now + timedelta(days=32), segments=[first_path, first_segment])
    store.finish_session(first.session_id, state=SESSION_STATE_ARCHIVED, ended_at=now + timedelta(days=32))
    second = store.register_session(
        target="203.0.113.10",
        sample_path=second_path,
        route_path=second_path.with_name("session.routes.csv"),
        started_at=now + timedelta(days=33),
        interval_seconds=1,
        measurement_mode="full_route",
        target_count=1,
    )
    store.add_samples(second.session_id, 4, now + timedelta(days=33, seconds=5), segments=[second_path])
    store.finish_session(second.session_id, state=SESSION_STATE_ARCHIVED, ended_at=now + timedelta(days=33, seconds=5))

    try:
        window.session_index_store = store
        window._sync_sessions_box()

        text = window.sessions_box.toPlainText()
        assert "Storage: targets 2 | target-month buckets 3 | segments 3" in text
        assert "indexed samples 14" in text
        assert "Recent buckets:" in text
        assert "203.0.113.10/2026-02 sessions 1 segments 1 indexed samples 4 states Archived 1" in text
        assert "198.51.100.10/2026-02 sessions 1 segments 1 indexed samples 10 states Archived 1" in text
    finally:
        window.close()


def test_main_window_session_manager_shows_summary_and_latest_display_limit(qt_app, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main_window_module, "SESSION_MANAGER_DISPLAY_LIMIT", 3)
    window = MainWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    store = SessionIndexStore.create(tmp_path)
    records = []
    for index in range(5):
        target = f"198.51.100.{index + 10}"
        sample_path = tmp_path / target / "2026-01" / f"session{index}.samples.csv"
        sample_path.parent.mkdir(parents=True, exist_ok=True)
        sample_path.write_text("header\n", encoding="utf-8")
        record = store.register_session(
            target=target,
            sample_path=sample_path,
            route_path=sample_path.with_name(f"session{index}.routes.csv"),
            started_at=now + timedelta(minutes=index),
            interval_seconds=1,
            measurement_mode="full_route",
            target_count=1,
        )
        store.add_samples(record.session_id, index + 1, now + timedelta(minutes=index, seconds=5), segments=[sample_path])
        store.finish_session(
            record.session_id,
            state=SESSION_STATE_ARCHIVED,
            ended_at=now + timedelta(minutes=index, seconds=5),
        )
        records.append(record)

    try:
        window.session_index_store = store
        window._sync_sessions_box()

        text = window.sessions_box.toPlainText()
        assert text.splitlines()[0] == "Sessions: 5 | showing latest 3 | Archived 5"
        assert window.session_combo.count() == 3
        assert window.session_combo.findData(records[-1].session_id) >= 0
        assert window.session_combo.findData(records[0].session_id) == -1
        assert records[-1].target in text
        assert records[0].target not in text
    finally:
        window.close()


def test_main_window_filters_saved_sessions(qt_app, tmp_path) -> None:
    window = MainWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    store = SessionIndexStore.create(tmp_path)
    archived_path = tmp_path / "198.51.100.10" / "2026-01" / "archived.samples.csv"
    archived_path.parent.mkdir(parents=True)
    archived_path.write_text("header\n", encoding="utf-8")
    archived = store.register_session(
        target="198.51.100.10",
        sample_path=archived_path,
        route_path=archived_path.with_name("archived.routes.csv"),
        started_at=now,
        interval_seconds=1,
        measurement_mode="full_route",
        target_count=1,
    )
    store.finish_session(archived.session_id, state=SESSION_STATE_ARCHIVED, ended_at=now + timedelta(seconds=5))
    missing_path = tmp_path / "203.0.113.10" / "2026-01" / "missing.samples.csv"
    missing = store.register_session(
        target="203.0.113.10",
        sample_path=missing_path,
        route_path=missing_path.with_name("missing.routes.csv"),
        started_at=now + timedelta(minutes=1),
        interval_seconds=10,
        measurement_mode=f"{MEASUREMENT_MODE_FINAL_HOP_ONLY}:{PROBE_ENGINE_TCP_CONNECT}:port443",
        target_count=3,
    )
    store.finish_session(
        missing.session_id,
        state=SESSION_STATE_WILL_DELETE,
        ended_at=now + timedelta(minutes=1, seconds=5),
        last_error="Session log missing: 203.0.113.10",
    )

    try:
        window.session_index_store = store
        window._sync_sessions_box()
        assert window.session_combo.count() == 2

        window.session_filter_edit.setText("203.0.113")
        text = window.sessions_box.toPlainText()
        assert text.splitlines()[0] == "Sessions: 1/2 | Will Delete 1"
        assert "Storage: targets 1 | target-month buckets 1 | segments 1" in text
        assert "203.0.113.10" in text
        assert "198.51.100.10" not in text
        assert window.session_combo.count() == 1
        assert window.session_combo.findData(missing.session_id) == 0

        window.session_filter_edit.setText("tcp_connect port443")
        assert "203.0.113.10" in window.sessions_box.toPlainText()
        assert window.session_combo.count() == 1

        window.session_filter_edit.setText("session log missing")
        assert "203.0.113.10" in window.sessions_box.toPlainText()
        assert window.session_combo.count() == 1

        window.session_filter_edit.setText("target:198.51.100.10 state:archived")
        assert "198.51.100.10" in window.sessions_box.toPlainText()
        assert window.session_combo.count() == 1
        assert window.session_combo.findData(archived.session_id) == 0

        window.session_filter_edit.setText("bucket:203.0.113.10/2026-01 state:will_delete")
        assert "203.0.113.10" in window.sessions_box.toPlainText()
        assert window.session_combo.count() == 1
        assert window.session_combo.findData(missing.session_id) == 0

        window.session_filter_edit.setText("month:2026-01 engine:tcp_connect port:443")
        assert "203.0.113.10" in window.sessions_box.toPlainText()
        assert "198.51.100.10" not in window.sessions_box.toPlainText()
        assert window.session_combo.count() == 1

        window.session_filter_edit.setText("no-match")
        text = window.sessions_box.toPlainText()
        assert text.splitlines()[0] == "Sessions: 0/2"
        assert "Storage: targets 0 | target-month buckets 0 | segments 0" in text
        assert "No saved sessions match filter." in text
        assert window.session_combo.count() == 0
    finally:
        window.close()


def test_main_window_recovers_stale_active_sessions_in_session_list(qt_app, tmp_path) -> None:
    window = MainWindow()
    now = datetime.now()
    store = SessionIndexStore.create(tmp_path)
    sample_path = tmp_path / "198.51.100.10" / "2026-01" / "stale.samples.csv"
    sample_path.parent.mkdir(parents=True)
    sample_path.write_text("header\n", encoding="utf-8")
    record = store.register_session(
        target="198.51.100.10",
        sample_path=sample_path,
        route_path=sample_path.with_name("stale.routes.csv"),
        started_at=now - timedelta(hours=3),
        interval_seconds=5,
        measurement_mode="full_route",
        target_count=1,
    )
    store.add_samples(record.session_id, 1, now - timedelta(hours=2), segments=[sample_path])

    try:
        window.session_index_store = store
        window._sync_sessions_box()

        recovered = store.find_session(record.session_id)
        assert recovered is not None
        assert recovered.state == SESSION_STATE_PAUSED
        assert SESSION_STATE_PAUSED in window.sessions_box.toPlainText()
        assert window.session_combo.count() == 1
    finally:
        window.close()


def test_main_window_marks_missing_session_files_for_delete(qt_app, tmp_path) -> None:
    window = MainWindow()
    now = datetime.now()
    store = SessionIndexStore.create(tmp_path)
    sample_path = tmp_path / "198.51.100.10" / "2026-01" / "missing.samples.csv"
    record = store.register_session(
        target="198.51.100.10",
        sample_path=sample_path,
        route_path=sample_path.with_name("missing.routes.csv"),
        started_at=now,
        interval_seconds=5,
        measurement_mode="full_route",
        target_count=1,
    )

    try:
        window.session_index_store = store
        window._sync_sessions_box()

        marked = store.find_session(record.session_id)
        assert marked is not None
        assert marked.state == SESSION_STATE_WILL_DELETE
        assert SESSION_STATE_WILL_DELETE in window.sessions_box.toPlainText()
        assert window.session_combo.count() == 1
        assert window.delete_session_button.isEnabled() is True
    finally:
        window.close()


def test_main_window_refresh_sessions_recovers_missing_saved_logs(qt_app, tmp_path) -> None:
    window = MainWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    store = SessionIndexStore.create(tmp_path)
    indexed_path = tmp_path / "198.51.100.10" / "2026-01" / "indexed.samples.csv"
    orphan_path = tmp_path / "203.0.113.20" / "2026-01" / "orphan.samples.csv"
    record = store.register_session(
        target="198.51.100.10",
        sample_path=indexed_path,
        route_path=indexed_path.with_name("indexed.routes.csv"),
        started_at=now,
        interval_seconds=1,
        measurement_mode="full_route",
        target_count=1,
    )
    store.finish_session(record.session_id, state=SESSION_STATE_ARCHIVED, ended_at=now)
    with SessionLogWriter(orphan_path) as writer:
        writer.write_many([
            HopObservation(now + timedelta(minutes=1), 0, "203.0.113.20", "Target", True, 20.0, STATUS_OK, True),
        ])

    try:
        window.session_index_store = store
        window._sync_sessions_box()
        assert window.session_combo.count() == 1

        window.refresh_saved_sessions()

        assert window.session_combo.count() == 2
        assert "203.0.113.20" in window.sessions_box.toPlainText()
        assert window.status_label.text() == "Session list refreshed from saved logs"
    finally:
        window.close()


def test_main_window_refresh_sessions_reconciles_existing_log_metadata(qt_app, tmp_path) -> None:
    window = MainWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    store = SessionIndexStore.create(tmp_path)
    sample_path = tmp_path / "198.51.100.10" / "2026-01" / "session.samples.csv"
    with SessionLogWriter(sample_path) as writer:
        writer.write_many(
            [
                HopObservation(now, 0, "198.51.100.10", "Target", True, 10.0, STATUS_OK, True),
                HopObservation(now + timedelta(seconds=1), 0, "198.51.100.10", "Target", True, 11.0, STATUS_OK, True),
            ]
        )
    record = store.register_session(
        target="198.51.100.10",
        sample_path=sample_path,
        route_path=sample_path.with_name("session.routes.csv"),
        started_at=now,
        interval_seconds=1,
        measurement_mode="full_route",
        target_count=1,
    )
    store.finish_session(record.session_id, state=SESSION_STATE_ARCHIVED, ended_at=now)

    try:
        window.session_index_store = store
        window._sync_sessions_box()
        assert "samples 0" in window.sessions_box.toPlainText()

        window.refresh_saved_sessions()

        refreshed = store.find_session(record.session_id)
        assert refreshed is not None
        assert refreshed.samples == 2
        assert "samples 2" in window.sessions_box.toPlainText()
        assert window.status_label.text() == "Session list refreshed from saved logs"
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


def test_main_window_restores_saved_alert_actions_when_opening_session(qt_app, tmp_path) -> None:
    window = MainWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    store = SessionIndexStore.create(tmp_path)
    sample_path = tmp_path / "198.51.100.10" / "2026-01" / "session.samples.csv"
    with SessionLogWriter(sample_path) as writer:
        writer.write_many([
            HopObservation(now, 0, "198.51.100.10", "Target", True, 10.0, STATUS_OK, True),
        ])
    alert_event = AlertEvent(
        key="target_latency_100ms",
        timestamp=now,
        start=now,
        end=now,
        severity="warning",
        title="Latency alert",
        message="Target latency 120.0 ms >= 100 ms",
    )
    append_alert_action(
        alert_action_log_path_for_session(sample_path),
        alert_event,
        actions=["timeline_annotation", "comment"],
    )
    record = store.register_session(
        target="198.51.100.10",
        sample_path=sample_path,
        route_path=sample_path.with_name("session.routes.csv"),
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

        window.open_selected_session()

        assert [event.title for event in window.alert_events] == ["Latency alert"]
        assert "Latency alert" in window.alerts_box.toPlainText()
        assert list(window.alert_event_actions.values()) == [["timeline_annotation", "comment"]]
        annotations = window.annotations_for_export()
        assert len(annotations) == 1
        assert annotations[0].source == "alert"
        assert annotations[0].title == "Latency alert"
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


def test_main_window_marks_new_session_when_starting_from_resume(qt_app, tmp_path) -> None:
    created_workers: list[_FakeWorker] = []

    def worker_factory(**kwargs) -> "_FakeWorker":
        worker = _FakeWorker(
            target=str(kwargs["target"]),
            interval_seconds=int(kwargs["interval_seconds"]),
            max_cycles=kwargs["max_cycles"],
            targets=list(kwargs["targets"]),
            measurement_mode=str(kwargs["measurement_mode"]),
            probe_engine=str(kwargs["probe_engine"]),
            tcp_port=int(kwargs["tcp_port"]),
        )
        created_workers.append(worker)
        return worker

    window = MainWindow(worker_factory=worker_factory)
    now = datetime(2026, 1, 1, 12, 0, 0)
    store = SessionIndexStore.create(tmp_path)
    sample_path = tmp_path / "198.51.100.10" / "2026-01" / "resume.samples.csv"
    with SessionLogWriter(sample_path) as writer:
        writer.write_many([
            HopObservation(now, 0, "198.51.100.10", "Target", True, 10.0, STATUS_OK, True),
            HopObservation(now + timedelta(seconds=1), 0, "203.0.113.20", "Target", True, 20.0, STATUS_OK, True),
        ])
    record = store.register_session(
        target="198.51.100.10",
        sample_path=sample_path,
        route_path=sample_path.with_name("session.routes.csv"),
        started_at=now,
        interval_seconds=5,
        measurement_mode="final_hop_only:icmp",
        target_count=2,
    )
    store.add_samples(record.session_id, 2, now + timedelta(seconds=1), segments=[sample_path])
    store.finish_session(record.session_id, state=SESSION_STATE_ARCHIVED, ended_at=now + timedelta(seconds=1))

    try:
        window.session_index_store = store
        window._sync_sessions_box()
        window.session_combo.setCurrentIndex(window.session_combo.findData(record.session_id))

        window.resume_selected_session()
        window.start_measurement()

        assert len(created_workers) == 1
        assert getattr(created_workers[0], "resumed_from_session_id") == record.session_id
        assert window.pending_resume_session_id == ""
        assert created_workers[0].started is True
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


def test_main_window_exports_visible_saved_sessions_zip(qt_app, tmp_path, monkeypatch) -> None:
    export_path = tmp_path / "visible_sessions.zip"

    def fake_get_save_file_name(*_args, **_kwargs):
        return str(export_path), "ZIP Files (*.zip)"

    monkeypatch.setattr(main_window_module.QFileDialog, "getSaveFileName", fake_get_save_file_name)
    window = MainWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    store = SessionIndexStore.create(tmp_path)
    first_path = tmp_path / "198.51.100.10" / "2026-01" / "first.samples.csv"
    first_path.parent.mkdir(parents=True, exist_ok=True)
    first_path.write_text("first\n", encoding="utf-8")
    first_route_path = first_path.with_name("first.routes.csv")
    first_route_path.write_text("route\n", encoding="utf-8")
    first = store.register_session(
        target="198.51.100.10",
        sample_path=first_path,
        route_path=first_route_path,
        started_at=now,
        interval_seconds=1,
        measurement_mode="full_route",
        target_count=1,
    )
    store.finish_session(first.session_id, state=SESSION_STATE_ARCHIVED, ended_at=now + timedelta(seconds=2))
    second_path = tmp_path / "203.0.113.10" / "2026-01" / "second.samples.csv"
    second_path.parent.mkdir(parents=True, exist_ok=True)
    second_path.write_text("second\n", encoding="utf-8")
    second = store.register_session(
        target="203.0.113.10",
        sample_path=second_path,
        route_path=second_path.with_name("second.routes.csv"),
        started_at=now + timedelta(minutes=1),
        interval_seconds=5,
        measurement_mode="final_hop_only:tcp_connect:port443",
        target_count=2,
        resumed_from_session_id="previous-session",
    )
    store.finish_session(second.session_id, state=SESSION_STATE_ARCHIVED, ended_at=now + timedelta(minutes=1, seconds=2))

    try:
        window.session_index_store = store
        window._sync_sessions_box()
        window.session_filter_edit.setText("203.0.113")

        window.export_visible_sessions()

        assert export_path.exists()
        with zipfile.ZipFile(export_path) as archive:
            names = archive.namelist()
            manifest = archive.read("session_manifest.csv").decode("utf-8")
        assert "203.0.113.10" in manifest
        assert "198.51.100.10" not in manifest
        assert "final_hop_only:tcp_connect:port443" in manifest
        assert "probe_engine,tcp_port,route_probe_engine" in manifest
        assert "tcp_connect,443,disabled" in manifest
        assert "resumed_from_session_id" in manifest
        assert "previous-session" in manifest
        assert any(name.endswith("/second.samples.csv") for name in names)
        assert not any(name.endswith("/first.samples.csv") for name in names)
        assert "Visible sessions ZIP saved" in window.status_label.text()
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


def test_main_window_prunes_old_saved_sessions(qt_app, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        main_window_module.QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    window = MainWindow()
    now = datetime.now()
    store = SessionIndexStore.create(tmp_path)
    old_path = tmp_path / "198.51.100.10" / "2026-01" / "old.samples.csv"
    recent_path = tmp_path / "198.51.100.20" / "2026-01" / "recent.samples.csv"
    for path in (old_path, recent_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("header\n", encoding="utf-8")
    old = store.register_session(
        target="198.51.100.10",
        sample_path=old_path,
        route_path=old_path.with_name("old.routes.csv"),
        started_at=now - timedelta(days=40),
        interval_seconds=1,
        measurement_mode="full_route",
        target_count=1,
    )
    store.finish_session(old.session_id, state=SESSION_STATE_ARCHIVED, ended_at=now - timedelta(days=39))
    recent = store.register_session(
        target="198.51.100.20",
        sample_path=recent_path,
        route_path=recent_path.with_name("recent.routes.csv"),
        started_at=now - timedelta(days=2),
        interval_seconds=1,
        measurement_mode="full_route",
        target_count=1,
    )
    store.finish_session(recent.session_id, state=SESSION_STATE_ARCHIVED, ended_at=now - timedelta(days=1))

    try:
        window.session_index_store = store
        window.session_retention_days_spin.setValue(30)
        window._sync_sessions_box()

        window.prune_old_sessions()

        assert store.find_session(old.session_id) is None
        assert store.find_session(recent.session_id) is not None
        assert not old_path.exists()
        assert recent_path.exists()
        assert window.session_combo.count() == 1
        assert "198.51.100.20" in window.sessions_box.toPlainText()
        assert "Pruned 1 saved session(s) older than 30 day(s)" in window.status_label.text()
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
        assert created_workers[0].target == "8.8.8.8"
        assert created_workers[0].targets == ["8.8.8.8", "192.168.0.1"]
        assert created_workers[0].interval_seconds == 1
        assert created_workers[0].max_cycles is None
        assert created_workers[0].measurement_mode == MEASUREMENT_MODE_FINAL_HOP_ONLY
        assert created_workers[0].probe_engine == PROBE_ENGINE_ICMP
        assert created_workers[0].tcp_port == 443
        assert created_workers[0].started is True
        assert window.start_button.isEnabled() is False
        assert window.stop_button.isEnabled() is True
        assert window.target_input.isHidden() is True
        assert window.running_target_summary_label.isHidden() is False
        assert window.running_target_summary_label.text() == "측정 IP 2개 | 기준 IP 8.8.8.8"

        window.stop_measurement()

        assert created_workers[0].stopped is True
        assert window.stop_button.isEnabled() is False
        assert window.session_state_label.text() == "중지"
        window.on_worker_finished()
        assert window.target_input.isHidden() is False
        assert window.running_target_summary_label.isHidden() is True
    finally:
        window.close()


def test_main_window_pasted_ip_list_updates_trace_targets_without_cutting(qt_app) -> None:
    window = MainWindow()
    targets = [f"10.0.0.{index}" for index in range(1, 12)]

    try:
        window.target_input.setPlainText("\n".join(targets))

        assert window.target_input.toPlainText().splitlines() == targets
        assert window.trace_target_combo.count() == len(targets)
        assert [window.trace_target_combo.itemText(index) for index in range(window.trace_target_combo.count())] == targets
        assert window.status_label.text() == "인식된 IPv4 11개"
    finally:
        window.close()


def test_main_window_passes_route_adjustment_options_to_worker(qt_app) -> None:
    captured_kwargs: dict[str, object] = {}

    def worker_factory(**kwargs) -> "_FakeWorker":
        captured_kwargs.update(kwargs)
        return _FakeWorker(
            target=str(kwargs["target"]),
            interval_seconds=int(kwargs["interval_seconds"]),
            max_cycles=kwargs["max_cycles"],
            targets=list(kwargs["targets"]),
            measurement_mode=str(kwargs["measurement_mode"]),
            probe_engine=str(kwargs["probe_engine"]),
            tcp_port=int(kwargs["tcp_port"]),
        )

    window = MainWindow(worker_factory=worker_factory)

    try:
        window.target_input.setText("198.51.100.10")
        window.refresh_trace_targets()
        window.latency_threshold_spin.setValue(250)
        window.alert_route_adjust_action_check.setChecked(True)
        mode_index = window.measurement_mode_combo.findData(MEASUREMENT_MODE_FINAL_HOP_ONLY)
        assert mode_index >= 0
        window.measurement_mode_combo.setCurrentIndex(mode_index)

        window.start_measurement()

        alert_config = captured_kwargs["alert_rule_config"]
        assert alert_config.latency_threshold_ms == 250
        assert captured_kwargs["auto_full_route_on_alert"] is True
        assert captured_kwargs["auto_restore_final_hop_on_recovery"] is True
    finally:
        window.close()


def test_main_window_saves_and_loads_target_group_preset(qt_app, tmp_path, monkeypatch) -> None:
    preset_path = tmp_path / "target_group.json"

    def fake_get_save_file_name(*_args, **_kwargs):
        return str(preset_path), "JSON Files (*.json)"

    def fake_get_open_file_name(*_args, **_kwargs):
        return str(preset_path), "JSON Files (*.json)"

    monkeypatch.setattr(main_window_module.QFileDialog, "getSaveFileName", fake_get_save_file_name)
    monkeypatch.setattr(main_window_module.QFileDialog, "getOpenFileName", fake_get_open_file_name)
    window = MainWindow()

    try:
        window.target_input.setText("198.51.100.10\n203.0.113.20\n198.51.100.10")
        window.refresh_trace_targets()
        window.trace_target_combo.setCurrentText("203.0.113.20")
        window.interval_combo.setCurrentText("5")
        window.unlimited_check.setChecked(False)
        window.count_spin.setValue(25)
        mode_index = window.measurement_mode_combo.findData(MEASUREMENT_MODE_FINAL_HOP_ONLY)
        probe_index = window.probe_engine_combo.findData(PROBE_ENGINE_TCP_CONNECT)
        assert mode_index >= 0
        assert probe_index >= 0
        window.measurement_mode_combo.setCurrentIndex(mode_index)
        window.probe_engine_combo.setCurrentIndex(probe_index)
        window.tcp_port_spin.setValue(8443)
        window.target_interval_overrides = {
            "198.51.100.10": 5,
            "203.0.113.20": 10,
            "192.0.2.1": 7,
        }

        window.save_target_group_preset()

        data = json.loads(preset_path.read_text(encoding="utf-8"))
        assert data["version"] == TARGET_GROUP_PRESET_VERSION
        assert data["name"] == "target_group"
        assert datetime.fromisoformat(data["created_at"])
        assert data["source"] == "all"
        assert data["summary"]["target_count"] == 2
        assert data["summary"]["trace_target"] == "203.0.113.20"
        assert data["summary"]["measurement_mode"] == MEASUREMENT_MODE_FINAL_HOP_ONLY
        assert data["summary"]["probe_engine"] == PROBE_ENGINE_TCP_CONNECT
        assert data["summary"]["tcp_port"] == 8443
        assert data["summary"]["target_interval_override_count"] == 1
        assert data["targets"] == ["198.51.100.10", "203.0.113.20"]
        assert data["trace_target"] == "203.0.113.20"
        assert data["target_interval_overrides"] == {"203.0.113.20": 10}
        assert data["settings"]["interval_seconds"] == 5
        assert data["settings"]["unlimited"] is False
        assert data["settings"]["count"] == 25
        assert data["settings"]["measurement_mode"] == MEASUREMENT_MODE_FINAL_HOP_ONLY
        assert data["settings"]["probe_engine"] == PROBE_ENGINE_TCP_CONNECT
        assert data["settings"]["tcp_port"] == 8443

        window.target_input.setText("8.8.8.8")
        window.refresh_trace_targets()
        window.interval_combo.setCurrentText("1")
        window.unlimited_check.setChecked(True)
        window.count_spin.setValue(100)
        window.measurement_mode_combo.setCurrentIndex(window.measurement_mode_combo.findData(MEASUREMENT_MODE_FULL_ROUTE))
        window.probe_engine_combo.setCurrentIndex(window.probe_engine_combo.findData("icmp"))
        window.tcp_port_spin.setValue(443)
        window.target_interval_overrides = {}

        window.load_target_group_preset()

        assert window.target_input.toPlainText().splitlines() == ["198.51.100.10", "203.0.113.20"]
        assert window.trace_target_combo.currentText() == "203.0.113.20"
        assert window.interval_combo.currentText() == "5"
        assert window.unlimited_check.isChecked() is False
        assert window.count_spin.value() == 25
        assert window.count_spin.isEnabled() is True
        assert window.measurement_mode_combo.currentData() == MEASUREMENT_MODE_FINAL_HOP_ONLY
        assert window.probe_engine_combo.currentData() == PROBE_ENGINE_TCP_CONNECT
        assert window.tcp_port_spin.value() == 8443
        assert window.tcp_port_spin.isEnabled() is True
        assert window.target_interval_overrides == {"203.0.113.20": 10}
        assert window.status_label.text() == "Target group loaded: 2 target(s) | target_group | interval overrides 1"
    finally:
        window.close()


def test_main_window_loads_legacy_target_group_preset(qt_app, tmp_path, monkeypatch) -> None:
    preset_path = tmp_path / "legacy_group.json"
    preset_path.write_text(
        json.dumps(
            {
                "version": 1,
                "targets": ["198.51.100.10", "203.0.113.20"],
                "trace_target": "203.0.113.20",
                "settings": {
                    "interval_seconds": 5,
                    "unlimited": False,
                    "count": 25,
                    "measurement_mode": MEASUREMENT_MODE_FINAL_HOP_ONLY,
                    "probe_engine": PROBE_ENGINE_TCP_CONNECT,
                    "tcp_port": 8443,
                },
            }
        ),
        encoding="utf-8",
    )

    def fake_get_open_file_name(*_args, **_kwargs):
        return str(preset_path), "JSON Files (*.json)"

    monkeypatch.setattr(main_window_module.QFileDialog, "getOpenFileName", fake_get_open_file_name)
    window = MainWindow()

    try:
        window.load_target_group_preset()

        assert window.target_input.toPlainText().splitlines() == ["198.51.100.10", "203.0.113.20"]
        assert window.trace_target_combo.currentText() == "203.0.113.20"
        assert window.interval_combo.currentText() == "5"
        assert window.unlimited_check.isChecked() is False
        assert window.count_spin.value() == 25
        assert window.measurement_mode_combo.currentData() == MEASUREMENT_MODE_FINAL_HOP_ONLY
        assert window.probe_engine_combo.currentData() == PROBE_ENGINE_TCP_CONNECT
        assert window.tcp_port_spin.value() == 8443
        assert window.target_interval_overrides == {}
        assert window.status_label.text() == "Target group loaded: 2 target(s)"
    finally:
        window.close()


def test_main_window_rejects_target_group_summary_count_mismatch(qt_app, tmp_path, monkeypatch) -> None:
    preset_path = tmp_path / "bad_group.json"
    preset_path.write_text(
        json.dumps(
            {
                "version": TARGET_GROUP_PRESET_VERSION,
                "name": "bad_group",
                "source": "all",
                "summary": {"target_count": 3},
                "targets": ["198.51.100.10", "203.0.113.20"],
                "trace_target": "198.51.100.10",
                "settings": {"interval_seconds": 1},
            }
        ),
        encoding="utf-8",
    )

    def fake_get_open_file_name(*_args, **_kwargs):
        return str(preset_path), "JSON Files (*.json)"

    monkeypatch.setattr(main_window_module.QFileDialog, "getOpenFileName", fake_get_open_file_name)
    window = MainWindow()
    warnings: list[str] = []
    monkeypatch.setattr(main_window_module.QMessageBox, "warning", lambda *_args: warnings.append(str(_args[-1])))

    try:
        window.load_target_group_preset()

        assert "target_count does not match" in warnings[-1]
        assert "target_count does not match" in window.status_label.text()
        assert window.target_input.toPlainText() == ""
    finally:
        window.close()


def test_main_window_rejects_target_group_interval_override_count_mismatch(qt_app, tmp_path, monkeypatch) -> None:
    preset_path = tmp_path / "bad_interval_group.json"
    preset_path.write_text(
        json.dumps(
            {
                "version": TARGET_GROUP_PRESET_VERSION,
                "name": "bad_interval_group",
                "source": "all",
                "summary": {
                    "target_count": 2,
                    "target_interval_override_count": 2,
                },
                "targets": ["198.51.100.10", "203.0.113.20"],
                "trace_target": "198.51.100.10",
                "target_interval_overrides": {"203.0.113.20": 5},
                "settings": {"interval_seconds": 1},
            }
        ),
        encoding="utf-8",
    )

    def fake_get_open_file_name(*_args, **_kwargs):
        return str(preset_path), "JSON Files (*.json)"

    monkeypatch.setattr(main_window_module.QFileDialog, "getOpenFileName", fake_get_open_file_name)
    window = MainWindow()
    warnings: list[str] = []
    monkeypatch.setattr(main_window_module.QMessageBox, "warning", lambda *_args: warnings.append(str(_args[-1])))

    try:
        window.target_input.setPlainText("8.8.8.8")
        window.load_target_group_preset()

        assert "target_interval_override_count does not match" in warnings[-1]
        assert "target_interval_override_count does not match" in window.status_label.text()
        assert window.target_interval_overrides == {}
        assert window.target_input.toPlainText() == "8.8.8.8"
    finally:
        window.close()


def test_main_window_applies_loaded_target_group_interval_overrides_on_start(qt_app, tmp_path, monkeypatch) -> None:
    preset_path = tmp_path / "interval_group.json"
    preset_path.write_text(
        json.dumps(
            {
                "version": TARGET_GROUP_PRESET_VERSION,
                "name": "interval_group",
                "source": "all",
                "summary": {
                    "target_count": 2,
                    "target_interval_override_count": 1,
                },
                "targets": ["198.51.100.10", "203.0.113.20"],
                "trace_target": "198.51.100.10",
                "target_interval_overrides": {"203.0.113.20": 5},
                "settings": {
                    "interval_seconds": 1,
                    "unlimited": True,
                    "measurement_mode": MEASUREMENT_MODE_FULL_ROUTE,
                },
            }
        ),
        encoding="utf-8",
    )

    def fake_get_open_file_name(*_args, **_kwargs):
        return str(preset_path), "JSON Files (*.json)"

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

    monkeypatch.setattr(main_window_module.QFileDialog, "getOpenFileName", fake_get_open_file_name)
    window = MainWindow(worker_factory=worker_factory)

    try:
        window.load_target_group_preset()
        window.start_measurement()

        assert window.target_interval_overrides == {"203.0.113.20": 5}
        assert created_workers[0].interval_seconds == 1
        assert created_workers[0].target_interval_updates == [(["203.0.113.20"], 5)]
        assert created_workers[0].started is True
    finally:
        window.close()


def test_main_window_saves_selected_target_group_from_summary(qt_app, tmp_path, monkeypatch) -> None:
    preset_path = tmp_path / "selected_target_group.json"

    def fake_get_save_file_name(*_args, **_kwargs):
        return str(preset_path), "JSON Files (*.json)"

    monkeypatch.setattr(main_window_module.QFileDialog, "getSaveFileName", fake_get_save_file_name)
    window = MainWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    target_one = _snapshot(0, "198.51.100.10", None, latency=10.0, is_target=True)
    target_two = _snapshot(0, "203.0.113.20", None, latency=20.0, is_target=True)
    target_three = _snapshot(0, "203.0.113.30", None, latency=30.0, is_target=True)

    try:
        window.target_input.setText("198.51.100.10\n203.0.113.20\n203.0.113.30")
        window.refresh_trace_targets()
        window.trace_target_combo.setCurrentText("198.51.100.10")
        window.on_measurement_updated(
            [],
            target_one,
            [target_one, target_two, target_three],
            ["live"],
            [HopObservation(now, 0, "198.51.100.10", "Target", True, 10.0, STATUS_OK, True)],
            [HopObservation(now, 0, "198.51.100.10", "Target", True, 10.0, STATUS_OK, True)],
        )

        window.target_table.item(1, 0).setSelected(True)
        window.target_table.item(2, 0).setSelected(True)
        window.save_selected_target_group_preset()

        data = json.loads(preset_path.read_text(encoding="utf-8"))
        assert data["version"] == TARGET_GROUP_PRESET_VERSION
        assert data["name"] == "selected_target_group"
        assert data["source"] == "selected"
        assert data["summary"]["target_count"] == 2
        assert data["summary"]["target_interval_override_count"] == 0
        assert data["targets"] == ["203.0.113.20", "203.0.113.30"]
        assert data["trace_target"] == "203.0.113.20"
        assert data["target_interval_overrides"] == {}
        assert window.status_label.text() == f"Target group saved: {preset_path} (2 target(s))"
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
        assert window.save_selected_target_group_button.isEnabled() is True
        window.on_measurement_updated(
            [],
            target_one,
            [target_one, target_two],
            ["live"],
            [HopObservation(now, 0, "198.51.100.10", "Target", True, 10.0, STATUS_OK, True)],
            [HopObservation(now, 0, "198.51.100.10", "Target", True, 10.0, STATUS_OK, True)],
        )

        window.target_table.selectRow(1)
        assert "선택 1" in window.target_summary_status_label.text()
        window.pause_selected_targets()
        window.resume_selected_targets()
        window.pause_all_targets()
        window.resume_all_targets()
        window.interval_combo.setCurrentText("5")
        window.apply_runtime_interval()
        interval_column = TARGET_HEADERS.index("Interval")
        interval_source_column = TARGET_HEADERS.index("Interval Source")
        assert "개별 주기 1" in window.target_summary_status_label.text()
        assert window.target_table.item(1, interval_column).text() == "5s"
        assert window.target_table.item(1, interval_source_column).text() == "target"
        window.target_table.clearSelection()
        window.interval_combo.setCurrentText("2")
        window.apply_runtime_interval()

        assert worker.paused_calls == [["203.0.113.10"], ["198.51.100.10", "203.0.113.10"]]
        assert worker.resumed_calls == [["203.0.113.10"], ["198.51.100.10", "203.0.113.10"]]
        assert worker.target_interval_updates == [(["203.0.113.10"], 5)]
        assert worker.interval_updates == [2]
        assert "개별 주기" not in window.target_summary_status_label.text()
        assert "선택" not in window.target_summary_status_label.text()
        assert window.target_table.item(0, interval_column).text() == "2s"
        assert window.target_table.item(1, interval_column).text() == "2s"
        assert window.apply_interval_button.isEnabled() is True
    finally:
        window.close()


def test_main_window_filters_visible_targets_for_batch_controls(qt_app) -> None:
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
    healthy = _snapshot(0, "198.51.100.10", None, latency=10.0, is_target=True)
    warning = _snapshot(0, "203.0.113.10", None, loss=8.0, latency=18.0, is_target=True)
    critical = _snapshot(
        0,
        "203.0.113.20",
        None,
        loss=35.0,
        latency=None,
        received=0,
        timeout_count=1,
        status=STATUS_TIMEOUT,
        is_target=True,
    )

    try:
        window.target_input.setText("198.51.100.10\n203.0.113.10\n203.0.113.20")
        window.refresh_trace_targets()
        window.start_measurement()
        worker = created_workers[0]
        window.on_measurement_updated(
            [],
            healthy,
            [healthy, warning, critical],
            ["live"],
            [
                HopObservation(now, 0, "198.51.100.10", "Target", True, 10.0, STATUS_OK, True),
                HopObservation(now, 0, "203.0.113.10", "Target", True, 18.0, STATUS_OK, True),
                HopObservation(now, 0, "203.0.113.20", "Target", False, None, STATUS_TIMEOUT, True),
            ],
            [],
        )

        problem_index = window.target_status_filter_combo.findData("problem")
        assert problem_index >= 0
        window.target_status_filter_combo.setCurrentIndex(problem_index)

        assert window.target_table.rowCount() == 2
        assert window.target_summary_status_label.text().startswith("IP: 2/3")
        assert "주의 1" in window.target_summary_status_label.text()
        assert "장애 1" in window.target_summary_status_label.text()

        window.pause_visible_targets()
        window.resume_visible_targets()
        window.interval_combo.setCurrentText("5")
        window.apply_visible_interval()

        assert worker.paused_calls == [["203.0.113.10", "203.0.113.20"]]
        assert worker.resumed_calls == [["203.0.113.10", "203.0.113.20"]]
        assert worker.target_interval_updates == [(["203.0.113.10", "203.0.113.20"], 5)]
        assert "개별 주기 2" in window.target_summary_status_label.text()

        window.target_filter_edit.setText("203.0.113.20")

        assert window.target_table.rowCount() == 1
        assert window.target_table.item(0, 0).text() == "203.0.113.20"
        assert window.target_table.item(0, TARGET_HEADERS.index("Interval")).text() == "5s"
        assert window.target_table.item(0, TARGET_HEADERS.index("Interval Source")).text() == "target"
        assert window.export_target_summary_button.isEnabled() is True

        window.target_filter_edit.setText("no-match")

        assert window.target_table.rowCount() == 0
        assert window.target_summary_status_label.text() == "IP: 0/3"
        assert window.export_target_summary_button.isEnabled() is False
    finally:
        window.close()


def test_main_window_renders_each_target_as_separate_graph_row(qt_app) -> None:
    window = MainWindow()
    now = datetime.now()
    first = _snapshot(0, "198.51.100.10", None, latency=10.0, is_target=True)
    selected = _snapshot(0, "203.0.113.10", None, latency=18.0, is_target=True)
    timeout = _snapshot(
        0,
        "203.0.113.20",
        None,
        loss=35.0,
        latency=None,
        received=0,
        timeout_count=1,
        status=STATUS_TIMEOUT,
        is_target=True,
    )
    observations = [
        HopObservation(now, 0, "198.51.100.10", "Target", True, 10.0, STATUS_OK, True),
        HopObservation(now, 0, "203.0.113.10", "Target", True, 18.0, STATUS_OK, True),
        HopObservation(now, 0, "203.0.113.20", "Target", False, None, STATUS_TIMEOUT, True),
    ]

    try:
        window.current_target = "203.0.113.10"
        window.on_measurement_updated([], selected, [first, selected, timeout], ["live"], observations, [])

        assert set(window.target_graph_rows) == {
            "198.51.100.10",
            "203.0.113.10",
            "203.0.113.20",
        }
        assert window.target_graph_widgets["203.0.113.10"] is window.graph
        assert window.graph._points == [observations[1]]
        assert window.target_graph_widgets["198.51.100.10"]._points == [observations[0]]
        assert window.target_graph_widgets["203.0.113.20"]._points == [observations[2]]
        assert "현재 18.0 ms" in window.target_graph_metric_labels["203.0.113.10"].text()
        assert "손실 35.0%" in window.target_graph_metric_labels["203.0.113.20"].text()
    finally:
        window.close()


def test_main_window_throttles_many_target_graph_rows_but_updates_table(qt_app) -> None:
    window = MainWindow()
    now = datetime.now()
    targets = [f"10.0.0.{index}" for index in range(1, 7)]
    first_snapshots = [
        _snapshot(0, target, None, latency=float(index), is_target=True)
        for index, target in enumerate(targets, start=1)
    ]
    second_snapshots = [
        _snapshot(0, target, None, latency=float(index + 100), is_target=True)
        for index, target in enumerate(targets, start=1)
    ]
    first_observations = [
        HopObservation(now, 0, target, "Target", True, float(index), STATUS_OK, True)
        for index, target in enumerate(targets, start=1)
    ]
    second_observations = [
        HopObservation(now + timedelta(seconds=1), 0, target, "Target", True, float(index + 100), STATUS_OK, True)
        for index, target in enumerate(targets, start=1)
    ]

    try:
        window.current_target = targets[0]
        window.on_measurement_updated([], first_snapshots[0], first_snapshots, ["live"], first_observations, [])

        assert window.graph._points == [first_observations[0]]

        window.on_measurement_updated([], second_snapshots[0], second_snapshots, ["live"], second_observations, [])

        current_column = TARGET_HEADERS.index("현재 지연")
        assert window._pending_graph_render is True
        assert window.graph._points == [first_observations[0]]
        assert window.target_table.item(0, current_column).text() == "101.0"

        window._render_pending_graph()

        assert window._pending_graph_render is False
        assert window.graph._points == [second_observations[0]]
    finally:
        window.close()


def test_main_window_keeps_small_target_graph_rows_immediate(qt_app) -> None:
    window = MainWindow()
    now = datetime.now()
    targets = [f"10.0.1.{index}" for index in range(1, 5)]
    first_snapshots = [
        _snapshot(0, target, None, latency=float(index), is_target=True)
        for index, target in enumerate(targets, start=1)
    ]
    second_snapshots = [
        _snapshot(0, target, None, latency=float(index + 50), is_target=True)
        for index, target in enumerate(targets, start=1)
    ]
    first_observations = [
        HopObservation(now, 0, target, "Target", True, float(index), STATUS_OK, True)
        for index, target in enumerate(targets, start=1)
    ]
    second_observations = [
        HopObservation(now + timedelta(seconds=1), 0, target, "Target", True, float(index + 50), STATUS_OK, True)
        for index, target in enumerate(targets, start=1)
    ]

    try:
        window.current_target = targets[0]
        window.on_measurement_updated([], first_snapshots[0], first_snapshots, ["live"], first_observations, [])
        window.on_measurement_updated([], second_snapshots[0], second_snapshots, ["live"], second_observations, [])

        assert window._pending_graph_render is False
        assert window.graph._points == [second_observations[0]]
    finally:
        window.close()


def test_main_window_problem_target_batch_controls(qt_app) -> None:
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
    healthy = _snapshot(0, "198.51.100.10", None, latency=10.0, is_target=True)
    warning = _snapshot(0, "203.0.113.10", None, loss=8.0, latency=18.0, is_target=True)
    critical = _snapshot(
        0,
        "203.0.113.20",
        None,
        loss=35.0,
        latency=None,
        received=0,
        timeout_count=1,
        status=STATUS_TIMEOUT,
        is_target=True,
    )

    try:
        window.target_input.setText("198.51.100.10\n203.0.113.10\n203.0.113.20")
        window.refresh_trace_targets()
        window.start_measurement()
        worker = created_workers[0]
        window.on_measurement_updated(
            [],
            healthy,
            [healthy, warning, critical],
            ["live"],
            [
                HopObservation(now, 0, "198.51.100.10", "Target", True, 10.0, STATUS_OK, True),
                HopObservation(now, 0, "203.0.113.10", "Target", True, 18.0, STATUS_OK, True),
                HopObservation(now, 0, "203.0.113.20", "Target", False, None, STATUS_TIMEOUT, True),
            ],
            [],
        )

        window.pause_problem_targets()
        window.resume_problem_targets()
        window.interval_combo.setCurrentText("5")
        window.apply_problem_interval()

        assert worker.paused_calls == [["203.0.113.10", "203.0.113.20"]]
        assert worker.resumed_calls == [["203.0.113.10", "203.0.113.20"]]
        assert worker.target_interval_updates == [(["203.0.113.10", "203.0.113.20"], 5)]
        assert window.target_interval_overrides == {
            "203.0.113.10": 5,
            "203.0.113.20": 5,
        }
        assert "Runtime interval applied to problem 2 target(s): 5s" in window.status_label.text()
        assert "개별 주기 2" in window.target_summary_status_label.text()
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
        summary = window.target_summary_status_label.text()
        assert "IP: 2" in summary
        assert "정상 1" in summary
        assert "장애 1" in summary
        assert "최대 손실 25.0%" in summary
        assert window.metric_value_labels["loss"].text() == "25.0%"
        assert window.metric_value_labels["samples"].text() == "1"
        assert "가능성" in window.analysis_box.toPlainText()
        assert window.csv_button.isEnabled() is True
        assert window.xlsx_button.isEnabled() is True
        assert window.report_button.isEnabled() is True
        assert window.graph_png_button.isEnabled() is True
        assert window.stats_csv_button.isEnabled() is True
        assert window.stats_xlsx_button.isEnabled() is True
    finally:
        window.close()


def test_main_window_starts_html_report_export(qt_app, tmp_path, monkeypatch) -> None:
    created_workers: list[_FakeExportWorker] = []
    export_path = tmp_path / "report.html"

    def fake_get_save_file_name(*_args, **_kwargs):
        return str(export_path), "HTML Files (*.html)"

    def fake_export_worker(**kwargs):
        worker = _FakeExportWorker(**kwargs)
        created_workers.append(worker)
        return worker

    monkeypatch.setattr(main_window_module.QFileDialog, "getSaveFileName", fake_get_save_file_name)
    monkeypatch.setattr(main_window_module, "ExportWorker", fake_export_worker)
    window = MainWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    target_snapshot = _snapshot(0, "198.51.100.10", None, latency=20.0, is_target=True)

    try:
        window.current_target = "198.51.100.10"
        window.session_log_path = tmp_path / "session.csv"
        window.focus_range = (now, now + timedelta(minutes=10))
        window.on_measurement_updated(
            [],
            target_snapshot,
            [target_snapshot],
            ["Target path needs review"],
            [HopObservation(now, 0, "198.51.100.10", "Target", True, 20.0, STATUS_OK, True)],
            [],
        )
        html_index = window.report_format_combo.findData("html")
        assert html_index >= 0
        window.report_format_combo.setCurrentIndex(html_index)

        window.save_report()

        assert len(created_workers) == 1
        worker = created_workers[0]
        assert worker.kwargs["kind"] == "html"
        assert worker.kwargs["path"] == export_path
        assert worker.kwargs["target"] == "198.51.100.10"
        assert worker.kwargs["focus_range"] == window.focus_range
        assert worker.started is True
        assert window.report_button.isEnabled() is False
        assert window.report_format_combo.isEnabled() is False
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


def test_main_window_exports_target_summary_csv(qt_app, tmp_path, monkeypatch) -> None:
    export_path = tmp_path / "target_summary.csv"

    def fake_get_save_file_name(*_args, **_kwargs):
        return str(export_path), "CSV Files (*.csv)"

    monkeypatch.setattr(main_window_module.QFileDialog, "getSaveFileName", fake_get_save_file_name)
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
        window.target_interval_overrides = {"203.0.113.10": 5}
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

        assert window.export_target_summary_button.isEnabled() is True
        window.problem_sort_check.setChecked(True)

        window.save_target_summary_csv()

        with export_path.open(newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        assert [row["target"] for row in rows] == ["203.0.113.10", "198.51.100.10"]
        assert rows[0]["status"] == "CRITICAL"
        assert rows[0]["failed"] == "1"
        assert rows[0]["loss_percent"] == "40.0"
        assert rows[0]["interval_seconds"] == "5"
        assert rows[0]["interval_source"] == "target"
        assert rows[1]["interval_seconds"] == "1"
        assert rows[1]["interval_source"] == "global"
        assert "Target summary CSV saved" in window.status_label.text()
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


def test_main_window_statistics_custom_scope_uses_explicit_range(qt_app, tmp_path, monkeypatch) -> None:
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
    start = now + timedelta(seconds=5)
    end = now + timedelta(seconds=65)

    try:
        window.current_target = "198.51.100.10"
        window.observations = [
            HopObservation(now, 0, "198.51.100.10", "Target", True, 1.0, STATUS_OK, True),
            HopObservation(now + timedelta(seconds=30), 0, "198.51.100.10", "Target", True, 2.0, STATUS_OK, True),
            HopObservation(now + timedelta(seconds=90), 0, "198.51.100.10", "Target", True, 3.0, STATUS_OK, True),
        ]
        scope_index = window.statistics_scope_combo.findData(STATISTICS_SCOPE_CUSTOM)
        assert scope_index >= 0
        window.statistics_scope_combo.setCurrentIndex(scope_index)
        window._set_statistics_custom_range(start, end)

        window.save_statistics_csv()

        assert len(created_workers) == 1
        worker = created_workers[0]
        assert worker.kwargs["focus_range"] == (start, end)
        assert [point.latency_ms for point in worker.kwargs["observations_override"]] == [2.0]
        assert window.statistics_start_edit.isEnabled() is False
        assert window.statistics_end_edit.isEnabled() is False
    finally:
        window.close()


def test_main_window_saves_graph_png_from_export_panel(qt_app, tmp_path, monkeypatch) -> None:
    export_path = tmp_path / "timeline"
    saved_paths: list[tuple[Path, str]] = []

    def fake_get_save_file_name(*_args, **_kwargs):
        return str(export_path), "PNG Files (*.png)"

    def fake_save_graph_png(path: Path, *, scope: str = GRAPH_PNG_SCOPE_TIMELINE) -> Path:
        saved_paths.append((path, scope))
        return path.with_suffix(".png")

    monkeypatch.setattr(main_window_module.QFileDialog, "getSaveFileName", fake_get_save_file_name)
    window = MainWindow()
    now = datetime.now()
    target_snapshot = _snapshot(0, "198.51.100.10", None, latency=12.0, is_target=True)
    history = [
        HopObservation(now, 0, "198.51.100.10", "Target", True, 12.0, STATUS_OK, True),
    ]

    try:
        window.current_target = "198.51.100.10"
        window.on_measurement_updated([], target_snapshot, [target_snapshot], ["OK"], history, history)
        monkeypatch.setattr(window, "_save_graph_png", fake_save_graph_png)

        window.save_graph_png()

        assert saved_paths == [(export_path, GRAPH_PNG_SCOPE_TIMELINE)]
        assert window.status_label.text() == f"PNG saved: {export_path.with_suffix('.png')}"
    finally:
        window.close()


def test_main_window_graph_png_helper_adds_suffix_and_saves_timeline_scope(qt_app, tmp_path, monkeypatch) -> None:
    class FakePixmap:
        def __init__(self) -> None:
            self.saved: list[tuple[str, str]] = []

        def isNull(self) -> bool:
            return False

        def save(self, path: str, fmt: str) -> bool:
            self.saved.append((path, fmt))
            Path(path).write_bytes(b"png")
            return True

    pixmap = FakePixmap()
    monkeypatch.setattr(main_window_module.LatencyGraphWidget, "grab", lambda _widget: pixmap)
    window = MainWindow()

    try:
        saved = window._save_graph_png(tmp_path / "timeline")

        assert saved == tmp_path / "timeline.png"
        assert pixmap.saved == [(str(saved), "PNG")]
        assert saved.read_bytes() == b"png"
    finally:
        window.close()


def test_main_window_graph_png_helper_saves_trace_scope(qt_app, tmp_path, monkeypatch) -> None:
    class FakePixmap:
        def __init__(self) -> None:
            self.saved: list[tuple[str, str]] = []

        def isNull(self) -> bool:
            return False

        def save(self, path: str, fmt: str) -> bool:
            self.saved.append((path, fmt))
            Path(path).write_bytes(b"trace")
            return True

    pixmap = FakePixmap()
    window = MainWindow()
    monkeypatch.setattr(window.table, "grab", lambda: pixmap)

    try:
        saved = window._save_graph_png(tmp_path / "trace", scope=GRAPH_PNG_SCOPE_TRACE)

        assert saved == tmp_path / "trace.png"
        assert pixmap.saved == [(str(saved), "PNG")]
        assert saved.read_bytes() == b"trace"
    finally:
        window.close()


def test_main_window_graph_png_helper_combines_trace_and_timeline_scopes(qt_app, tmp_path, monkeypatch) -> None:
    window = MainWindow()

    try:
        trace = main_window_module.QPixmap(120, 40)
        trace.fill(Qt.GlobalColor.white)
        timeline = main_window_module.QPixmap(80, 30)
        timeline.fill(Qt.GlobalColor.white)
        monkeypatch.setattr(window.table, "grab", lambda: trace)
        monkeypatch.setattr(window.graph, "grab", lambda: timeline)

        pixmap = window._graph_png_pixmap(GRAPH_PNG_SCOPE_BOTH)

        assert pixmap.width() == 120
        assert pixmap.height() == 70
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
        assert window.timeline_label.text().startswith("Timeline: ")
        assert window.timeline_label.text() != "Timeline: Live"
        assert "10m" in window.timeline_label.toolTip()
        assert "10m" in window.graph_detail_window.timeline_status_label.text()
        window.load_timeline_range(172800)
        assert "48h" in window.timeline_label.toolTip()
        assert "48h" in window.graph_detail_window.timeline_status_label.text()

        window.apply_focus_range((now, now + timedelta(seconds=3)))

        assert window.table.item(0, 8).text() == "50.0"
        assert window.target_table.item(0, 6).text() == "50.0"
        assert window.analysis_for_export()[0].startswith("Focus period:")
        window.clear_timeline_range()
        assert window.timeline_label.text() == "Timeline: Live"
    finally:
        window.close()


def test_main_window_loads_timeline_range_from_main_controls(qt_app, tmp_path) -> None:
    window = MainWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    session_path = tmp_path / "session.csv"
    writer = SessionLogWriter(session_path)
    writer.write_many([
        HopObservation(now - timedelta(minutes=20), 0, "198.51.100.10", "Target", True, 18.0, STATUS_OK, True),
        HopObservation(now - timedelta(minutes=20), 1, "192.0.2.1", "gateway", True, 2.0, STATUS_OK),
        HopObservation(now - timedelta(minutes=5), 0, "198.51.100.10", "Target", True, 20.0, STATUS_OK, True),
        HopObservation(now, 0, "198.51.100.10", "Target", False, None, STATUS_TIMEOUT, True),
        HopObservation(now, 1, "192.0.2.1", "gateway", True, 3.0, STATUS_OK),
    ])
    writer.close()

    try:
        window.current_target = "198.51.100.10"
        window.session_log_path = session_path
        window.timeline_range_combo.setCurrentIndex(window.timeline_range_combo.findData(600))

        window.load_selected_timeline_range()

        assert window.timeline_range == (now - timedelta(minutes=10), now)
        assert [point.timestamp for point in window.graph._points] == [
            now - timedelta(minutes=5),
            now,
        ]
        assert window.timeline_label.text().startswith("Timeline: ")
        assert "10m" in window.timeline_label.toolTip()
        assert "Timeline: last 10m" in window.status_label.text()
    finally:
        window.close()


def test_main_window_reset_focus_to_current_clears_timeline_and_focus(qt_app) -> None:
    window = MainWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    history = [
        HopObservation(now + timedelta(seconds=index), 0, "198.51.100.10", "Target", True, 20.0, STATUS_OK, True)
        for index in range(120)
    ]
    target_snapshot = _snapshot(0, "198.51.100.10", None, latency=20.0, is_target=True)

    try:
        window.current_target = "198.51.100.10"
        window.on_measurement_updated([], target_snapshot, [target_snapshot], ["live"], history, history)
        window.load_timeline_range(60)
        window.apply_focus_range((now + timedelta(seconds=30), now + timedelta(seconds=60)))
        window.graph.zoom_in()
        window.graph.pan_left()

        window.reset_focus_to_current()

        assert window.focus_range is None
        assert window.timeline_range is None
        assert window.focus_label.text() == "Live"
        assert window.timeline_label.text() == "Timeline: Live"
        assert window.graph.visible_datetime_range() is not None
        assert window.graph.visible_datetime_range()[1] == history[-1].timestamp
        assert window.status_label.text() == "Focus and timeline reset to current"
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


def test_main_window_open_session_does_not_replay_route_alert_actions(qt_app, tmp_path) -> None:
    window = MainWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    store = SessionIndexStore.create(tmp_path)
    sample_path = tmp_path / "198.51.100.10" / "2026-01" / "session.samples.csv"
    with SessionLogWriter(sample_path) as writer:
        writer.write_many([
            HopObservation(now, 0, "198.51.100.10", "Target", True, 10.0, STATUS_OK, True),
            HopObservation(now + timedelta(seconds=60), 0, "198.51.100.10", "Target", True, 12.0, STATUS_OK, True),
        ])
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
    route_path = route_log_path_for_session(sample_path)
    with RouteLogWriter(route_path) as route_writer:
        route_writer.write_snapshot(history.snapshots[0])
        route_writer.write_snapshot(history.snapshots[1], change)
    append_alert_action(
        alert_action_log_path_for_session(sample_path),
        AlertEvent(
            key=f"route_changed:{change.timestamp.isoformat(timespec='seconds')}",
            timestamp=change.timestamp,
            start=change.timestamp,
            end=change.timestamp,
            severity="warning",
            title="Route changed",
            message=change.summary,
            series_key=None,
        ),
        actions=["comment"],
        source="route",
    )
    record = store.register_session(
        target="198.51.100.10",
        sample_path=sample_path,
        route_path=route_path,
        started_at=now,
        interval_seconds=1,
        measurement_mode="full_route",
        target_count=1,
    )
    store.add_samples(record.session_id, 2, now + timedelta(seconds=60), segments=[sample_path])
    store.finish_session(record.session_id, state=SESSION_STATE_ARCHIVED, ended_at=now + timedelta(seconds=60))

    try:
        window.session_index_store = store
        window._sync_sessions_box()
        window.session_combo.setCurrentIndex(window.session_combo.findData(record.session_id))

        window.open_selected_session()

        rows = read_alert_actions(alert_action_log_path_for_session(sample_path))
        assert len(rows) == 1
        assert rows[0]["actions"] == "comment"
        assert [event.title for event in window.alert_events] == ["Route changed"]
        assert list(window.alert_event_actions.values()) == [["comment"]]
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
        assert window.alert_table.rowCount() == 2
        alert_titles = {
            window.alert_table.item(row, 2).text()
            for row in range(window.alert_table.rowCount())
        }
        assert alert_titles == {"Loss alert", "Latency alert"}
        first_row_actions = window.alert_table.item(0, 6).text()
        second_row_actions = window.alert_table.item(1, 6).text()
        assert {first_row_actions, second_row_actions} == {"timeline_annotation, comment"}
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


def test_main_window_records_route_adjustment_action_for_final_hop_alert(qt_app, tmp_path) -> None:
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
        window.alert_route_adjust_action_check.setChecked(True)
        window.alert_timeline_action_check.setChecked(False)
        window.alert_comment_action_check.setChecked(False)
        mode_index = window.measurement_mode_combo.findData(MEASUREMENT_MODE_FINAL_HOP_ONLY)
        assert mode_index >= 0
        window.measurement_mode_combo.setCurrentIndex(mode_index)

        window.on_measurement_updated([], target_snapshot, [target_snapshot], ["live"], history, history)

        rows = read_alert_actions(window.alert_action_log_path)
        assert rows[0]["title"] == "Latency alert"
        assert rows[0]["actions"] == "route_adjustment"
    finally:
        window.close()


def test_main_window_disabled_alert_condition_does_not_fire(qt_app, tmp_path) -> None:
    window = MainWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    history = [
        HopObservation(now, 0, "198.51.100.10", "Target", True, 90.0, STATUS_OK, True),
    ]
    target_snapshot = _snapshot(0, "198.51.100.10", None, latency=90.0, is_target=True)

    try:
        window.current_target = "198.51.100.10"
        window.alert_action_log_path = tmp_path / "session.alerts.csv"
        window.latency_threshold_spin.setValue(80)
        window.latency_alert_check.setChecked(False)

        window.on_measurement_updated([], target_snapshot, [target_snapshot], ["live"], history, history)

        assert window.alerts_box.toPlainText() == "No alert events."
        assert read_alert_actions(window.alert_action_log_path) == []
    finally:
        window.close()


def test_main_window_saves_and_loads_alert_rule_preset(qt_app, tmp_path, monkeypatch) -> None:
    preset_path = tmp_path / "voice_alerts.json"

    def fake_get_save_file_name(*_args, **_kwargs):
        return str(preset_path), "JSON Files (*.json)"

    def fake_get_open_file_name(*_args, **_kwargs):
        return str(preset_path), "JSON Files (*.json)"

    monkeypatch.setattr(main_window_module.QFileDialog, "getSaveFileName", fake_get_save_file_name)
    monkeypatch.setattr(main_window_module.QFileDialog, "getOpenFileName", fake_get_open_file_name)
    window = MainWindow()

    try:
        window.loss_alert_check.setChecked(False)
        window.latency_alert_check.setChecked(True)
        window.jitter_alert_check.setChecked(False)
        window.sample_alert_check.setChecked(True)
        window.timer_alert_check.setChecked(False)
        window.loss_threshold_spin.setValue(35)
        window.loss_window_spin.setValue(7)
        window.latency_threshold_spin.setValue(180)
        window.jitter_threshold_spin.setValue(45)
        window.sample_window_spin.setValue(12)
        window.sample_bad_spin.setValue(8)
        window.timer_window_spin.setValue(9)
        window.mos_alert_check.setChecked(True)
        window.mos_threshold_spin.setValue(3.2)
        window.mos_window_spin.setValue(4)
        window.route_ip_alert_check.setChecked(True)
        window.route_ip_alert_edit.setText("203.0.113.50")
        window.alert_start_action_check.setChecked(True)
        window.alert_end_action_check.setChecked(False)
        window.alert_route_adjust_action_check.setChecked(False)
        window.alert_timeline_action_check.setChecked(False)
        window.alert_comment_action_check.setChecked(True)
        window.alert_log_action_check.setChecked(True)
        window.alert_beep_action_check.setChecked(True)
        window.alert_image_action_check.setChecked(True)
        window.alert_email_action_check.setChecked(True)
        window.alert_email_server_edit.setText("smtp.example:2525")
        window.alert_email_to_edit.setText("ops@example.com")
        window.alert_email_from_edit.setText("npd@example.com")
        window.alert_email_security_combo.setCurrentIndex(
            window.alert_email_security_combo.findData(main_window_module.ALERT_EMAIL_SECURITY_STARTTLS)
        )
        window.alert_email_user_edit.setText("alert-user")
        window.alert_email_password_env_edit.setText("NPD_SMTP_PASSWORD")
        window.alert_rest_action_check.setChecked(True)
        window.alert_rest_url_edit.setText("https://collector.example/alerts")
        window.alert_executable_action_check.setChecked(True)
        window.alert_executable_path_edit.setText(r"C:\Tools\alert.exe")

        window.save_alert_rule_preset()

        data = json.loads(preset_path.read_text(encoding="utf-8"))
        assert data["version"] == ALERT_RULE_PRESET_VERSION
        assert data["name"] == "voice_alerts"
        assert datetime.fromisoformat(data["created_at"])
        assert data["summary"] == {
            "active_rule_count": 4,
            "active_action_count": 7,
            "action_phase_count": 1,
            "external_action_count": 3,
            "route_adjustment_enabled": False,
        }
        assert data["rules"]["loss_enabled"] is False
        assert data["rules"]["loss_threshold_percent"] == 35
        assert data["rules"]["latency_enabled"] is True
        assert data["rules"]["jitter_enabled"] is False
        assert data["rules"]["sample_enabled"] is True
        assert data["rules"]["timer_enabled"] is False
        assert data["rules"]["route_ip"] == "203.0.113.50"
        assert data["actions"]["start"] is True
        assert data["actions"]["end"] is False
        assert data["actions"]["route_adjustment"] is False
        assert data["actions"]["timeline"] is False
        assert data["actions"]["log"] is True
        assert data["actions"]["email"] is True
        assert data["actions"]["email_server"] == "smtp.example:2525"
        assert data["actions"]["email_to"] == "ops@example.com"
        assert data["actions"]["email_from"] == "npd@example.com"
        assert data["actions"]["email_security"] == main_window_module.ALERT_EMAIL_SECURITY_STARTTLS
        assert data["actions"]["email_username"] == "alert-user"
        assert data["actions"]["email_password_env"] == "NPD_SMTP_PASSWORD"
        assert data["actions"]["executable_path"] == r"C:\Tools\alert.exe"

        window.loss_alert_check.setChecked(True)
        window.latency_alert_check.setChecked(False)
        window.jitter_alert_check.setChecked(True)
        window.sample_alert_check.setChecked(False)
        window.timer_alert_check.setChecked(True)
        window.loss_threshold_spin.setValue(1)
        window.loss_window_spin.setValue(1)
        window.latency_threshold_spin.setValue(1)
        window.jitter_threshold_spin.setValue(1)
        window.sample_window_spin.setValue(1)
        window.sample_bad_spin.setValue(1)
        window.timer_window_spin.setValue(1)
        window.mos_alert_check.setChecked(False)
        window.mos_threshold_spin.setValue(1.0)
        window.mos_window_spin.setValue(1)
        window.route_ip_alert_check.setChecked(False)
        window.route_ip_alert_edit.clear()
        window.alert_start_action_check.setChecked(False)
        window.alert_end_action_check.setChecked(True)
        window.alert_route_adjust_action_check.setChecked(True)
        window.alert_timeline_action_check.setChecked(True)
        window.alert_comment_action_check.setChecked(False)
        window.alert_log_action_check.setChecked(False)
        window.alert_beep_action_check.setChecked(False)
        window.alert_image_action_check.setChecked(False)
        window.alert_email_action_check.setChecked(False)
        window.alert_email_server_edit.clear()
        window.alert_email_to_edit.clear()
        window.alert_email_from_edit.clear()
        window.alert_email_security_combo.setCurrentIndex(
            window.alert_email_security_combo.findData(main_window_module.ALERT_EMAIL_SECURITY_PLAIN)
        )
        window.alert_email_user_edit.clear()
        window.alert_email_password_env_edit.clear()
        window.alert_rest_action_check.setChecked(False)
        window.alert_rest_url_edit.clear()
        window.alert_executable_action_check.setChecked(False)
        window.alert_executable_path_edit.clear()

        window.load_alert_rule_preset()

        assert window.loss_alert_check.isChecked() is False
        assert window.loss_threshold_spin.value() == 35
        assert window.loss_window_spin.value() == 7
        assert window.latency_alert_check.isChecked() is True
        assert window.latency_threshold_spin.value() == 180
        assert window.jitter_alert_check.isChecked() is False
        assert window.jitter_threshold_spin.value() == 45
        assert window.sample_alert_check.isChecked() is True
        assert window.sample_window_spin.value() == 12
        assert window.sample_bad_spin.value() == 8
        assert window.timer_alert_check.isChecked() is False
        assert window.timer_window_spin.value() == 9
        assert window.mos_alert_check.isChecked() is True
        assert window.mos_threshold_spin.value() == 3.2
        assert window.mos_window_spin.value() == 4
        assert window.route_ip_alert_check.isChecked() is True
        assert window.route_ip_alert_edit.text() == "203.0.113.50"
        assert window.alert_start_action_check.isChecked() is True
        assert window.alert_end_action_check.isChecked() is False
        assert window.alert_route_adjust_action_check.isChecked() is False
        assert window.alert_timeline_action_check.isChecked() is False
        assert window.alert_comment_action_check.isChecked() is True
        assert window.alert_log_action_check.isChecked() is True
        assert window.alert_beep_action_check.isChecked() is True
        assert window.alert_image_action_check.isChecked() is True
        assert window.alert_email_action_check.isChecked() is True
        assert window.alert_email_server_edit.text() == "smtp.example:2525"
        assert window.alert_email_to_edit.text() == "ops@example.com"
        assert window.alert_email_from_edit.text() == "npd@example.com"
        assert window.alert_email_security_combo.currentData() == main_window_module.ALERT_EMAIL_SECURITY_STARTTLS
        assert window.alert_email_user_edit.text() == "alert-user"
        assert window.alert_email_password_env_edit.text() == "NPD_SMTP_PASSWORD"
        assert window.alert_rest_action_check.isChecked() is True
        assert window.alert_rest_url_edit.text() == "https://collector.example/alerts"
        assert window.alert_executable_action_check.isChecked() is True
        assert window.alert_executable_path_edit.text() == r"C:\Tools\alert.exe"
        assert window.status_label.text() == f"Alert preset loaded: {preset_path} | voice_alerts | rules 4 | actions 7"
    finally:
        window.close()


def test_main_window_loads_legacy_alert_rule_preset(qt_app, tmp_path, monkeypatch) -> None:
    preset_path = tmp_path / "legacy_alerts.json"
    preset_path.write_text(
        json.dumps(
            {
                "version": 1,
                "rules": {
                    "latency_enabled": True,
                    "latency_threshold_ms": 250,
                    "loss_enabled": False,
                },
                "actions": {
                    "start": True,
                    "end": False,
                    "log": True,
                },
            }
        ),
        encoding="utf-8",
    )

    def fake_get_open_file_name(*_args, **_kwargs):
        return str(preset_path), "JSON Files (*.json)"

    monkeypatch.setattr(main_window_module.QFileDialog, "getOpenFileName", fake_get_open_file_name)
    window = MainWindow()

    try:
        window.latency_alert_check.setChecked(False)
        window.latency_threshold_spin.setValue(1)
        window.alert_log_action_check.setChecked(False)

        window.load_alert_rule_preset()

        assert window.latency_alert_check.isChecked() is True
        assert window.latency_threshold_spin.value() == 250
        assert window.loss_alert_check.isChecked() is False
        assert window.alert_log_action_check.isChecked() is True
        assert window.status_label.text() == f"Alert preset loaded: {preset_path} | rules 1 | actions 1"
    finally:
        window.close()


def test_main_window_rejects_alert_preset_summary_count_mismatch(qt_app, tmp_path, monkeypatch) -> None:
    preset_path = tmp_path / "bad_alerts.json"
    preset_path.write_text(
        json.dumps(
            {
                "version": ALERT_RULE_PRESET_VERSION,
                "name": "bad_alerts",
                "summary": {
                    "active_rule_count": 2,
                    "active_action_count": 1,
                    "action_phase_count": 1,
                    "external_action_count": 0,
                    "route_adjustment_enabled": False,
                },
                "rules": {
                    "latency_enabled": True,
                    "loss_enabled": False,
                },
                "actions": {
                    "start": True,
                    "end": False,
                    "log": True,
                },
            }
        ),
        encoding="utf-8",
    )

    def fake_get_open_file_name(*_args, **_kwargs):
        return str(preset_path), "JSON Files (*.json)"

    monkeypatch.setattr(main_window_module.QFileDialog, "getOpenFileName", fake_get_open_file_name)
    window = MainWindow()
    warnings: list[str] = []
    monkeypatch.setattr(main_window_module.QMessageBox, "warning", lambda *_args: warnings.append(str(_args[-1])))

    try:
        window.latency_alert_check.setChecked(False)

        window.load_alert_rule_preset()

        assert "active_rule_count does not match" in warnings[-1]
        assert "active_rule_count does not match" in window.status_label.text()
        assert window.latency_alert_check.isChecked() is False
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
        window.alert_route_adjust_action_check.setChecked(False)
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


def test_main_window_alert_log_action_writes_without_annotation(qt_app, tmp_path) -> None:
    window = MainWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    history = [
        HopObservation(now, 0, "198.51.100.10", "Target", True, 95.0, STATUS_OK, True),
    ]
    target_snapshot = _snapshot(0, "198.51.100.10", None, latency=95.0, is_target=True)

    try:
        window.current_target = "198.51.100.10"
        window.alert_action_log_path = tmp_path / "session.alerts.csv"
        window.loss_threshold_spin.setValue(100)
        window.latency_threshold_spin.setValue(80)
        window.alert_route_adjust_action_check.setChecked(False)
        window.alert_timeline_action_check.setChecked(False)
        window.alert_comment_action_check.setChecked(False)
        window.alert_log_action_check.setChecked(True)

        window.on_measurement_updated([], target_snapshot, [target_snapshot], ["live"], history, history)

        rows = read_alert_actions(window.alert_action_log_path)
        assert rows[0]["title"] == "Latency alert"
        assert rows[0]["actions"] == "log"
        assert "Latency alert" in window.alerts_box.toPlainText()
        assert window.graph._annotations == []
        assert window.annotations_for_export() == []
    finally:
        window.close()


def test_main_window_alert_rest_action_posts_event_payload(qt_app, tmp_path, monkeypatch) -> None:
    posted: list[tuple[str, dict[str, object]]] = []
    window = MainWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    history = [
        HopObservation(now, 0, "198.51.100.10", "Target", True, 90.0, STATUS_OK, True),
    ]
    target_snapshot = _snapshot(0, "198.51.100.10", None, latency=90.0, is_target=True)

    def fake_post(url: str, payload: dict[str, object]) -> None:
        posted.append((url, payload))

    monkeypatch.setattr(window, "_post_alert_webhook", fake_post)

    try:
        window.current_target = "198.51.100.10"
        window.alert_action_log_path = tmp_path / "session.alerts.csv"
        window.loss_threshold_spin.setValue(100)
        window.latency_threshold_spin.setValue(80)
        window.alert_route_adjust_action_check.setChecked(False)
        window.alert_timeline_action_check.setChecked(False)
        window.alert_comment_action_check.setChecked(False)
        window.alert_rest_action_check.setChecked(True)
        window.alert_rest_url_edit.setText("https://collector.example/alerts")

        window.on_measurement_updated([], target_snapshot, [target_snapshot], ["live"], history, history)

        rows = read_alert_actions(window.alert_action_log_path)
        assert rows[0]["title"] == "Latency alert"
        assert rows[0]["actions"] == "rest"
        assert posted == [
            (
                "https://collector.example/alerts",
                {
                    "key": "target_latency_100ms",
                    "timestamp": "2026-01-01T12:00:00",
                    "start": "2026-01-01T12:00:00",
                    "end": "2026-01-01T12:00:00",
                    "severity": "warning",
                    "title": "Latency alert",
                    "message": "Target latency 90.0 ms >= 80 ms",
                    "target": "198.51.100.10",
                    "series_key": "target",
                },
            )
        ]
    finally:
        window.close()


def test_main_window_alert_email_action_sends_event_message(qt_app, tmp_path, monkeypatch) -> None:
    sent: list[dict[str, object]] = []
    window = MainWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    history = [
        HopObservation(now, 0, "198.51.100.10", "Target", True, 90.0, STATUS_OK, True),
    ]
    target_snapshot = _snapshot(0, "198.51.100.10", None, latency=90.0, is_target=True)

    def fake_send_email(
        host: str,
        port: int,
        sender: str,
        recipient: str,
        subject: str,
        body: str,
        *,
        security: str,
        username: str,
        password_env: str,
    ) -> None:
        sent.append(
            {
                "host": host,
                "port": port,
                "sender": sender,
                "recipient": recipient,
                "subject": subject,
                "body": body,
                "security": security,
                "username": username,
                "password_env": password_env,
            }
        )

    monkeypatch.setattr(window, "_send_alert_email", fake_send_email)

    try:
        window.current_target = "198.51.100.10"
        window.alert_action_log_path = tmp_path / "session.alerts.csv"
        window.loss_threshold_spin.setValue(100)
        window.latency_threshold_spin.setValue(80)
        window.alert_route_adjust_action_check.setChecked(False)
        window.alert_timeline_action_check.setChecked(False)
        window.alert_comment_action_check.setChecked(False)
        window.alert_email_action_check.setChecked(True)
        window.alert_email_server_edit.setText("smtp.example:2525")
        window.alert_email_to_edit.setText("ops@example.com")
        window.alert_email_from_edit.setText("npd@example.com")
        window.alert_email_security_combo.setCurrentIndex(
            window.alert_email_security_combo.findData(main_window_module.ALERT_EMAIL_SECURITY_STARTTLS)
        )
        window.alert_email_user_edit.setText("alert-user")
        window.alert_email_password_env_edit.setText("NPD_SMTP_PASSWORD")

        window.on_measurement_updated([], target_snapshot, [target_snapshot], ["live"], history, history)

        rows = read_alert_actions(window.alert_action_log_path)
        assert rows[0]["title"] == "Latency alert"
        assert rows[0]["actions"] == "email"
        assert sent
        assert sent[0]["host"] == "smtp.example"
        assert sent[0]["port"] == 2525
        assert sent[0]["sender"] == "npd@example.com"
        assert sent[0]["recipient"] == "ops@example.com"
        assert sent[0]["security"] == main_window_module.ALERT_EMAIL_SECURITY_STARTTLS
        assert sent[0]["username"] == "alert-user"
        assert sent[0]["password_env"] == "NPD_SMTP_PASSWORD"
        assert "Latency alert" in str(sent[0]["subject"])
        assert "Target: 198.51.100.10" in str(sent[0]["body"])
        assert "Target latency 90.0 ms >= 80 ms" in str(sent[0]["body"])
    finally:
        window.close()


def test_main_window_send_alert_email_supports_starttls_auth(qt_app, monkeypatch) -> None:
    calls: list[tuple[object, ...]] = []

    class FakeSmtp:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            calls.append(("init", host, port, timeout))

        def __enter__(self):
            calls.append(("enter",))
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            calls.append(("exit",))

        def starttls(self) -> None:
            calls.append(("starttls",))

        def login(self, username: str, password: str) -> None:
            calls.append(("login", username, password))

        def send_message(self, message) -> None:
            calls.append(("send", message["From"], message["To"], message["Subject"]))

    monkeypatch.setattr(main_window_module.smtplib, "SMTP", FakeSmtp)
    monkeypatch.setenv("NPD_SMTP_PASSWORD", "test-password")
    window = MainWindow()

    try:
        window._send_alert_email(
            "smtp.example",
            587,
            "npd@example.com",
            "ops@example.com",
            "Latency alert",
            "body",
            security=main_window_module.ALERT_EMAIL_SECURITY_STARTTLS,
            username="alert-user",
            password_env="NPD_SMTP_PASSWORD",
        )

        assert calls == [
            ("init", "smtp.example", 587, main_window_module.ALERT_EMAIL_TIMEOUT_SECONDS),
            ("enter",),
            ("starttls",),
            ("login", "alert-user", "test-password"),
            ("send", "npd@example.com", "ops@example.com", "Latency alert"),
            ("exit",),
        ]
    finally:
        window.close()


def test_main_window_alert_executable_action_launches_configured_file(qt_app, tmp_path, monkeypatch) -> None:
    launched: list[tuple[Path, str, str, str]] = []
    window = MainWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    executable_path = tmp_path / "alert-action.exe"
    executable_path.write_bytes(b"stub")
    history = [
        HopObservation(now, 0, "198.51.100.10", "Target", True, 90.0, STATUS_OK, True),
    ]
    target_snapshot = _snapshot(0, "198.51.100.10", None, latency=90.0, is_target=True)

    def fake_launch(path: Path, event: AlertEvent, env: dict[str, str]) -> None:
        launched.append((path, event.title, env["NPD_ALERT_TARGET"], env["NPD_ALERT_MESSAGE"]))

    monkeypatch.setattr(window, "_launch_alert_executable", fake_launch)

    try:
        window.current_target = "198.51.100.10"
        window.alert_action_log_path = tmp_path / "session.alerts.csv"
        window.loss_threshold_spin.setValue(100)
        window.latency_threshold_spin.setValue(80)
        window.alert_route_adjust_action_check.setChecked(False)
        window.alert_timeline_action_check.setChecked(False)
        window.alert_comment_action_check.setChecked(False)
        window.alert_executable_action_check.setChecked(True)
        window.alert_executable_path_edit.setText(str(executable_path))

        window.on_measurement_updated([], target_snapshot, [target_snapshot], ["live"], history, history)

        rows = read_alert_actions(window.alert_action_log_path)
        assert rows[0]["title"] == "Latency alert"
        assert rows[0]["actions"] == "executable"
        assert launched == [
            (
                executable_path,
                "Latency alert",
                "198.51.100.10",
                "Target latency 90.0 ms >= 80 ms",
            )
        ]
    finally:
        window.close()


def test_main_window_marks_failed_external_alert_actions_in_log(qt_app, tmp_path, monkeypatch) -> None:
    window = MainWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    executable_path = tmp_path / "alert-action.exe"
    executable_path.write_bytes(b"stub")

    try:
        window.current_target = "198.51.100.10"
        window.alert_action_log_path = tmp_path / "session.alerts.csv"
        window.alert_route_adjust_action_check.setChecked(False)
        window.alert_timeline_action_check.setChecked(False)
        window.alert_comment_action_check.setChecked(False)
        window.alert_log_action_check.setChecked(False)
        window.alert_beep_action_check.setChecked(False)
        window.alert_image_action_check.setChecked(False)

        window.alert_rest_action_check.setChecked(True)
        window.alert_rest_url_edit.setText("https://collector.example/alerts")
        monkeypatch.setattr(window, "_post_alert_webhook", lambda *_args: (_ for _ in ()).throw(OSError("rest down")))
        assert window._record_alert_actions(
            AlertEvent("rest-key", now, now, now, "warning", "REST alert", "REST failed")
        ) == ["rest_failed"]

        window.alert_rest_action_check.setChecked(False)
        window.alert_email_action_check.setChecked(True)
        window.alert_email_server_edit.setText("smtp.example:2525")
        window.alert_email_to_edit.setText("ops@example.com")
        monkeypatch.setattr(window, "_send_alert_email", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("smtp down")))
        assert window._record_alert_actions(
            AlertEvent("email-key", now, now, now, "warning", "Email alert", "Email failed")
        ) == ["email_failed"]

        window.alert_email_action_check.setChecked(False)
        window.alert_executable_action_check.setChecked(True)
        window.alert_executable_path_edit.setText(str(executable_path))
        monkeypatch.setattr(window, "_launch_alert_executable", lambda *_args: (_ for _ in ()).throw(OSError("run down")))
        assert window._record_alert_actions(
            AlertEvent("exe-key", now, now, now, "warning", "Run alert", "Run failed")
        ) == ["executable_failed"]

        rows = read_alert_actions(window.alert_action_log_path)
        assert [row["actions"] for row in rows] == ["rest_failed", "email_failed", "executable_failed"]
        assert window.status_label.text().startswith("Alert executable action failed:")
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
        assert window.alert_table.rowCount() == 2
        assert {
            window.alert_table.item(row, 2).text()
            for row in range(window.alert_table.rowCount())
        } == {"Sample count alert", "Alert ended"}
        assert {
            window.alert_table.item(row, 1).text()
            for row in range(window.alert_table.rowCount())
        } == {"CRITICAL", "INFO"}
        assert [row["title"] for row in rows] == ["Sample count alert", "Alert ended"]
        assert rows[1]["message"] == "Sample count alert recovered"
    finally:
        window.close()


def test_main_window_can_disable_recovery_alert_actions(qt_app, tmp_path) -> None:
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
        window.alert_end_action_check.setChecked(False)

        window.on_measurement_updated([], target_snapshot, [target_snapshot], ["live"], bad_history, bad_history)
        window.on_measurement_updated([], target_snapshot, [target_snapshot], ["live"], good_history, good_history)

        rows = read_alert_actions(window.alert_action_log_path)
        assert [event.title for event in window.alert_events] == ["Sample count alert", "Alert ended"]
        assert [row["title"] for row in rows] == ["Sample count alert"]
        assert window.alert_event_actions[window.alert_events[0].key] == ["timeline_annotation", "comment"]
        assert window.alert_event_actions[window.alert_events[1].key] == []
        assert [annotation.label for annotation in window.graph._annotations] == ["Sample count alert"]
    finally:
        window.close()


def test_main_window_records_jitter_alert_action(qt_app, tmp_path) -> None:
    window = MainWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    history = [
        HopObservation(now, 0, "198.51.100.10", "Target", True, 10.0, STATUS_OK, True),
        HopObservation(now + timedelta(seconds=1), 0, "198.51.100.10", "Target", True, 60.0, STATUS_OK, True),
        HopObservation(now + timedelta(seconds=2), 0, "198.51.100.10", "Target", True, 11.0, STATUS_OK, True),
        HopObservation(now + timedelta(seconds=3), 0, "198.51.100.10", "Target", True, 61.0, STATUS_OK, True),
    ]
    target_snapshot = _snapshot(0, "198.51.100.10", None, latency=61.0, is_target=True)

    try:
        window.current_target = "198.51.100.10"
        window.alert_action_log_path = tmp_path / "session.alerts.csv"
        window.loss_threshold_spin.setValue(100)
        window.latency_threshold_spin.setValue(1000)
        window.jitter_threshold_spin.setValue(20)
        window.sample_window_spin.setValue(4)
        window.sample_bad_spin.setValue(4)

        window.on_measurement_updated([], target_snapshot, [target_snapshot], ["live"], history, history)

        rows = read_alert_actions(window.alert_action_log_path)
        assert "Jitter alert" in window.alerts_box.toPlainText()
        assert [row["title"] for row in rows] == ["Jitter alert"]
        assert rows[0]["message"] == "Target jitter 28.9 ms >= 20 ms over last 4 samples"
        assert window.graph._annotations[0].label == "Jitter alert"
    finally:
        window.close()


def test_main_window_records_mos_alert_when_enabled(qt_app, tmp_path) -> None:
    window = MainWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    history = [
        HopObservation(now, 0, "198.51.100.10", "Target", True, 160.0, STATUS_OK, True),
        HopObservation(now + timedelta(seconds=20), 0, "198.51.100.10", "Target", False, None, STATUS_TIMEOUT, True),
        HopObservation(now + timedelta(seconds=40), 0, "198.51.100.10", "Target", True, 240.0, STATUS_OK, True),
        HopObservation(now + timedelta(seconds=60), 0, "198.51.100.10", "Target", False, None, STATUS_TIMEOUT, True),
    ]
    target_snapshot = _snapshot(0, "198.51.100.10", None, latency=None, is_target=True, status=STATUS_TIMEOUT)

    try:
        window.current_target = "198.51.100.10"
        window.alert_action_log_path = tmp_path / "session.alerts.csv"
        window.loss_threshold_spin.setValue(100)
        window.latency_threshold_spin.setValue(1000)
        window.jitter_threshold_spin.setValue(1000)
        window.sample_window_spin.setValue(4)
        window.sample_bad_spin.setValue(4)
        window.timer_window_spin.setValue(5)
        window.mos_alert_check.setChecked(True)
        window.mos_threshold_spin.setValue(3.5)
        window.mos_window_spin.setValue(1)

        window.on_measurement_updated([], target_snapshot, [target_snapshot], ["live"], history, history)

        rows = read_alert_actions(window.alert_action_log_path)
        assert "MOS alert" in window.alerts_box.toPlainText()
        assert [row["title"] for row in rows] == ["MOS alert"]
        assert rows[0]["message"].startswith("Estimated MOS ")
        assert window.graph._annotations[0].label == "MOS alert"
    finally:
        window.close()


def test_main_window_records_route_ip_alert_and_recovery(qt_app, tmp_path) -> None:
    window = MainWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    first_history = [
        HopObservation(now, 0, "198.51.100.10", "Target", True, 20.0, STATUS_OK, True),
    ]
    second_history = [
        HopObservation(now + timedelta(seconds=30), 0, "198.51.100.10", "Target", True, 21.0, STATUS_OK, True),
    ]
    first_snapshots = [
        _snapshot(1, "192.0.2.1", "gateway"),
        _snapshot(2, "203.0.113.50", "vpn"),
    ]
    second_snapshots = [
        _snapshot(1, "192.0.2.1", "gateway"),
        _snapshot(2, "203.0.113.99", "backup"),
    ]
    target_snapshot = _snapshot(0, "198.51.100.10", None, latency=21.0, is_target=True)

    try:
        window.current_target = "198.51.100.10"
        window.alert_action_log_path = tmp_path / "session.alerts.csv"
        window.route_ip_alert_check.setChecked(True)
        window.route_ip_alert_edit.setText("203.0.113.50")

        window.on_measurement_updated(first_snapshots, target_snapshot, [target_snapshot], ["live"], first_history, first_history)
        window.on_measurement_updated(second_snapshots, target_snapshot, [target_snapshot], ["live"], second_history, second_history)

        rows = read_alert_actions(window.alert_action_log_path)
        assert [event.title for event in window.alert_events] == ["Route IP alert", "Alert ended"]
        assert "Route IP alert" in window.alerts_box.toPlainText()
        assert [row["title"] for row in rows] == ["Route IP alert", "Alert ended"]
        assert [row["source"] for row in rows] == ["route", "route"]
        assert rows[0]["message"] == "Watched IP 203.0.113.50 appeared in route at Hop 2"
        assert rows[1]["message"] == "Route IP alert recovered"
        assert [annotation.label for annotation in window.graph._annotations] == ["Route IP alert", "Alert ended"]
        assert window.annotations_for_export()[0].source == "route"
    finally:
        window.close()


def test_main_window_records_timer_alert_and_recovery(qt_app, tmp_path) -> None:
    window = MainWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    bad_history = [
        HopObservation(now, 0, "198.51.100.10", "Target", False, None, STATUS_TIMEOUT, True),
        HopObservation(now + timedelta(seconds=60), 0, "198.51.100.10", "Target", False, None, STATUS_TIMEOUT, True),
        HopObservation(now + timedelta(seconds=120), 0, "198.51.100.10", "Target", False, None, STATUS_TIMEOUT, True),
    ]
    good_history = [
        HopObservation(now + timedelta(seconds=180), 0, "198.51.100.10", "Target", True, 10.0, STATUS_OK, True),
    ]
    target_snapshot = _snapshot(0, "198.51.100.10", None, latency=None, is_target=True, status=STATUS_TIMEOUT)

    try:
        window.current_target = "198.51.100.10"
        window.alert_action_log_path = tmp_path / "session.alerts.csv"
        window.loss_threshold_spin.setValue(100)
        window.loss_window_spin.setValue(3)
        window.latency_threshold_spin.setValue(100)
        window.sample_window_spin.setValue(10)
        window.sample_bad_spin.setValue(10)
        window.timer_window_spin.setValue(1)

        window.on_measurement_updated([], target_snapshot, [target_snapshot], ["live"], bad_history, bad_history)
        window.on_measurement_updated([], target_snapshot, [target_snapshot], ["live"], good_history, good_history)

        rows = read_alert_actions(window.alert_action_log_path)
        assert [event.title for event in window.alert_events] == ["Timer alert", "Alert ended"]
        assert "Timer alert" in window.alerts_box.toPlainText()
        assert [row["title"] for row in rows] == ["Timer alert", "Alert ended"]
        assert rows[0]["message"] == "Target stayed failed or >= 100 ms for 1m"
        assert rows[1]["message"] == "Timer alert recovered"
    finally:
        window.close()


def test_main_window_alert_image_action_saves_graph_after_render(qt_app, tmp_path, monkeypatch) -> None:
    window = MainWindow()
    saved_paths: list[Path] = []
    now = datetime(2026, 1, 1, 12, 0, 0)
    history = [
        HopObservation(now, 0, "198.51.100.10", "Target", True, 90.0, STATUS_OK, True),
    ]
    target_snapshot = _snapshot(0, "198.51.100.10", None, latency=90.0, is_target=True)

    def fake_save_graph_png(path: Path) -> Path:
        assert window.graph._points == history
        assert window.graph._annotations[0].label == "Latency alert"
        saved_paths.append(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"png")
        return path

    monkeypatch.setattr(window, "_save_graph_png", fake_save_graph_png)

    try:
        window.current_target = "198.51.100.10"
        window.alert_action_log_path = tmp_path / "session.alerts.csv"
        window.loss_threshold_spin.setValue(100)
        window.latency_threshold_spin.setValue(80)
        window.alert_image_action_check.setChecked(True)

        window.on_measurement_updated([], target_snapshot, [target_snapshot], ["live"], history, history)

        rows = read_alert_actions(window.alert_action_log_path)
        assert len(saved_paths) == 1
        assert saved_paths[0].parent == tmp_path / "alert_images"
        assert saved_paths[0].suffix == ".png"
        assert rows[0]["actions"] == "timeline_annotation;comment;image"
        assert rows[0]["title"] == "Latency alert"
        assert window.pending_alert_image_keys == set()
        assert window.status_label.text() == f"Alert image saved: {saved_paths[0]}"
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
        self.target_interval_updates: list[tuple[list[str], int]] = []

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

    def set_target_interval_seconds(self, targets: list[str], interval_seconds: int) -> None:
        self.target_interval_updates.append((list(targets), interval_seconds))

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
