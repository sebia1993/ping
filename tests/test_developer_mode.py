from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QPoint, QThread, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton

from app.ui.main_window import MainWindow


def _show(window: MainWindow, qt_app: QApplication) -> None:
    window.resize(1366, 768)
    window.show()
    window.activateWindow()
    qt_app.processEvents()


def test_f12_toggles_developer_mode_from_text_input_and_cleans_inspector(qt_app) -> None:
    window = MainWindow()
    try:
        _show(window, qt_app)
        window.target_input.setFocus()
        assert window.developer_mode.active is False
        assert window.developer_mode.panel is None
        assert window.developer_mode.shortcut.autoRepeat() is False

        QTest.keyClick(window.target_input, Qt.Key_F12)
        qt_app.processEvents()

        assert window.developer_mode.active is True
        assert window.developer_mode.dock is not None
        assert window.developer_mode.dock.isVisible()
        assert window.developer_mode.inspector is not None
        assert window.developer_mode.inspector._event_filter_installed is True
        assert window.developer_mode.inspector._hover_timer.isActive() is True
        panel = window.developer_mode.panel
        assert panel is not None
        copy_top_left = panel.current_copy_button.mapTo(window, QPoint(0, 0))
        assert copy_top_left.y() >= 0
        assert copy_top_left.y() + panel.current_copy_button.height() <= window.height()
        reset_top_left = panel.reset_button.mapTo(window, QPoint(0, 0))
        assert reset_top_left.y() >= 0
        assert reset_top_left.y() + panel.reset_button.height() <= window.height()

        QTest.keyClick(window.target_input, Qt.Key_F12)
        qt_app.processEvents()

        assert window.developer_mode.active is False
        assert window.developer_mode.dock.isVisible() is False
        assert window.developer_mode.inspector._event_filter_installed is False
        assert window.developer_mode.inspector._hover_timer.isActive() is False
        assert window.developer_mode.inspector._rubber_band.isVisible() is False
    finally:
        window.close()


def test_inspection_click_selects_widget_without_running_original_action(qt_app) -> None:
    window = MainWindow()
    clicks: list[bool] = []
    try:
        _show(window, qt_app)
        button = QPushButton("테스트 동작", window.centralWidget())
        button.setGeometry(40, 240, 120, 36)
        button.clicked.connect(lambda: clicks.append(True))
        button.show()
        window.developer_mode.enable()
        qt_app.processEvents()

        QTest.mouseClick(button, Qt.LeftButton)
        qt_app.processEvents()

        inspector = window.developer_mode.inspector
        assert inspector is not None
        assert clicks == []
        assert inspector.selected_widget is button
        assert window.developer_mode.panel is not None
        assert "QPushButton" in window.developer_mode.panel.ui_info_edit.toPlainText()
        dock = window.developer_mode.dock
        assert dock is not None
        assert inspector._id_label.geometry().right() < dock.geometry().left()

        QTest.keyClick(button, Qt.Key_Escape)
        qt_app.processEvents()
        assert inspector.selected_widget is None
    finally:
        window.close()


def test_registered_ui_ids_are_unique_and_dynamic_target_ids_do_not_expose_ip(qt_app) -> None:
    window = MainWindow()
    try:
        window._create_target_graph_row("192.168.10.20", use_primary_graph=True)
        window._create_target_graph_row("192.168.10.21", use_primary_graph=False)

        identifiers = window.developer_mode.registry.registered_ui_ids()
        assert len(identifiers) == len(set(identifiers))
        assert "main_dashboard.measurement.start_button" in identifiers
        assert any(identifier.endswith(".pause_button") for identifier in identifiers)
        assert all("192.168.10" not in identifier for identifier in identifiers)
    finally:
        window.close()


