from __future__ import annotations

import hashlib
import logging
import platform
from collections import deque
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QCheckBox,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.developer.build_info import load_build_info
from app.developer.history import (
    ENVIRONMENT_KINDS,
    DeveloperHistoryError,
    DeveloperPreferencesStore,
    RequestHistoryStore,
)
from app.developer.inspector import UiInspector, UiSelection, format_inspection, inspect_widget
from app.developer.masking import SensitiveDataMasker
from app.developer.models import BuildInfo, ErrorSnapshot, RequestRecord, RuntimeInfo, UNKNOWN_VALUE
from app.developer.registry import DeveloperRegistry, FeatureMetadata, UiMetadata
from app.developer.request_builder import (
    RequestContext,
    RequestIdFactory,
    build_error_request,
    build_feature_request,
    build_technical_information,
    build_ui_request,
)


DEVELOPER_PANEL_OBJECT_NAME = "developerModeDock"
DEVELOPER_PANEL_MINIMUM_WIDTH = 390
DEVELOPER_PANEL_DEFAULT_WIDTH = 440
RECENT_LOG_LIMIT = 50


class _DeveloperLogRelay(QObject):
    """Marshal log records from worker threads onto the Qt UI thread."""

    record_received = Signal(object)


class DeveloperModeController:
    """Own the dormant-by-default F12 developer mode lifecycle."""

    def __init__(self, window: QMainWindow) -> None:
        self.window = window
        self.registry = DeveloperRegistry(recent_limit=20)
        self.history_store = RequestHistoryStore()
        self.preferences_store = DeveloperPreferencesStore()
        self.active = False
        self.dock: QDockWidget | None = None
        self.panel: DeveloperPanel | None = None
        self.inspector: UiInspector | None = None
        self._build_info: BuildInfo | None = None
        self._last_error: ErrorSnapshot | None = None
        self._recent_logs: deque[str] = deque(maxlen=RECENT_LOG_LIMIT)
        self._log_relay = _DeveloperLogRelay(window)
        self._log_relay.record_received.connect(self._on_log_record, Qt.QueuedConnection)
        self._log_handler = _DeveloperLogHandler(self._log_relay)
        self._toast: QLabel | None = None
        self._toast_timer = QTimer(window)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(self._hide_toast)

        self.shortcut = QShortcut(QKeySequence(Qt.Key_F12), window)
        self.shortcut.setContext(Qt.ApplicationShortcut)
        self.shortcut.setAutoRepeat(False)
        self.shortcut.activated.connect(self.toggle)

        self._register_features()
        self._register_static_ui()
        self._connect_feature_tracking()

    @property
    def build_info(self) -> BuildInfo:
        if self._build_info is None:
            self._build_info = load_build_info()
        return self._build_info

    @property
    def last_error(self) -> ErrorSnapshot | None:
        return self._last_error

    def toggle(self) -> None:
        if self.active:
            self.disable()
        else:
            self.enable()

    def enable(self) -> None:
        if self.active:
            return
        self._ensure_panel()
        self.active = True
        self.registry.enabled = True
        logging.getLogger().addHandler(self._log_handler)
        assert self.inspector is not None
        assert self.panel is not None
        assert self.dock is not None
        self.inspector.start()
        self.panel.reset_transient_selection()
        self.panel.refresh_build_information()
        self.panel.refresh_functions()
        self.panel.refresh_recent_runs()
        self.panel.refresh_history()
        self.dock.show()
        self.dock.raise_()
        try:
            self.window.resizeDocks([self.dock], [DEVELOPER_PANEL_DEFAULT_WIDTH], Qt.Horizontal)
        except (AttributeError, RuntimeError):
            pass
        self.panel.set_inspection_for_current_tab()
        self._show_toast("개발자 모드 켜짐 — F12로 종료")

    def disable(self) -> None:
        if not self.active:
            return
        self.active = False
        self.registry.enabled = False
        logging.getLogger().removeHandler(self._log_handler)
        if self.inspector is not None:
            self.inspector.stop()
        if self.panel is not None:
            self.panel.reset_transient_selection()
        if self.dock is not None:
            self.dock.hide()
        self._show_toast("개발자 모드 꺼짐")

    def shutdown(self) -> None:
        self.disable()
        self.shortcut.setEnabled(False)
        logging.getLogger().removeHandler(self._log_handler)
        self._toast_timer.stop()
        if self._toast is not None:
            self._toast.hide()
            self._toast.deleteLater()
            self._toast = None
        if self.dock is not None:
            self.dock.hide()

    def record_error(
        self,
        message: str,
        *,
        error_type: str = "ApplicationError",
        feature_id: str = "",
        screen_name: str = "main_dashboard",
        application_terminated: bool = False,
    ) -> None:
        ui_id = ""
        if self.panel is not None:
            ui_id = self.panel.selected_ui_id()
        self._last_error = ErrorSnapshot(
            occurred_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            error_type=error_type or "ApplicationError",
            message=str(message or UNKNOWN_VALUE)[:4000],
            feature_id=feature_id or UNKNOWN_VALUE,
            screen_name=screen_name or "main_dashboard",
            ui_id=ui_id or UNKNOWN_VALUE,
            application_terminated=application_terminated,
        )
        if feature_id:
            self.registry.record_feature(feature_id, "실패")
        if self.panel is not None and self.active:
            self.panel.refresh_last_error()

    def register_target_row(self, address: str, widgets: dict[str, QWidget]) -> None:
        opaque_key = hashlib.blake2s(address.encode("utf-8", errors="replace"), digest_size=6).hexdigest()
        base = f"main_dashboard.live_graph.target_{opaque_key}"
        definitions = {
            "row": (f"{base}.row", "", ""),
            "title": (f"{base}.title", "", ""),
            "address": (f"{base}.address", "", ""),
            "status": (f"{base}.status", "", ""),
            "metric": (f"{base}.metrics", "", ""),
            "alias": (f"{base}.alias_button", "target.alias.edit", "edit_target_alias"),
            "pause": (f"{base}.pause_button", "target.pause.toggle", "toggle_runtime_target_pause"),
            "remove": (f"{base}.remove_button", "target.remove", "remove_runtime_target"),
            "graph": (f"{base}.graph", "", ""),
        }
        for key, widget in widgets.items():
            definition = definitions.get(key)
            if definition is None:
                continue
            developer_id, feature_id, handler = definition
            self.registry.register_ui(
                widget,
                UiMetadata(
                    developer_id=developer_id,
                    source_file="app/ui/main_window.py",
                    component_class=type(widget).__name__,
                    creation_method="MainWindow._create_target_graph_row",
                    event_handler=handler,
                    feature_id=feature_id,
                ),
            )
            if feature_id and isinstance(widget, QAbstractButton):
                self._connect_widget_feature(widget, feature_id)

    def unregister_target_row(self, row: QWidget) -> None:
        if self.inspector is not None and self.inspector.selected_widget is not None:
            selected = self.inspector.selected_widget
            if selected is row or row.isAncestorOf(selected):
                self.inspector.clear_selection()
        for widget in [row, *row.findChildren(QWidget)]:
            self.registry.unregister_ui(widget)

    def runtime_info(self, *, selected_ui_id: str = "", selected_feature_id: str = "") -> RuntimeInfo:
        screen = self.window.screen() or QApplication.primaryScreen()
        if screen is None:
            resolution = UNKNOWN_VALUE
            scale = UNKNOWN_VALUE
        else:
            geometry = screen.geometry()
            resolution = f"{geometry.width()} x {geometry.height()}"
            scale = f"{round((screen.logicalDotsPerInch() / 96.0) * 100):d}%"
        operating_system = " ".join(
            value for value in (platform.system(), platform.release(), platform.version()) if value
        ) or UNKNOWN_VALUE
        environment_kind = (
            self.panel.environment_combo.currentText()
            if self.panel is not None
            else self.preferences_store.load_environment_kind()
        )
        return RuntimeInfo(
            operating_system=operating_system,
            screen_resolution=resolution,
            display_scale=scale,
            window_size=f"{self.window.width()} x {self.window.height()}",
            current_screen="main_dashboard",
            selected_ui_id=selected_ui_id or UNKNOWN_VALUE,
            selected_feature_id=selected_feature_id or UNKNOWN_VALUE,
            generated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            environment_kind=environment_kind,
        )

    def recent_log_excerpt(self) -> str:
        selected: list[str] = []
        total_characters = 0
        for line in reversed(self._recent_logs):
            bounded_line = line[:1000]
            if selected and total_characters + len(bounded_line) > 8000:
                break
            selected.append(bounded_line)
            total_characters += len(bounded_line)
            if len(selected) >= 20:
                break
        return "\n".join(reversed(selected))

    def set_panel_collapsed(self, collapsed: bool) -> None:
        if self.dock is None:
            return
        self.dock.setMinimumWidth(170 if collapsed else DEVELOPER_PANEL_MINIMUM_WIDTH)
        width = 190 if collapsed else DEVELOPER_PANEL_DEFAULT_WIDTH
        try:
            self.window.resizeDocks([self.dock], [width], Qt.Horizontal)
        except (AttributeError, RuntimeError):
            pass

    def _ensure_panel(self) -> None:
        if self.panel is not None:
            return
        self.panel = DeveloperPanel(self)
        self.inspector = UiInspector(self.window, self.registry, panel_provider=lambda: self.dock)
        self.inspector.selection_changed.connect(self.panel.on_ui_selection_changed)
        self.dock = QDockWidget("개발자 모드", self.window)
        self.dock.setObjectName(DEVELOPER_PANEL_OBJECT_NAME)
        self.dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.dock.setFeatures(
            QDockWidget.DockWidgetClosable
            | QDockWidget.DockWidgetMovable
            | QDockWidget.DockWidgetFloatable
        )
        self.dock.setMinimumWidth(DEVELOPER_PANEL_MINIMUM_WIDTH)
        self.dock.setWidget(self.panel)
        self.dock.visibilityChanged.connect(self._on_dock_visibility_changed)
        self.window.addDockWidget(Qt.RightDockWidgetArea, self.dock)
        self.dock.hide()

    def _on_dock_visibility_changed(self, visible: bool) -> None:
        if self.active and not visible:
            self.disable()

    def _register_features(self) -> None:
        definitions = (
            FeatureMetadata(
                "measurement.start",
                "Ping 측정 시작",
                "입력한 IPv4 대상의 실시간 측정을 시작합니다.",
                ui_id="main_dashboard.measurement.start_button",
                event_handler="MainWindow.start_measurement",
                service_function="MeasurementWorker.start",
                source_file="app/ui/main_window.py",
                log_category="measurement",
                may_contain_sensitive_data=True,
            ),
            FeatureMetadata(
                "measurement.stop",
                "Ping 측정 중지",
                "현재 측정 작업에 중지 요청을 전달합니다.",
                ui_id="main_dashboard.measurement.stop_button",
                event_handler="MainWindow.stop_measurement",
                service_function="MeasurementWorker.request_stop",
                source_file="app/ui/main_window.py",
                log_category="measurement",
            ),
            FeatureMetadata(
                "target.add",
                "측정 대상 추가",
                "측정 중 새 IPv4 대상을 추가합니다.",
                ui_id="main_dashboard.targets.runtime_add_button",
                event_handler="MainWindow.add_runtime_targets",
                service_function="MeasurementWorker.add_targets",
                source_file="app/ui/main_window.py",
                may_contain_sensitive_data=True,
            ),
            FeatureMetadata(
                "target.alias.edit",
                "대상 이름 변경",
                "그래프 행에 표시되는 대상 이름을 추가, 수정 또는 삭제합니다.",
                ui_id="main_dashboard.live_graph.target_*.alias_button",
                event_handler="MainWindow.edit_target_alias",
                source_file="app/ui/main_window.py",
                may_contain_sensitive_data=True,
            ),
            FeatureMetadata(
                "target.pause.toggle",
                "개별 대상 일시중지 또는 재개",
                "선택한 대상 하나의 측정을 일시중지하거나 재개합니다.",
                ui_id="main_dashboard.live_graph.target_*.pause_button",
                event_handler="MainWindow.toggle_runtime_target_pause",
                service_function="MeasurementWorker.pause_targets/resume_targets",
                source_file="app/ui/main_window.py",
                may_contain_sensitive_data=True,
            ),
            FeatureMetadata(
                "target.remove",
                "개별 대상 삭제",
                "선택한 대상의 측정을 중단하고 그래프 행에서 제거합니다.",
                ui_id="main_dashboard.live_graph.target_*.remove_button",
                event_handler="MainWindow.remove_runtime_target",
                service_function="MeasurementWorker.remove_targets",
                source_file="app/ui/main_window.py",
                may_contain_sensitive_data=True,
            ),
            FeatureMetadata(
                "graph.range.change",
                "그래프 시간 범위 변경",
                "모든 실시간 그래프의 표시 시간 범위를 함께 변경합니다.",
                ui_id="main_dashboard.live_graph.range_combo",
                event_handler="MainWindow.on_main_graph_range_changed",
                source_file="app/ui/main_window.py",
            ),
            FeatureMetadata(
                "result.export.csv",
                "CSV 내보내기",
                "현재 측정 결과를 CSV 파일로 저장합니다.",
                ui_id="main_dashboard.export.csv_button",
                event_handler="MainWindow.save_csv",
                service_function="ExportWorker",
                source_file="app/ui/main_window.py",
                may_contain_sensitive_data=True,
            ),
            FeatureMetadata(
                "result.export.xlsx",
                "XLSX 내보내기",
                "현재 측정 결과를 XLSX 파일로 저장합니다.",
                ui_id="main_dashboard.export.xlsx_button",
                event_handler="MainWindow.save_xlsx",
                service_function="ExportWorker",
                source_file="app/ui/main_window.py",
                may_contain_sensitive_data=True,
            ),
            FeatureMetadata(
                "result.export.report",
                "보고서 내보내기",
                "현재 측정 결과를 TXT 또는 HTML 보고서로 저장합니다.",
                ui_id="main_dashboard.export.report_button",
                event_handler="MainWindow.save_report",
                service_function="ExportWorker",
                source_file="app/ui/main_window.py",
                may_contain_sensitive_data=True,
            ),
            FeatureMetadata(
                "result.export.graph_png",
                "그래프 이미지 저장",
                "현재 그래프를 PNG 이미지로 저장합니다.",
                ui_id="main_dashboard.export.graph_png_button",
                event_handler="MainWindow.save_graph_png",
                source_file="app/ui/main_window.py",
                may_contain_sensitive_data=True,
            ),
            FeatureMetadata(
                "session.open",
                "저장 세션 열기",
                "로컬에 저장된 측정 세션을 엽니다.",
                ui_id="main_dashboard.sessions.open_button",
                event_handler="MainWindow.open_selected_session",
                service_function="SessionOpenWorker",
                source_file="app/ui/main_window.py",
            ),
            FeatureMetadata(
                "session.resume",
                "저장 세션 이어서 측정",
                "선택한 저장 세션의 대상을 다시 측정합니다.",
                ui_id="main_dashboard.sessions.resume_button",
                event_handler="MainWindow.resume_selected_session",
                source_file="app/ui/main_window.py",
                may_contain_sensitive_data=True,
            ),
            FeatureMetadata(
                "session.delete",
                "저장 세션 삭제",
                "선택한 로컬 세션 파일을 삭제 대기 상태로 전환합니다.",
                ui_id="main_dashboard.sessions.delete_button",
                event_handler="MainWindow.delete_selected_session",
                source_file="app/ui/main_window.py",
            ),
            FeatureMetadata(
                "settings.target_group.save",
                "대상 그룹 저장",
                "현재 대상과 측정 설정을 JSON 프리셋으로 저장합니다.",
                ui_id="main_dashboard.settings.target_group_save_button",
                event_handler="MainWindow.save_target_group_preset",
                source_file="app/ui/main_window.py",
                settings_key="target-group=2",
                may_contain_sensitive_data=True,
            ),
            FeatureMetadata(
                "settings.target_group.load",
                "대상 그룹 불러오기",
                "저장한 대상 그룹 JSON 프리셋을 불러옵니다.",
                ui_id="main_dashboard.settings.target_group_load_button",
                event_handler="MainWindow.load_target_group_preset",
                source_file="app/ui/main_window.py",
                settings_key="target-group=2",
                may_contain_sensitive_data=True,
            ),
            FeatureMetadata(
                "alert.evaluate",
                "네트워크 상태 알림 판정",
                "지연, 손실, 변동 및 품질 상태를 기존 알림 기준으로 판정합니다.",
                event_handler="MainWindow._record_metric_alerts",
                service_function="app.core.alerts.evaluate_target_alerts",
                source_file="app/ui/main_window.py",
                log_category="alert",
            ),
        )
        for definition in definitions:
            self.registry.register_feature(definition)

    def _register_static_ui(self) -> None:
        self.registry.register_ui(
            self.window,
            UiMetadata(
                developer_id="main_window",
                source_file="app/ui/main_window.py",
                component_class=type(self.window).__name__,
                creation_method="MainWindow.__init__",
            ),
        )
        central = self.window.centralWidget()
        if central is not None:
            self.registry.register_ui(
                central,
                UiMetadata(
                    developer_id="main_dashboard",
                    source_file="app/ui/main_window.py",
                    component_class=type(central).__name__,
                    creation_method="MainWindow._build_ui",
                ),
            )

        definitions = (
            ("controls_panel", "main_dashboard.controls", "", "app/ui/control_panel.py", "build_controls_panel"),
            ("target_input", "main_dashboard.targets.input", "", "app/ui/control_panel.py", "build_controls_panel"),
            ("start_button", "main_dashboard.measurement.start_button", "measurement.start", "app/ui/control_panel.py", "build_controls_panel"),
            ("stop_button", "main_dashboard.measurement.stop_button", "measurement.stop", "app/ui/control_panel.py", "build_controls_panel"),
            ("status_label", "main_dashboard.measurement.status", "", "app/ui/control_panel.py", "build_controls_panel"),
            ("session_state_label", "main_dashboard.measurement.state_badge", "", "app/ui/control_panel.py", "build_controls_panel"),
            ("running_target_summary_label", "main_dashboard.targets.running_summary", "", "app/ui/control_panel.py", "build_controls_panel"),
            ("runtime_target_input", "main_dashboard.targets.runtime_input", "", "app/ui/control_panel.py", "build_controls_panel"),
            ("add_runtime_target_button", "main_dashboard.targets.runtime_add_button", "target.add", "app/ui/control_panel.py", "build_controls_panel"),
            ("main_graph_range_combo", "main_dashboard.live_graph.range_combo", "graph.range.change", "app/ui/main_window.py", "MainWindow._build_graph_panel"),
            ("main_graph_time_status_label", "main_dashboard.live_graph.time_status", "", "app/ui/main_window.py", "MainWindow._build_graph_panel"),
            ("target_graph_legend_label", "main_dashboard.live_graph.legend", "", "app/ui/main_window.py", "MainWindow._build_graph_panel"),
            ("target_graph_container", "main_dashboard.live_graph.targets", "", "app/ui/main_window.py", "MainWindow._build_graph_panel"),
            ("target_graph_empty_label", "main_dashboard.live_graph.empty_state", "", "app/ui/main_window.py", "MainWindow._build_graph_panel"),
            ("graph_panel", "main_dashboard.live_graph", "", "app/ui/main_window.py", "MainWindow._build_graph_panel"),
            ("csv_button", "main_dashboard.export.csv_button", "result.export.csv", "app/ui/control_panel.py", "build_controls_panel"),
            ("xlsx_button", "main_dashboard.export.xlsx_button", "result.export.xlsx", "app/ui/control_panel.py", "build_controls_panel"),
            ("report_button", "main_dashboard.export.report_button", "result.export.report", "app/ui/control_panel.py", "build_controls_panel"),
            ("graph_png_button", "main_dashboard.export.graph_png_button", "result.export.graph_png", "app/ui/control_panel.py", "build_controls_panel"),
            ("open_session_button", "main_dashboard.sessions.open_button", "session.open", "app/ui/main_window.py", "MainWindow._build_sessions_panel"),
            ("resume_session_button", "main_dashboard.sessions.resume_button", "session.resume", "app/ui/main_window.py", "MainWindow._build_sessions_panel"),
            ("delete_session_button", "main_dashboard.sessions.delete_button", "session.delete", "app/ui/main_window.py", "MainWindow._build_sessions_panel"),
            ("save_target_group_button", "main_dashboard.settings.target_group_save_button", "settings.target_group.save", "app/ui/control_panel.py", "build_controls_panel"),
            ("load_target_group_button", "main_dashboard.settings.target_group_load_button", "settings.target_group.load", "app/ui/control_panel.py", "build_controls_panel"),
        )
        for attribute, developer_id, feature_id, source_file, creation_method in definitions:
            widget = getattr(self.window, attribute, None)
            if not isinstance(widget, QWidget):
                continue
            feature = self.registry.feature(feature_id) if feature_id else None
            self.registry.register_ui(
                widget,
                UiMetadata(
                    developer_id=developer_id,
                    source_file=source_file,
                    component_class=type(widget).__name__,
                    creation_method=creation_method,
                    event_handler=feature.event_handler if feature is not None else "",
                    service_function=feature.service_function if feature is not None else "",
                    settings_key=feature.settings_key if feature is not None else "",
                    feature_id=feature_id,
                ),
            )

    def _connect_feature_tracking(self) -> None:
        for widget in self.window.findChildren(QWidget):
            metadata = self.registry.metadata_for(widget)
            if metadata is not None and metadata.feature_id:
                self._connect_widget_feature(widget, metadata.feature_id)

    def _connect_widget_feature(self, widget: QWidget, feature_id: str) -> None:
        if bool(widget.property("developerFeatureTrackingConnected")):
            return
        signal = None
        if isinstance(widget, QAbstractButton):
            signal = widget.clicked
        elif isinstance(widget, QComboBox):
            signal = widget.activated
        if signal is None:
            return
        signal.connect(lambda *_args, registered_feature_id=feature_id: self.registry.record_feature(registered_feature_id, "성공"))
        widget.setProperty("developerFeatureTrackingConnected", True)

    def _show_toast(self, text: str) -> None:
        if self._toast is None:
            self._toast = QLabel(self.window)
            self._toast.setObjectName("developerModeToast")
            self._toast.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            self._toast.setStyleSheet(
                "background: #111827; color: white; border: 1px solid #374151; "
                "padding: 6px 10px; font-weight: 600;"
            )
        self._toast.setText(text)
        self._toast.adjustSize()
        x = 18
        y = max(12, self.window.height() - self._toast.height() - 18)
        self._toast.move(x, y)
        self._toast.show()
        self._toast.raise_()
        self._toast_timer.start(2200)

    def _hide_toast(self) -> None:
        if self._toast is not None:
            self._toast.hide()

    def _on_log_record(self, record: logging.LogRecord) -> None:
        if not self.active:
            return
        try:
            message = record.getMessage()
        except Exception:
            message = "로그 메시지를 읽을 수 없음"
        timestamp = datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="seconds")
        self._recent_logs.append(f"{timestamp} {record.levelname} {record.name} {message[:2000]}")
        if record.levelno >= logging.ERROR:
            error_type = record.exc_info[0].__name__ if record.exc_info and record.exc_info[0] else "LoggedError"
            self.record_error(message, error_type=error_type)


class DeveloperPanel(QWidget):
    def __init__(self, controller: DeveloperModeController) -> None:
        super().__init__()
        self.controller = controller
        self.registry = controller.registry
        self.id_factory = RequestIdFactory()
        self._selected_ui: UiSelection | None = None
        self._restored_ui_info: dict[str, str] | None = None
        self._history_records: list[RequestRecord] = []
        self._collapsed = False
        self.setObjectName("developerModePanel")
        self.setStyleSheet(DEVELOPER_PANEL_STYLE)
        self._build_ui()
        self.registry.on_recent_changed(self.refresh_recent_runs)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(7)

        title_row = QHBoxLayout()
        title = QLabel("개발자 모드")
        title.setObjectName("developerPanelTitle")
        title_row.addWidget(title, 1)
        self.collapse_button = QPushButton("접기")
        self.collapse_button.clicked.connect(self._toggle_collapsed)
        title_row.addWidget(self.collapse_button)
        root.addLayout(title_row)

        self.mode_hint = QLabel("F12로 종료 · 모든 요청 처리는 이 PC 안에서만 수행됩니다.")
        self.mode_hint.setWordWrap(True)
        self.mode_hint.setObjectName("developerHint")
        root.addWidget(self.mode_hint)

        security_row = QHBoxLayout()
        self.environment_combo = QComboBox()
        self.environment_combo.addItems(list(ENVIRONMENT_KINDS))
        saved_environment = self.controller.preferences_store.load_environment_kind()
        self.environment_combo.setCurrentText(saved_environment)
        self.environment_combo.currentTextChanged.connect(self._save_environment_kind)
        self.mask_company_check = QCheckBox("회사 정보 마스킹")
        self.mask_company_check.setChecked(True)
        security_row.addWidget(self.environment_combo, 1)
        security_row.addWidget(self.mask_company_check)
        root.addLayout(security_row)

        privacy_row = QHBoxLayout()
        self.include_logs_check = QCheckBox("로그 포함")
        self.include_logs_check.setChecked(False)
        self.include_values_check = QCheckBox("현재 입력값 포함")
        self.include_values_check.setChecked(False)
        self.include_values_check.toggled.connect(self._refresh_selected_ui_values)
        self.include_screenshot_check = QCheckBox("스크린샷 포함")
        self.include_screenshot_check.setChecked(False)
        self.include_screenshot_check.setEnabled(False)
        self.include_screenshot_check.setToolTip("민감정보 보호를 위해 현재 버전에서는 스크린샷 첨부를 사용하지 않습니다.")
        privacy_row.addWidget(self.include_logs_check)
        privacy_row.addWidget(self.include_values_check)
        privacy_row.addWidget(self.include_screenshot_check)
        root.addLayout(privacy_row)

        self.body = QWidget()
        body_layout = QVBoxLayout(self.body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(7)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_ui_inspection_tab(), "UI 검사")
        self.tabs.addTab(self._build_feature_request_tab(), "기능 요청")
        self.tabs.addTab(self._build_error_report_tab(), "오류 보고")
        self.tabs.addTab(self._build_history_tab(), "요청 기록")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        body_layout.addWidget(self.tabs, 1)

        preview_header = QHBoxLayout()
        preview_header.addWidget(QLabel("요청문 미리보기"), 1)
        self.mask_summary_label = QLabel("마스킹된 항목 없음")
        self.mask_summary_label.setObjectName("developerHint")
        preview_header.addWidget(self.mask_summary_label)
        body_layout.addLayout(preview_header)
        self.preview_edit = QPlainTextEdit()
        self.preview_edit.setReadOnly(True)
        self.preview_edit.setMinimumHeight(88)
        self.preview_edit.setMaximumHeight(110)
        self.preview_edit.setPlaceholderText("요청문을 생성하면 여기에 표시됩니다.")
        body_layout.addWidget(self.preview_edit)

        preview_actions = QHBoxLayout()
        self.preview_button = QPushButton("미리보기 생성")
        self.preview_button.clicked.connect(self.preview_current_request)
        self.current_copy_button = QPushButton("UI 수정 요청 복사")
        self.current_copy_button.clicked.connect(self._copy_current_request)
        preview_actions.addWidget(self.preview_button)
        preview_actions.addWidget(self.current_copy_button)
        body_layout.addLayout(preview_actions)

        secondary_preview_actions = QHBoxLayout()
        self.technical_copy_button = QPushButton("기술 정보만 복사")
        self.technical_copy_button.clicked.connect(self.copy_technical_information)
        self.save_preview_button = QPushButton("Markdown 저장")
        self.save_preview_button.clicked.connect(self.save_preview)
        secondary_preview_actions.addWidget(self.technical_copy_button)
        secondary_preview_actions.addWidget(self.save_preview_button)
        body_layout.addLayout(secondary_preview_actions)

        id_actions = QHBoxLayout()
        self.copy_ui_id_button = QPushButton("개발자 ID만 복사")
        self.copy_ui_id_button.clicked.connect(self.copy_selected_ui_id)
        self.copy_feature_id_button = QPushButton("기능 ID만 복사")
        self.copy_feature_id_button.clicked.connect(self.copy_selected_feature_id)
        self.reset_button = QPushButton("요청문 초기화")
        self.reset_button.clicked.connect(self.reset_request_fields)
        id_actions.addWidget(self.copy_ui_id_button)
        id_actions.addWidget(self.copy_feature_id_button)
        id_actions.addWidget(self.reset_button)
        body_layout.addLayout(id_actions)

        root.addWidget(self.body, 1)
        self.notification_label = QLabel("")
        self.notification_label.setObjectName("developerNotification")
        self.notification_label.setWordWrap(True)
        root.addWidget(self.notification_label)

    def _build_ui_inspection_tab(self) -> QWidget:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(7)
        self.inspect_mode_check = QCheckBox("화면 요소 선택")
        self.inspect_mode_check.setChecked(True)
        self.inspect_mode_check.toggled.connect(lambda _checked: self.set_inspection_for_current_tab())
        layout.addWidget(self.inspect_mode_check)
        self.selected_ui_label = QLabel("선택된 UI 없음")
        self.selected_ui_label.setWordWrap(True)
        layout.addWidget(self.selected_ui_label)
        self.ui_info_edit = QPlainTextEdit()
        self.ui_info_edit.setReadOnly(True)
        self.ui_info_edit.setMinimumHeight(180)
        layout.addWidget(self.ui_info_edit)

        navigation = QHBoxLayout()
        self.select_parent_button = QPushButton("상위 요소")
        self.select_parent_button.clicked.connect(self._select_parent)
        self.child_combo = QComboBox()
        self.select_child_button = QPushButton("하위 요소 선택")
        self.select_child_button.clicked.connect(self._select_child)
        navigation.addWidget(self.select_parent_button)
        navigation.addWidget(self.child_combo, 1)
        navigation.addWidget(self.select_child_button)
        layout.addLayout(navigation)

        form = QFormLayout()
        self.ui_desired_edit = _multi_line("어떻게 바꾸고 싶은지 입력")
        self.ui_keep_edit = _multi_line("그대로 유지할 부분")
        self.ui_acceptance_edit = _multi_line("완료됐다고 판단할 기준")
        form.addRow("원하는 변경 *", self.ui_desired_edit)
        form.addRow("그대로 유지", self.ui_keep_edit)
        form.addRow("완료 기준", self.ui_acceptance_edit)
        layout.addLayout(form)
        self.ui_copy_button = QPushButton("UI 수정 요청 복사")
        self.ui_copy_button.clicked.connect(lambda: self.generate_ui_request(copy_to_clipboard=True))
        layout.addWidget(self.ui_copy_button)
        layout.addStretch(1)
        return _scrollable(content)

    def _build_feature_request_tab(self) -> QWidget:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(7)
        form = QFormLayout()
        self.feature_request_type_combo = QComboBox()
        self.feature_request_type_combo.addItems(
            ["기존 기능 변경", "새 기능 추가", "동작 방식 개선", "사용성 개선", "성능 개선", "검증 요청"]
        )
        self.feature_search_edit = QLineEdit()
        self.feature_search_edit.setPlaceholderText("기능 이름 또는 ID 검색")
        self.feature_search_edit.textChanged.connect(self.refresh_functions)
        self.feature_combo = QComboBox()
        self.feature_combo.setEditable(True)
        self.feature_combo.currentIndexChanged.connect(self._sync_selected_feature_from_combo)
        self.manual_feature_edit = QLineEdit()
        self.manual_feature_edit.setPlaceholderText("목록에 없을 때 기능을 직접 설명")
        self.feature_desired_edit = _multi_line("원하는 변경만 입력해도 요청문을 만들 수 있습니다.")
        self.feature_keep_edit = _multi_line("그대로 유지할 부분")
        self.feature_acceptance_edit = _multi_line("완료 기준")
        form.addRow("요청 유형", self.feature_request_type_combo)
        form.addRow("기능 검색", self.feature_search_edit)
        form.addRow("대상 기능", self.feature_combo)
        form.addRow("수동 설명", self.manual_feature_edit)
        form.addRow("원하는 변경 *", self.feature_desired_edit)
        form.addRow("그대로 유지", self.feature_keep_edit)
        form.addRow("완료 기준", self.feature_acceptance_edit)
        layout.addLayout(form)

        self.feature_details_toggle = QToolButton()
        self.feature_details_toggle.setText("상세 입력 펼치기")
        self.feature_details_toggle.setCheckable(True)
        self.feature_details_toggle.toggled.connect(self._toggle_feature_details)
        layout.addWidget(self.feature_details_toggle)
        self.feature_details = QWidget()
        details_form = QFormLayout(self.feature_details)
        details_form.setContentsMargins(0, 0, 0, 0)
        self.feature_current_edit = _multi_line("현재 동작")
        self.feature_steps_edit = _multi_line("사용 또는 재현 순서")
        self.feature_reason_edit = _multi_line("변경 이유")
        self.feature_boundaries_edit = _multi_line("변경하면 안 되는 범위")
        self.feature_additional_edit = _multi_line("추가 설명 또는 관련 오류 메시지")
        details_form.addRow("현재 동작", self.feature_current_edit)
        details_form.addRow("사용·재현 순서", self.feature_steps_edit)
        details_form.addRow("변경 이유", self.feature_reason_edit)
        details_form.addRow("변경 금지 범위", self.feature_boundaries_edit)
        details_form.addRow("추가 설명", self.feature_additional_edit)
        self.feature_details.hide()
        layout.addWidget(self.feature_details)

        layout.addWidget(QLabel("최근 실행 기능"))
        self.recent_feature_list = QListWidget()
        self.recent_feature_list.setMaximumHeight(120)
        self.recent_feature_list.itemDoubleClicked.connect(self._use_recent_feature)
        layout.addWidget(self.recent_feature_list)
        recent_actions = QHBoxLayout()
        use_for_feature = QPushButton("기능 요청 대상으로")
        use_for_feature.clicked.connect(self._use_recent_feature)
        use_for_error = QPushButton("오류 대상으로")
        use_for_error.clicked.connect(lambda: self._use_recent_feature(for_error=True))
        recent_actions.addWidget(use_for_feature)
        recent_actions.addWidget(use_for_error)
        layout.addLayout(recent_actions)
        self.feature_copy_button = QPushButton("기능 변경 요청 복사")
        self.feature_copy_button.clicked.connect(lambda: self.generate_feature_request(copy_to_clipboard=True))
        layout.addWidget(self.feature_copy_button)
        layout.addStretch(1)
        return _scrollable(content)

    def _build_error_report_tab(self) -> QWidget:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(6, 6, 6, 6)
        form = QFormLayout()
        self.error_feature_combo = QComboBox()
        self.error_feature_combo.setEditable(True)
        self.error_screen_edit = QLineEdit("main_dashboard")
        self.error_before_edit = _multi_line("오류 직전에 수행한 작업")
        self.error_steps_edit = _multi_line("한 단계씩 줄을 바꿔 입력")
        self.error_actual_edit = _multi_line("실제로 발생한 결과")
        self.error_expected_edit = _multi_line("원래 기대한 결과")
        self.error_frequency_combo = QComboBox()
        self.error_frequency_combo.addItems(["항상 발생", "자주 발생", "가끔 발생", "한 번만 발생", "확인하지 못함"])
        self.error_reproducible_combo = QComboBox()
        self.error_reproducible_combo.addItems(["재현 가능", "재현 불가", "확인하지 못함"])
        self.error_additional_edit = _multi_line("추가 설명")
        self.error_keep_edit = _multi_line("그대로 유지해야 하는 기능")
        self.include_last_error_check = QCheckBox("마지막 오류 정보 포함")
        self.include_last_error_check.setChecked(True)
        self.last_error_label = QLabel("기록된 오류 없음")
        self.last_error_label.setWordWrap(True)
        form.addRow("대상 기능", self.error_feature_combo)
        form.addRow("오류 화면", self.error_screen_edit)
        form.addRow("오류 전 작업", self.error_before_edit)
        form.addRow("재현 순서", self.error_steps_edit)
        form.addRow("실제 결과 *", self.error_actual_edit)
        form.addRow("기대 결과", self.error_expected_edit)
        form.addRow("발생 빈도", self.error_frequency_combo)
        form.addRow("재현 가능", self.error_reproducible_combo)
        form.addRow("추가 설명", self.error_additional_edit)
        form.addRow("유지할 기능", self.error_keep_edit)
        form.addRow(self.include_last_error_check, self.last_error_label)
        layout.addLayout(form)
        self.error_copy_button = QPushButton("오류 수정 요청 복사")
        self.error_copy_button.clicked.connect(lambda: self.generate_error_request(copy_to_clipboard=True))
        layout.addWidget(self.error_copy_button)
        layout.addStretch(1)
        return _scrollable(content)

    def _build_history_tab(self) -> QWidget:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(6, 6, 6, 6)
        self.history_table = QTableWidget(0, 7)
        self.history_table.setHorizontalHeaderLabels(
            ["요청 ID", "유형", "생성 시각", "화면", "UI ID", "기능 ID", "요약"]
        )
        self.history_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.history_table.setSelectionMode(QTableWidget.SingleSelection)
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.history_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.history_table)
        actions = QHBoxLayout()
        copy_button = QPushButton("다시 복사")
        copy_button.clicked.connect(self.copy_history_record)
        edit_button = QPushButton("다시 편집")
        edit_button.clicked.connect(self.edit_history_record)
        delete_button = QPushButton("삭제")
        delete_button.clicked.connect(self.delete_history_record)
        clear_button = QPushButton("전체 기록 삭제")
        clear_button.clicked.connect(self.clear_history)
        actions.addWidget(copy_button)
        actions.addWidget(edit_button)
        actions.addWidget(delete_button)
        actions.addWidget(clear_button)
        layout.addLayout(actions)
        return content

    def set_inspection_for_current_tab(self) -> None:
        inspector = self.controller.inspector
        if inspector is None:
            return
        enabled = bool(self.controller.active and self.tabs.currentIndex() == 0 and self.inspect_mode_check.isChecked())
        inspector.set_inspection_enabled(enabled)

    def _on_tab_changed(self, index: int) -> None:
        self.set_inspection_for_current_tab()
        labels = {
            0: "UI 수정 요청 복사",
            1: "기능 변경 요청 복사",
            2: "오류 수정 요청 복사",
            3: "선택 기록 다시 복사",
        }
        self.current_copy_button.setText(labels.get(index, "현재 요청 복사"))

    def on_ui_selection_changed(self, selection: object) -> None:
        self._selected_ui = selection if isinstance(selection, UiSelection) else None
        self._restored_ui_info = None
        if self._selected_ui is not None and self.include_values_check.isChecked():
            self._selected_ui = UiSelection(
                widget=self._selected_ui.widget,
                values=inspect_widget(
                    self._selected_ui.widget,
                    self.controller.window,
                    self.registry,
                    include_current_value=True,
                ),
            )
        if self._selected_ui is None:
            self.selected_ui_label.setText("선택된 UI 없음")
            self.ui_info_edit.clear()
            self.child_combo.clear()
            return
        values = self._selected_ui.values
        developer_id = values.get("developer_id", "미등록")
        if developer_id == "미등록":
            developer_id = f"미등록 | {values.get('temporary_path', UNKNOWN_VALUE)}"
        self.selected_ui_label.setText(f"{developer_id}\n{values.get('ui_type', UNKNOWN_VALUE)} / {values.get('display_text', UNKNOWN_VALUE)}")
        self.ui_info_edit.setPlainText(format_inspection(values))
        self._refresh_children()
        feature_id = values.get("feature_id", "")
        if feature_id:
            self._select_feature(self.feature_combo, feature_id)
            self._select_feature(self.error_feature_combo, feature_id)

    def _refresh_selected_ui_values(self, _checked: bool) -> None:
        if self._selected_ui is None:
            return
        widget = self._selected_ui.widget
        self.on_ui_selection_changed(UiSelection(widget=widget, values=inspect_widget(widget, self.controller.window, self.registry)))

    def reset_transient_selection(self) -> None:
        self._selected_ui = None
        self._restored_ui_info = None
        if hasattr(self, "selected_ui_label"):
            self.selected_ui_label.setText("선택된 UI 없음")
            self.ui_info_edit.clear()
            self.child_combo.clear()
        if hasattr(self, "inspect_mode_check"):
            self.inspect_mode_check.setChecked(True)

    def selected_ui_id(self) -> str:
        values = self._current_ui_values()
        if not values:
            return ""
        developer_id = values.get("developer_id", "")
        if developer_id and developer_id != "미등록":
            return developer_id
        return values.get("temporary_path", "")

    def refresh_build_information(self) -> None:
        build = self.controller.build_info
        self.mode_hint.setToolTip(
            f"버전 {build.program_version} | 빌드 {build.build_id} | Git {build.git_commit} | {build.distribution}"
        )

    def refresh_functions(self, *_args) -> None:
        query = self.feature_search_edit.text().strip().casefold() if hasattr(self, "feature_search_edit") else ""
        selected_feature = self._combo_feature_id(self.feature_combo) if hasattr(self, "feature_combo") else ""
        error_feature = self._combo_feature_id(self.error_feature_combo) if hasattr(self, "error_feature_combo") else ""
        features = [
            feature
            for feature in self.registry.features()
            if not query
            or query in feature.feature_id.casefold()
            or query in feature.display_name.casefold()
            or query in feature.description.casefold()
        ]
        if hasattr(self, "feature_combo"):
            self._populate_feature_combo(self.feature_combo, features, selected_feature)
        if hasattr(self, "error_feature_combo"):
            self._populate_feature_combo(self.error_feature_combo, self.registry.features(), error_feature)

    def refresh_recent_runs(self) -> None:
        if not hasattr(self, "recent_feature_list"):
            return
        self.recent_feature_list.clear()
        for run in self.registry.recent_runs():
            repeat = f" x{run.repeat_count}" if run.repeat_count > 1 else ""
            item = QListWidgetItem(f"{run.executed_at[11:19]} | {run.status} | {run.feature_id}{repeat}")
            item.setData(Qt.UserRole, run.feature_id)
            self.recent_feature_list.addItem(item)

    def refresh_last_error(self) -> None:
        error = self.controller.last_error
        if error is None:
            self.last_error_label.setText("기록된 오류 없음")
            return
        result = SensitiveDataMasker(mask_company_information=self.mask_company_check.isChecked()).mask(
            f"{error.occurred_at} | {error.error_type} | {error.message}"
        )
        self.last_error_label.setText(result.text)

    def refresh_history(self) -> None:
        self._history_records = self.controller.history_store.load()
        self.history_table.setRowCount(len(self._history_records))
        for row, record in enumerate(self._history_records):
            values = (
                record.request_id,
                record.request_type,
                record.created_at,
                record.screen_name,
                record.ui_id,
                record.feature_id,
                record.summary,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.UserRole, record.request_id)
                self.history_table.setItem(row, column, item)

    def preview_current_request(self) -> None:
        if self.tabs.currentIndex() == 0:
            self.generate_ui_request(copy_to_clipboard=False)
        elif self.tabs.currentIndex() == 1:
            self.generate_feature_request(copy_to_clipboard=False)
        elif self.tabs.currentIndex() == 2:
            self.generate_error_request(copy_to_clipboard=False)
        else:
            self._notify("요청 기록을 선택해 다시 복사하거나 편집하세요.")

    def _copy_current_request(self) -> None:
        if self.tabs.currentIndex() == 0:
            self.generate_ui_request(copy_to_clipboard=True)
        elif self.tabs.currentIndex() == 1:
            self.generate_feature_request(copy_to_clipboard=True)
        elif self.tabs.currentIndex() == 2:
            self.generate_error_request(copy_to_clipboard=True)
        else:
            self.copy_history_record()

    def generate_ui_request(self, *, copy_to_clipboard: bool) -> str:
        ui_values = self._current_ui_values()
        if not ui_values:
            self._notify("먼저 UI 검사에서 화면 요소를 선택하세요.")
            return ""
        desired = self.ui_desired_edit.toPlainText().strip()
        if not desired:
            self._notify("원하는 변경을 입력하세요.")
            return ""
        request_id = self.id_factory.create("UI")
        fields = {
            "desired_change": desired,
            "keep": self.ui_keep_edit.toPlainText(),
            "acceptance": self.ui_acceptance_edit.toPlainText(),
        }
        context = self._request_context(
            request_id=request_id,
            fields=fields,
            feature_id=ui_values.get("feature_id", ""),
            ui_info=ui_values,
        )
        prompt = build_ui_request(context)
        draft = {"kind": "ui", **fields, **{f"ui_{key}": value for key, value in ui_values.items()}}
        return self._finish_prompt(prompt, request_id, "UI 수정", desired, draft, copy_to_clipboard)

    def generate_feature_request(self, *, copy_to_clipboard: bool) -> str:
        desired = self.feature_desired_edit.toPlainText().strip()
        if not desired:
            self._notify("원하는 변경을 입력하세요.")
            return ""
        feature_id = self._combo_feature_id(self.feature_combo)
        request_id = self.id_factory.create("REQ")
        fields = {
            "request_type": self.feature_request_type_combo.currentText(),
            "manual_feature": self.manual_feature_edit.text(),
            "desired_change": desired,
            "keep": self.feature_keep_edit.toPlainText(),
            "acceptance": self.feature_acceptance_edit.toPlainText(),
            "current_behavior": self.feature_current_edit.toPlainText(),
            "steps": self.feature_steps_edit.toPlainText(),
            "reason": self.feature_reason_edit.toPlainText(),
            "boundaries": self.feature_boundaries_edit.toPlainText(),
            "additional": self.feature_additional_edit.toPlainText(),
        }
        context = self._request_context(request_id=request_id, fields=fields, feature_id=feature_id)
        prompt = build_feature_request(context)
        draft = {"kind": "feature", "feature_id": feature_id, **fields}
        return self._finish_prompt(prompt, request_id, fields["request_type"], desired, draft, copy_to_clipboard)

    def generate_error_request(self, *, copy_to_clipboard: bool) -> str:
        actual = self.error_actual_edit.toPlainText().strip()
        if not actual:
            self._notify("실제로 발생한 결과를 입력하세요.")
            return ""
        feature_id = self._combo_feature_id(self.error_feature_combo)
        request_id = self.id_factory.create("BUG")
        fields = {
            "screen_name": self.error_screen_edit.text(),
            "before_error": self.error_before_edit.toPlainText(),
            "steps": self.error_steps_edit.toPlainText(),
            "actual_result": actual,
            "expected_result": self.error_expected_edit.toPlainText(),
            "frequency": self.error_frequency_combo.currentText(),
            "reproducible": self.error_reproducible_combo.currentText(),
            "additional": self.error_additional_edit.toPlainText(),
            "keep": self.error_keep_edit.toPlainText(),
        }
        context = self._request_context(
            request_id=request_id,
            fields=fields,
            feature_id=feature_id,
            include_error=self.include_last_error_check.isChecked(),
        )
        prompt = build_error_request(context)
        draft = {"kind": "error", "feature_id": feature_id, **fields}
        return self._finish_prompt(prompt, request_id, "오류 수정", actual, draft, copy_to_clipboard)

    def copy_technical_information(self) -> None:
        context = self._request_context(request_id="", fields={}, feature_id=self.current_feature_id())
        self._set_preview_and_mask(build_technical_information(context))
        self._copy_preview()

    def copy_selected_ui_id(self) -> None:
        value = self.selected_ui_id()
        if not value:
            self._notify("선택된 UI가 없습니다.")
            return
        self._copy_text(value)

    def copy_selected_feature_id(self) -> None:
        value = self.current_feature_id()
        if not value:
            self._notify("선택된 기능이 없습니다.")
            return
        self._copy_text(value)

    def save_preview(self) -> None:
        text = self.preview_edit.toPlainText()
        if not text:
            self._notify("저장할 요청문이 없습니다.")
            return
        selected, _filter = QFileDialog.getSaveFileName(
            self,
            "Codex 요청문 저장",
            str(Path.home() / "Documents" / "codex-request.md"),
            "Markdown (*.md);;텍스트 파일 (*.txt)",
        )
        if not selected:
            return
        try:
            Path(selected).write_text(text, encoding="utf-8")
        except OSError:
            self._notify("요청문 파일을 저장할 수 없습니다.")
            return
        self._notify(f"요청문 저장 완료: {Path(selected).name}")

    def reset_request_fields(self) -> None:
        edits = (
            self.ui_desired_edit,
            self.ui_keep_edit,
            self.ui_acceptance_edit,
            self.feature_desired_edit,
            self.feature_keep_edit,
            self.feature_acceptance_edit,
            self.feature_current_edit,
            self.feature_steps_edit,
            self.feature_reason_edit,
            self.feature_boundaries_edit,
            self.feature_additional_edit,
            self.error_before_edit,
            self.error_steps_edit,
            self.error_actual_edit,
            self.error_expected_edit,
            self.error_additional_edit,
            self.error_keep_edit,
        )
        for edit in edits:
            edit.clear()
        self.manual_feature_edit.clear()
        self.preview_edit.clear()
        self.mask_summary_label.setText("마스킹된 항목 없음")
        self._notify("요청 입력을 초기화했습니다.")

    def copy_history_record(self) -> None:
        record = self._selected_history_record()
        if record is None:
            self._notify("요청 기록을 선택하세요.")
            return
        self.preview_edit.setPlainText(record.prompt)
        self._copy_text(record.prompt)

    def edit_history_record(self) -> None:
        record = self._selected_history_record()
        if record is None:
            self._notify("요청 기록을 선택하세요.")
            return
        draft = record.draft
        kind = draft.get("kind", "")
        if kind == "feature":
            self.tabs.setCurrentIndex(1)
            self._select_feature(self.feature_combo, draft.get("feature_id", ""))
            self.feature_desired_edit.setPlainText(draft.get("desired_change", ""))
            self.feature_keep_edit.setPlainText(draft.get("keep", ""))
            self.feature_acceptance_edit.setPlainText(draft.get("acceptance", ""))
            self.feature_current_edit.setPlainText(draft.get("current_behavior", ""))
            self.feature_steps_edit.setPlainText(draft.get("steps", ""))
            self.feature_reason_edit.setPlainText(draft.get("reason", ""))
            self.feature_boundaries_edit.setPlainText(draft.get("boundaries", ""))
            self.feature_additional_edit.setPlainText(draft.get("additional", ""))
        elif kind == "error":
            self.tabs.setCurrentIndex(2)
            self._select_feature(self.error_feature_combo, draft.get("feature_id", ""))
            self.error_screen_edit.setText(draft.get("screen_name", "main_dashboard"))
            self.error_before_edit.setPlainText(draft.get("before_error", ""))
            self.error_steps_edit.setPlainText(draft.get("steps", ""))
            self.error_actual_edit.setPlainText(draft.get("actual_result", ""))
            self.error_expected_edit.setPlainText(draft.get("expected_result", ""))
            self.error_additional_edit.setPlainText(draft.get("additional", ""))
            self.error_keep_edit.setPlainText(draft.get("keep", ""))
        elif kind == "ui":
            self.tabs.setCurrentIndex(0)
            self.ui_desired_edit.setPlainText(draft.get("desired_change", ""))
            self.ui_keep_edit.setPlainText(draft.get("keep", ""))
            self.ui_acceptance_edit.setPlainText(draft.get("acceptance", ""))
            restored = {
                key[3:]: value
                for key, value in draft.items()
                if key.startswith("ui_") and len(key) > 3
            }
            self._selected_ui = None
            self._restored_ui_info = restored or None
            if restored:
                identifier = restored.get("developer_id", "미등록")
                self.selected_ui_label.setText(f"기록에서 복원: {identifier}")
                self.ui_info_edit.setPlainText(format_inspection(restored))
        else:
            self._notify("이 기록에는 다시 편집할 입력 정보가 없습니다.")
            return
        self.preview_edit.setPlainText(record.prompt)
        self._notify("요청 기록을 입력란에 불러왔습니다.")

    def delete_history_record(self) -> None:
        record = self._selected_history_record()
        if record is None:
            self._notify("요청 기록을 선택하세요.")
            return
        try:
            self.controller.history_store.delete(record.request_id)
        except DeveloperHistoryError as exc:
            self._notify(str(exc))
            return
        self.refresh_history()
        self._notify("요청 기록을 삭제했습니다.")

    def clear_history(self) -> None:
        answer = QMessageBox.question(
            self,
            "요청 기록 삭제",
            "저장된 개발자 모드 요청 기록을 모두 삭제할까요?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            self.controller.history_store.clear()
        except DeveloperHistoryError as exc:
            self._notify(str(exc))
            return
        self.refresh_history()
        self._notify("요청 기록을 모두 삭제했습니다.")

    def current_feature_id(self) -> str:
        if self.tabs.currentIndex() == 2:
            return self._combo_feature_id(self.error_feature_combo)
        if self.tabs.currentIndex() == 0:
            return self._current_ui_values().get("feature_id", "")
        feature_id = self._combo_feature_id(self.feature_combo)
        if feature_id:
            return feature_id
        if self._selected_ui is not None:
            return self._selected_ui.values.get("feature_id", "")
        return ""

    def _request_context(
        self,
        *,
        request_id: str,
        fields: dict[str, str],
        feature_id: str,
        include_error: bool = False,
        ui_info: dict[str, str] | None = None,
    ) -> RequestContext:
        selected_ui_info = ui_info if ui_info is not None else self._current_ui_values()
        runtime = self.controller.runtime_info(
            selected_ui_id=self.selected_ui_id(),
            selected_feature_id=feature_id,
        )
        feature = self.registry.feature(feature_id)
        log_excerpt = self.controller.recent_log_excerpt() if self.include_logs_check.isChecked() else ""
        return RequestContext(
            request_id=request_id,
            build_info=self.controller.build_info,
            runtime_info=runtime,
            fields=fields,
            ui_info=selected_ui_info,
            feature=feature,
            manual_feature_id=feature_id if feature is None else "",
            recent_runs=self.registry.recent_runs(),
            error=self.controller.last_error if include_error else None,
            log_excerpt=log_excerpt,
        )

    def _finish_prompt(
        self,
        raw_prompt: str,
        request_id: str,
        request_type: str,
        summary: str,
        draft: dict[str, str],
        copy_to_clipboard: bool,
    ) -> str:
        masked_prompt = self._set_preview_and_mask(raw_prompt)
        if not copy_to_clipboard:
            self._notify("요청문 미리보기를 생성했습니다.")
            return masked_prompt

        copied = self._copy_text(masked_prompt, success_message=False)
        ui_id = self.selected_ui_id() or UNKNOWN_VALUE
        feature_id = self.current_feature_id() or draft.get("feature_id", "") or UNKNOWN_VALUE
        masked_draft = self._mask_draft(draft)
        record = RequestRecord(
            request_id=request_id,
            request_type=request_type,
            created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            screen_name=self._current_ui_values().get("screen_name", "main_dashboard"),
            ui_id=self._mask_text(ui_id),
            feature_id=self._mask_text(feature_id),
            summary=self._mask_text(summary)[:180],
            prompt=masked_prompt,
            draft=masked_draft,
        )
        history_error = ""
        try:
            self.controller.history_store.add(record)
            self.refresh_history()
        except DeveloperHistoryError as exc:
            history_error = str(exc)
        if copied and not history_error:
            self._notify("Codex 요청문이 클립보드에 복사되었습니다.")
        elif copied:
            self._notify(f"요청문은 복사했지만 기록하지 못했습니다. {history_error}")
        return masked_prompt

    def _set_preview_and_mask(self, raw_text: str) -> str:
        result = SensitiveDataMasker(mask_company_information=self.mask_company_check.isChecked()).mask(raw_text)
        self.preview_edit.setPlainText(result.text)
        self.mask_summary_label.setText(result.summary)
        return result.text

    def _mask_text(self, value: str) -> str:
        return SensitiveDataMasker(mask_company_information=self.mask_company_check.isChecked()).mask(value).text

    def _mask_draft(self, draft: dict[str, str]) -> dict[str, str]:
        return {key: self._mask_text(str(value)) for key, value in draft.items()}

    def _copy_preview(self) -> bool:
        text = self.preview_edit.toPlainText()
        if not text:
            self._notify("복사할 요청문이 없습니다.")
            return False
        return self._copy_text(text)

    def _copy_text(self, text: str, *, success_message: bool = True) -> bool:
        try:
            clipboard = QApplication.clipboard()
            if clipboard is None:
                raise RuntimeError("clipboard unavailable")
            clipboard.setText(text)
        except Exception:
            self._notify("클립보드 복사에 실패했습니다. 미리보기에서 전체 선택 후 직접 복사하거나 파일로 저장하세요.")
            return False
        if success_message:
            self._notify("Codex 요청문이 클립보드에 복사되었습니다.")
        return True

    def _selected_history_record(self) -> RequestRecord | None:
        row = self.history_table.currentRow()
        if row < 0 or row >= len(self._history_records):
            return None
        return self._history_records[row]

    def _current_ui_values(self) -> dict[str, str]:
        if self._selected_ui is not None:
            return self._selected_ui.values
        return self._restored_ui_info or {}

    def _select_parent(self) -> None:
        if self.controller.inspector is not None:
            self.controller.inspector.select_parent()

    def _select_child(self) -> None:
        if self.controller.inspector is None:
            return
        child = self.child_combo.currentData()
        if isinstance(child, QWidget):
            self.controller.inspector.select_widget(child)

    def _refresh_children(self) -> None:
        self.child_combo.clear()
        if self.controller.inspector is None:
            return
        for child in self.controller.inspector.selectable_children():
            values = inspect_widget(child, self.controller.window, self.registry)
            identifier = values.get("developer_id", "")
            if not identifier or identifier == "미등록":
                identifier = values.get("temporary_path", type(child).__name__).split(" > ")[-1]
            self.child_combo.addItem(f"{identifier} ({type(child).__name__})", child)

    def _toggle_feature_details(self, checked: bool) -> None:
        self.feature_details.setVisible(checked)
        self.feature_details_toggle.setText("상세 입력 접기" if checked else "상세 입력 펼치기")

    def _toggle_collapsed(self) -> None:
        self._collapsed = not self._collapsed
        self.body.setVisible(not self._collapsed)
        self.mode_hint.setVisible(not self._collapsed)
        self.collapse_button.setText("펼치기" if self._collapsed else "접기")
        self.controller.set_panel_collapsed(self._collapsed)

    def _sync_selected_feature_from_combo(self, _index: int) -> None:
        feature_id = self._combo_feature_id(self.feature_combo)
        if feature_id:
            self._select_feature(self.error_feature_combo, feature_id)

    def _use_recent_feature(self, _item=None, *, for_error: bool = False) -> None:
        item = self.recent_feature_list.currentItem()
        if item is None:
            self._notify("최근 실행 기능을 선택하세요.")
            return
        feature_id = str(item.data(Qt.UserRole) or "")
        if for_error:
            self.tabs.setCurrentIndex(2)
            self._select_feature(self.error_feature_combo, feature_id)
        else:
            self.tabs.setCurrentIndex(1)
            self._select_feature(self.feature_combo, feature_id)

    @staticmethod
    def _combo_feature_id(combo: QComboBox) -> str:
        data = str(combo.currentData() or "").strip()
        text = combo.currentText().strip()
        if data and (text == data or text.endswith(f"| {data}")):
            return data
        if text == "기능 선택 또는 ID 직접 입력":
            return ""
        return text

    @staticmethod
    def _populate_feature_combo(combo: QComboBox, features, selected: str) -> None:
        current_text = combo.currentText()
        blocked = combo.blockSignals(True)
        combo.clear()
        combo.addItem("기능 선택 또는 ID 직접 입력", "")
        for feature in features:
            combo.addItem(f"{feature.display_name} | {feature.feature_id}", feature.feature_id)
        combo.blockSignals(blocked)
        target = selected or current_text
        if target:
            DeveloperPanel._select_feature(combo, target)

    @staticmethod
    def _select_feature(combo: QComboBox, feature_id: str) -> None:
        if not feature_id:
            return
        index = combo.findData(feature_id)
        if index >= 0:
            combo.setCurrentIndex(index)
        else:
            combo.setEditText(feature_id)

    def _save_environment_kind(self, value: str) -> None:
        try:
            self.controller.preferences_store.save_environment_kind(value)
        except OSError:
            self._notify("실행 환경 선택을 저장하지 못했습니다.")

    def _notify(self, message: str) -> None:
        self.notification_label.setText(message)


class _DeveloperLogHandler(logging.Handler):
    def __init__(self, relay: _DeveloperLogRelay) -> None:
        super().__init__(level=logging.INFO)
        self.relay = relay

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.relay.record_received.emit(record)
        except Exception:
            self.handleError(record)


def _scrollable(content: QWidget) -> QScrollArea:
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.NoFrame)
    scroll.setWidget(content)
    return scroll


def _multi_line(placeholder: str) -> QPlainTextEdit:
    edit = QPlainTextEdit()
    edit.setPlaceholderText(placeholder)
    edit.setMinimumHeight(62)
    return edit


DEVELOPER_PANEL_STYLE = """
#developerModePanel {
    background: #f8fafc;
    color: #111827;
}
#developerPanelTitle {
    font-size: 16px;
    font-weight: 700;
}
#developerHint {
    color: #4b5563;
    font-size: 11px;
}
#developerNotification {
    color: #0f766e;
    font-weight: 600;
}
QLineEdit, QPlainTextEdit, QComboBox, QListWidget, QTableWidget {
    background: white;
    border: 1px solid #cbd5e1;
    padding: 4px;
}
QPushButton, QToolButton {
    min-height: 26px;
    padding: 3px 7px;
}
QTabWidget::pane {
    border: 1px solid #cbd5e1;
}
"""