def test_ui_request_masks_current_private_ip_and_copies_to_clipboard(qt_app) -> None:
    window = MainWindow()
    try:
        _show(window, qt_app)
        window.target_input.setPlainText("192.168.10.20")
        window.developer_mode.enable()
        panel = window.developer_mode.panel
        inspector = window.developer_mode.inspector
        assert panel is not None
        assert inspector is not None
        panel.include_values_check.setChecked(True)
        inspector.select_widget(window.target_input)
        panel.ui_desired_edit.setPlainText("입력칸 높이를 조금 늘려 주세요.")

        prompt = panel.generate_ui_request(copy_to_clipboard=True)

        assert "[Codex UI 수정 요청]" in prompt
        assert "main_dashboard.targets.input" in prompt
        assert "192.168.10.20" not in prompt
        assert "INTERNAL_IP_01" in prompt
        assert QApplication.clipboard().text() == prompt
        assert panel.history_table.rowCount() == 1

        inspector.clear_selection()
        panel.history_table.selectRow(0)
        panel.edit_history_record()
        regenerated = panel.generate_ui_request(copy_to_clipboard=False)
        assert "main_dashboard.targets.input" in regenerated
        assert "입력칸 높이를 조금 늘려 주세요." in regenerated
    finally:
        window.close()


def test_feature_and_error_requests_include_registered_function_and_last_error(qt_app) -> None:
    window = MainWindow()
    try:
        _show(window, qt_app)
        window.developer_mode.enable()
        panel = window.developer_mode.panel
        assert panel is not None
        panel.tabs.setCurrentIndex(1)
        panel._select_feature(panel.feature_combo, "measurement.start")
        panel.feature_desired_edit.setPlainText("시작 실패 시 사용자 안내를 더 명확히 표시")

        feature_prompt = panel.generate_feature_request(copy_to_clipboard=False)

        assert "기능 ID: measurement.start" in feature_prompt
        assert f"Git 커밋: {window.developer_mode.build_info.git_commit}" in feature_prompt
        assert "실행하지 못한 테스트를 완료했다고 표현하지 말 것" in feature_prompt

        window.developer_mode.record_error(
            "target 10.20.30.40 password=secret timeout",
            error_type="TimeoutError",
            feature_id="measurement.start",
        )
        panel.tabs.setCurrentIndex(2)
        panel.error_actual_edit.setPlainText("측정 시작 후 오류 창이 표시됨")
        panel.error_steps_edit.setPlainText("IP 입력\n시작 클릭")

        error_prompt = panel.generate_error_request(copy_to_clipboard=False)

        assert "[Codex 오류 수정 요청]" in error_prompt
        assert "TimeoutError" in error_prompt
        assert "10.20.30.40" not in error_prompt
        assert "secret" not in error_prompt
        assert "INTERNAL_IP_01" in error_prompt
        assert "[MASKED_SECRET]" in error_prompt
    finally:
        window.close()


def test_switching_away_from_ui_tab_disables_click_interception_and_records_registered_feature(qt_app) -> None:
    window = MainWindow()
    try:
        _show(window, qt_app)
        window.developer_mode.enable()
        panel = window.developer_mode.panel
        inspector = window.developer_mode.inspector
        assert panel is not None
        assert inspector is not None

        panel.tabs.setCurrentIndex(1)
        qt_app.processEvents()
        assert inspector.inspection_enabled is False

        window.developer_mode.registry.record_feature("measurement.start", "성공")
        qt_app.processEvents()
        assert panel.recent_feature_list.count() == 1
        assert "measurement.start" in panel.recent_feature_list.item(0).text()
    finally:
        window.close()


def test_window_close_shuts_down_developer_mode_objects(qt_app) -> None:
    window = MainWindow()
    _show(window, qt_app)
    window.developer_mode.enable()
    inspector = window.developer_mode.inspector
    assert inspector is not None

    window.close()
    qt_app.processEvents()

    assert window.developer_mode.active is False
    assert window.developer_mode.shortcut.isEnabled() is False
    assert inspector._event_filter_installed is False
    assert inspector._hover_timer.isActive() is False


def test_background_log_updates_developer_panel_only_on_ui_thread(qt_app) -> None:
    window = MainWindow()
    try:
        _show(window, qt_app)
        window.developer_mode.enable()
        panel = window.developer_mode.panel
        assert panel is not None
        update_threads: list[QThread] = []
        original_refresh = panel.refresh_last_error

        def capture_refresh() -> None:
            update_threads.append(QThread.currentThread())
            original_refresh()

        panel.refresh_last_error = capture_refresh
        record = logging.LogRecord(
            name="test.background",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="background failure",
            args=(),
            exc_info=None,
        )
        thread = threading.Thread(target=lambda: window.developer_mode._log_handler.emit(record))
        thread.start()
        thread.join(timeout=1.0)

        assert thread.is_alive() is False
        assert update_threads == []
        qt_app.processEvents()
        assert update_threads == [qt_app.thread()]
    finally:
        window.close()
