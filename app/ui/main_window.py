from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QDate, QDateTime, Qt, QTime
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.core.alerts import AlertEvent, AlertRuleConfig, alert_recovery_event, evaluate_target_alerts, route_change_alert
from app.core.analyzer import analyze_path
from app.core.models import HopObservation, MetricSnapshot
from app.core.observation_stats import build_focus_snapshots, observations_in_range
from app.core.route_history import RouteChange, route_path
from app.ui.control_panel import build_controls_panel
from app.ui.export_worker import ExportWorker
from app.ui.graph_detail_window import GraphDetailWindow
from app.ui.latency_graph import LatencyGraphWidget, TimelineAnnotation
from app.ui.table_panels import (
    TABLE_HEADERS,
    TARGET_HEADERS,
    TARGET_SCORE_COLUMN,
    create_hop_table,
    create_target_table,
    display_status,
    fmt_ms,
    populate_trace_table,
    target_problem_score,
    update_hop_table,
    update_target_table,
)
from app.ui.worker import (
    MAX_IPV4_TARGETS,
    MEASUREMENT_MODE_FINAL_HOP_ONLY,
    MEASUREMENT_MODE_FULL_ROUTE,
    PROBE_ENGINE_ICMP,
    PROBE_ENGINE_TCP_CONNECT,
    MeasurementWorker,
)
from app.storage.alert_action_log import append_alert_action, alert_action_log_path_for_session, read_alert_actions
from app.storage.export_annotations import ExportAnnotation, annotations_in_range
from app.storage.route_log import route_changes_in_range, route_log_path_for_session
from app.storage.session_index import SessionIndexStore, TraceSessionRecord, session_index_root_for_sample_path
from app.storage.session_log import iter_observations, iter_observations_in_range, session_log_bounds
from app.storage.statistics_exporter import TIMEZONE_LOCAL, TIMEZONE_UTC, StatisticsExportOptions
from app.storage.target_summary_exporter import TargetSummaryExportRow, export_target_summary_csv
from app.utils.filename import default_export_path, safe_target_name
from app.utils.validators import IPV4_ONLY_MESSAGE, parse_ipv4_targets, validate_target

STATISTICS_SCOPE_ALL = "all"
STATISTICS_SCOPE_VISIBLE = "visible"
STATISTICS_SCOPE_FOCUS = "focus"
STATISTICS_SCOPE_CUSTOM = "custom"
STALE_ACTIVE_SESSION_RECOVERY_SECONDS = 3600


class MainWindow(QMainWindow):
    def __init__(self, worker_factory=None) -> None:
        super().__init__()
        _apply_default_font()
        self.setWindowTitle("Network Path Diagnostics")
        self.resize(1440, 900)

        self.worker: MeasurementWorker | None = None
        self.export_worker: ExportWorker | None = None
        self.graph_detail_window: GraphDetailWindow | None = None
        self.worker_factory = worker_factory or MeasurementWorker
        self.session_index_store = SessionIndexStore.create()
        self.current_target = ""
        self.current_targets: list[str] = []
        self.session_log_path: Path | None = None
        self.route_log_path: Path | None = None
        self.alert_action_log_path: Path | None = None
        self.snapshots: list[MetricSnapshot] = []
        self.target_snapshot: MetricSnapshot | None = None
        self.target_snapshots: list[MetricSnapshot] = []
        self.observations: list[HopObservation] = []
        self.target_history: list[HopObservation] = []
        self.selected_hop_index: int | None = None
        self.analysis: list[str] = []
        self.focus_range: tuple[datetime, datetime] | None = None
        self.focus_observations: list[HopObservation] = []
        self.focus_snapshots: list[MetricSnapshot] = []
        self.focus_target_snapshot: MetricSnapshot | None = None
        self.focus_target_snapshots: list[MetricSnapshot] = []
        self.focus_analysis: list[str] = []
        self.timeline_range: tuple[datetime, datetime] | None = None
        self.timeline_observations: list[HopObservation] = []
        self.timeline_target_history: list[HopObservation] = []
        self.timeline_snapshots: list[MetricSnapshot] = []
        self.timeline_target_snapshot: MetricSnapshot | None = None
        self.timeline_status = "Timeline source: live buffer"
        self.route_changes: list[RouteChange] = []
        self.alert_events: list[AlertEvent] = []
        self.alert_event_actions: dict[str, list[str]] = {}
        self.pending_alert_image_keys: set[str] = set()
        self.active_alert_keys: set[str] = set()
        self.metric_value_labels: dict[str, QLabel] = {}

        self._build_ui()
        self._set_running(False)
        self._set_state_chip("대기", "neutral")
        self._update_target_summary(None)
        self._sync_sessions_box()

    def _build_ui(self) -> None:
        central = QWidget(self)
        central.setStyleSheet(APP_STYLE)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        root.addWidget(self._build_header())
        root.addWidget(self._build_controls())
        root.addWidget(self._build_main_area(), 1)
        root.addWidget(self._build_footer())

        self.setCentralWidget(central)

    def _build_header(self) -> QFrame:
        header = _panel("header")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(12)

        title_group = QVBoxLayout()
        title_group.setSpacing(2)
        title = QLabel("Network Path Diagnostics")
        title.setObjectName("title")
        subtitle = QLabel("Windows 일반 권한 기반 IPv4 경로 품질 측정 - tracert + ping fallback")
        subtitle.setObjectName("muted")
        title_group.addWidget(title)
        title_group.addWidget(subtitle)

        layout.addLayout(title_group, 1)

        self.session_state_label = _chip("대기", "neutral")
        self.permission_label = _chip("일반 권한", "success")
        self.icmp_notice_label = _chip("ICMP 주의", "warning")
        layout.addWidget(self.session_state_label)
        layout.addWidget(self.permission_label)
        layout.addWidget(self.icmp_notice_label)
        return header

    def _build_controls(self) -> QFrame:
        return build_controls_panel(self, _panel, _field_label)

    def _build_main_area(self) -> QSplitter:
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_left_work_area())
        splitter.addWidget(self._build_right_panel())
        splitter.setSizes([960, 420])
        return splitter

    def _build_left_work_area(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        layout.addWidget(self._build_metrics_strip())
        layout.addWidget(self._build_target_table_panel())

        self.table = create_hop_table()
        self.table.itemSelectionChanged.connect(self.on_hop_selection_changed)
        self.table.cellDoubleClicked.connect(self.on_hop_double_clicked)

        table_panel = _panel("tablePanel")
        table_layout = QVBoxLayout(table_panel)
        table_layout.setContentsMargins(12, 10, 12, 12)
        table_layout.setSpacing(8)
        table_header = QHBoxLayout()
        heading = QLabel("Hop Quality Table")
        heading.setObjectName("panelTitle")
        hint = QLabel("손실률과 평균 지연이 높은 Hop은 자동 강조")
        hint.setObjectName("muted")
        table_header.addWidget(heading)
        table_header.addStretch(1)
        table_header.addWidget(hint)
        table_layout.addLayout(table_header)
        table_layout.addWidget(self.table, 1)
        layout.addWidget(table_panel, 2)

        graph_panel = _panel("graphPanel")
        graph_layout = QVBoxLayout(graph_panel)
        graph_layout.setContentsMargins(12, 10, 12, 12)
        graph_layout.setSpacing(8)
        graph_header = QHBoxLayout()
        graph_title = QLabel("Target Latency Timeline")
        graph_title.setObjectName("panelTitle")
        graph_hint = QLabel("최종 대상 응답 기준 - timeout은 붉은 막대로 표시")
        graph_hint.setObjectName("muted")
        self.focus_label = _chip("Live", "neutral")
        self.clear_focus_button = QPushButton("Clear focus")
        self.clear_focus_button.setEnabled(False)
        self.clear_focus_button.clicked.connect(self.clear_focus_range)
        self.graph_detail_button = QPushButton("그래프 확대")
        self.graph_detail_button.clicked.connect(self.open_graph_detail)
        graph_header.addWidget(graph_title)
        graph_header.addStretch(1)
        graph_header.addWidget(graph_hint)
        graph_header.addWidget(self.focus_label)
        graph_header.addWidget(self.clear_focus_button)
        graph_header.addWidget(self.graph_detail_button)
        self.graph = LatencyGraphWidget()
        graph_layout.addLayout(graph_header)
        graph_layout.addWidget(self.graph, 1)
        layout.addWidget(graph_panel, 1)

        return container

    def _build_target_table_panel(self) -> QFrame:
        self.target_table = create_target_table()
        self.target_table.cellDoubleClicked.connect(self.on_target_double_clicked)

        panel = _panel("targetPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(8)
        header = QHBoxLayout()
        heading = QLabel("IPv4 Target Monitor")
        heading.setObjectName("panelTitle")
        hint = QLabel("중복 IPv4는 자동 제외, Tracert는 선택된 IPv4 1개만 수행")
        hint.setObjectName("muted")
        self.target_summary_status_label = QLabel("Targets: 0")
        self.target_summary_status_label.setObjectName("muted")
        self.pause_selected_targets_button = QPushButton("Pause selected")
        self.pause_selected_targets_button.clicked.connect(self.pause_selected_targets)
        self.resume_selected_targets_button = QPushButton("Resume selected")
        self.resume_selected_targets_button.clicked.connect(self.resume_selected_targets)
        self.pause_all_targets_button = QPushButton("Pause all")
        self.pause_all_targets_button.clicked.connect(self.pause_all_targets)
        self.resume_all_targets_button = QPushButton("Resume all")
        self.resume_all_targets_button.clicked.connect(self.resume_all_targets)
        self.apply_interval_button = QPushButton("Apply interval")
        self.apply_interval_button.clicked.connect(self.apply_runtime_interval)
        self.export_target_summary_button = QPushButton("Export summary")
        self.export_target_summary_button.clicked.connect(self.save_target_summary_csv)
        self.problem_sort_check = QCheckBox("Problem first")
        self.problem_sort_check.toggled.connect(self._on_problem_sort_toggled)
        header.addWidget(heading)
        header.addStretch(1)
        header.addWidget(self.target_summary_status_label)
        header.addWidget(hint)
        header.addWidget(self.problem_sort_check)
        header.addWidget(self.export_target_summary_button)
        header.addWidget(self.pause_selected_targets_button)
        header.addWidget(self.resume_selected_targets_button)
        header.addWidget(self.pause_all_targets_button)
        header.addWidget(self.resume_all_targets_button)
        header.addWidget(self.apply_interval_button)
        layout.addLayout(header)
        layout.addWidget(self.target_table)
        return panel

    def _build_metrics_strip(self) -> QFrame:
        strip = _panel("metrics")
        layout = QHBoxLayout(strip)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)

        for key, label in [
            ("current", "Target Current"),
            ("avg", "Avg Latency"),
            ("loss", "Packet Loss"),
            ("jitter", "Jitter"),
            ("samples", "Samples"),
        ]:
            box = _panel("metricBox")
            box_layout = QVBoxLayout(box)
            box_layout.setContentsMargins(10, 8, 10, 8)
            box_layout.setSpacing(2)
            title = QLabel(label)
            title.setObjectName("metricLabel")
            value = QLabel("-")
            value.setObjectName("metricValue")
            box_layout.addWidget(title)
            box_layout.addWidget(value)
            self.metric_value_labels[key] = value
            layout.addWidget(box, 1)
        return strip

    def _build_right_panel(self) -> QFrame:
        right = _panel("rightPanel")
        right.setMinimumWidth(390)
        layout = QVBoxLayout(right)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(12)

        analysis_title = QLabel("Analysis Summary")
        analysis_title.setObjectName("panelTitle")
        self.analysis_box = QTextEdit()
        self.analysis_box.setReadOnly(True)
        self.analysis_box.setMinimumHeight(180)
        self.analysis_box.setObjectName("analysisBox")

        states_title = QLabel("Operational States")
        states_title.setObjectName("panelTitle")
        self.states_box = QTextEdit()
        self.states_box.setReadOnly(True)
        self.states_box.setMaximumHeight(126)
        self.states_box.setObjectName("statesBox")
        self.states_box.setPlainText(
            "OK: 최종 대상 응답 정상, 손실률 0-5%\n"
            "Warning: 손실률 5-20% 또는 jitter 30ms 이상\n"
            "Critical: 손실률 20% 이상 또는 연속 timeout"
        )

        diagnostics_title = QLabel("Engine Diagnostics")
        diagnostics_title.setObjectName("panelTitle")
        self.diagnostics_box = QTextEdit()
        self.diagnostics_box.setReadOnly(True)
        self.diagnostics_box.setMaximumHeight(150)
        self.diagnostics_box.setObjectName("statesBox")
        self.diagnostics_box.setPlainText("측정 시작 후 엔진 상태가 표시됩니다.")

        alerts_title = QLabel("Alert Events")
        alerts_title.setObjectName("panelTitle")
        alert_rule_row = QHBoxLayout()
        alert_rule_row.setSpacing(6)
        self.loss_threshold_spin = QSpinBox()
        self.loss_threshold_spin.setRange(1, 100)
        self.loss_threshold_spin.setValue(20)
        self.loss_threshold_spin.setSuffix("%")
        self.loss_window_spin = QSpinBox()
        self.loss_window_spin.setRange(1, 60)
        self.loss_window_spin.setValue(3)
        self.loss_window_spin.setSuffix("m")
        self.latency_threshold_spin = QSpinBox()
        self.latency_threshold_spin.setRange(1, 5000)
        self.latency_threshold_spin.setValue(100)
        self.latency_threshold_spin.setSuffix("ms")
        self.jitter_threshold_spin = QSpinBox()
        self.jitter_threshold_spin.setRange(1, 1000)
        self.jitter_threshold_spin.setValue(30)
        self.jitter_threshold_spin.setSuffix("ms")
        self.sample_window_spin = QSpinBox()
        self.sample_window_spin.setRange(1, 100)
        self.sample_window_spin.setValue(10)
        self.sample_window_spin.setSuffix(" samples")
        self.sample_bad_spin = QSpinBox()
        self.sample_bad_spin.setRange(1, 100)
        self.sample_bad_spin.setValue(10)
        self.sample_bad_spin.setSuffix(" bad")
        self.alert_timeline_action_check = QCheckBox("Timeline")
        self.alert_timeline_action_check.setChecked(True)
        self.alert_comment_action_check = QCheckBox("Comment")
        self.alert_comment_action_check.setChecked(True)
        self.alert_beep_action_check = QCheckBox("Beep")
        self.alert_image_action_check = QCheckBox("Image")
        for label, spin in [
            ("Loss", self.loss_threshold_spin),
            ("Window", self.loss_window_spin),
            ("Latency", self.latency_threshold_spin),
            ("Jitter", self.jitter_threshold_spin),
            ("Samples", self.sample_window_spin),
            ("Bad", self.sample_bad_spin),
        ]:
            alert_rule_row.addWidget(QLabel(label))
            alert_rule_row.addWidget(spin)
        alert_rule_row.addWidget(QLabel("Actions"))
        alert_rule_row.addWidget(self.alert_timeline_action_check)
        alert_rule_row.addWidget(self.alert_comment_action_check)
        alert_rule_row.addWidget(self.alert_beep_action_check)
        alert_rule_row.addWidget(self.alert_image_action_check)
        alert_rule_row.addStretch(1)
        self.alerts_box = QTextEdit()
        self.alerts_box.setReadOnly(True)
        self.alerts_box.setMaximumHeight(120)
        self.alerts_box.setObjectName("statesBox")
        self.alerts_box.setPlainText("No alert events.")

        route_title = QLabel("Route Changes")
        route_title.setObjectName("panelTitle")
        self.route_changes_box = QTextEdit()
        self.route_changes_box.setReadOnly(True)
        self.route_changes_box.setMaximumHeight(130)
        self.route_changes_box.setObjectName("statesBox")
        self.route_changes_box.setPlainText("No route changes detected.")

        sessions_title = QLabel("Sessions")
        sessions_title.setObjectName("panelTitle")
        sessions_header = QHBoxLayout()
        self.session_combo = QComboBox()
        self.session_combo.setMinimumWidth(170)
        self.open_session_button = QPushButton("Open")
        self.open_session_button.clicked.connect(self.open_selected_session)
        self.resume_session_button = QPushButton("Resume")
        self.resume_session_button.clicked.connect(self.resume_selected_session)
        self.export_session_button = QPushButton("Export")
        self.export_session_button.clicked.connect(self.export_selected_session)
        self.delete_session_button = QPushButton("Delete")
        self.delete_session_button.clicked.connect(self.delete_selected_session)
        self.refresh_sessions_button = QPushButton("Refresh")
        self.refresh_sessions_button.clicked.connect(self.refresh_saved_sessions)
        sessions_header.addWidget(sessions_title)
        sessions_header.addStretch(1)
        sessions_header.addWidget(self.session_combo)
        sessions_header.addWidget(self.open_session_button)
        sessions_header.addWidget(self.resume_session_button)
        sessions_header.addWidget(self.export_session_button)
        sessions_header.addWidget(self.delete_session_button)
        sessions_header.addWidget(self.refresh_sessions_button)
        self.sessions_box = QTextEdit()
        self.sessions_box.setReadOnly(True)
        self.sessions_box.setMaximumHeight(118)
        self.sessions_box.setObjectName("statesBox")
        self.sessions_box.setPlainText("No saved sessions.")

        export_title = QLabel("Export")
        export_title.setObjectName("panelTitle")
        export_row = QHBoxLayout()
        export_row.setSpacing(8)
        for button in (self.csv_button, self.xlsx_button, self.report_button, self.graph_png_button):
            export_row.addWidget(button)
        export_row.addStretch(1)

        self.statistics_group_combo = QComboBox()
        self.statistics_group_combo.addItem("5m", 300)
        self.statistics_group_combo.addItem("1h", 3600)
        self.statistics_group_combo.addItem("1d", 86400)
        self.statistics_group_combo.addItem("1w", 604800)
        self.statistics_timezone_combo = QComboBox()
        self.statistics_timezone_combo.addItem("Local", TIMEZONE_LOCAL)
        self.statistics_timezone_combo.addItem("UTC", TIMEZONE_UTC)
        self.statistics_scope_combo = QComboBox()
        self.statistics_scope_combo.addItem("All time", STATISTICS_SCOPE_ALL)
        self.statistics_scope_combo.addItem("Visible timeline", STATISTICS_SCOPE_VISIBLE)
        self.statistics_scope_combo.addItem("Focus period", STATISTICS_SCOPE_FOCUS)
        self.statistics_scope_combo.addItem("Custom range", STATISTICS_SCOPE_CUSTOM)
        self.statistics_scope_combo.currentIndexChanged.connect(self._sync_statistics_range_controls)
        self.statistics_start_edit = QDateTimeEdit()
        self.statistics_start_edit.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self.statistics_start_edit.setCalendarPopup(True)
        self.statistics_end_edit = QDateTimeEdit()
        self.statistics_end_edit.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self.statistics_end_edit.setCalendarPopup(True)
        self._set_statistics_custom_range(datetime.now() - timedelta(hours=1), datetime.now())
        statistics_export_row = QHBoxLayout()
        statistics_export_row.setSpacing(8)
        statistics_export_row.addWidget(QLabel("Stats scope"))
        statistics_export_row.addWidget(self.statistics_scope_combo)
        statistics_export_row.addWidget(QLabel("Start"))
        statistics_export_row.addWidget(self.statistics_start_edit)
        statistics_export_row.addWidget(QLabel("End"))
        statistics_export_row.addWidget(self.statistics_end_edit)
        statistics_export_row.addWidget(QLabel("Stats group"))
        statistics_export_row.addWidget(self.statistics_group_combo)
        statistics_export_row.addWidget(QLabel("Timezone"))
        statistics_export_row.addWidget(self.statistics_timezone_combo)
        statistics_export_row.addWidget(self.stats_csv_button)
        statistics_export_row.addWidget(self.stats_xlsx_button)
        statistics_export_row.addStretch(1)
        self._sync_statistics_range_controls()

        export_note = QLabel(
            "파일명은 network_trace_target_YYYYMMDD_HHMMSS 형식을 사용합니다. "
            "기본 리포트에는 원시 민감 출력 대신 Hop 요약과 상태 코드를 저장합니다."
        )
        export_note.setObjectName("warningText")
        export_note.setWordWrap(True)

        layout.addWidget(analysis_title)
        layout.addWidget(self.analysis_box, 2)
        layout.addWidget(states_title)
        layout.addWidget(self.states_box)
        layout.addWidget(diagnostics_title)
        layout.addWidget(self.diagnostics_box)
        layout.addWidget(alerts_title)
        layout.addLayout(alert_rule_row)
        layout.addWidget(self.alerts_box)
        layout.addWidget(route_title)
        layout.addWidget(self.route_changes_box)
        layout.addLayout(sessions_header)
        layout.addWidget(self.sessions_box)
        layout.addWidget(export_title)
        layout.addLayout(export_row)
        layout.addLayout(statistics_export_row)
        layout.addWidget(export_note)
        layout.addStretch(1)
        return right

    def _build_footer(self) -> QFrame:
        footer = _panel("footer")
        layout = QHBoxLayout(footer)
        layout.setContentsMargins(14, 7, 14, 7)
        runtime = QLabel("Worker thread active only during measurement. subprocess console hidden on Windows.")
        runtime.setObjectName("muted")
        safety = QLabel("No raw sensitive output is exported unless explicitly requested.")
        safety.setObjectName("mutedStrong")
        layout.addWidget(runtime)
        layout.addStretch(1)
        layout.addWidget(safety)
        return footer

    def start_measurement(self) -> None:
        targets, invalid = parse_ipv4_targets(self.target_input.toPlainText())
        if invalid:
            QMessageBox.warning(self, "입력 오류", f"{IPV4_ONLY_MESSAGE}\n\n제외된 입력: {', '.join(invalid[:8])}")
            self.refresh_trace_targets()
            return
        if not targets:
            QMessageBox.warning(self, "입력 오류", "대상 IPv4 주소를 입력하세요.")
            return
        limited_targets = self._confirm_target_limit(targets)
        if limited_targets is None:
            return
        targets = limited_targets
        self._apply_trace_targets(targets)

        target = self.trace_target_combo.currentText().strip() or targets[0]
        valid, message = validate_target(target)
        if not valid or target not in targets:
            QMessageBox.warning(self, "입력 오류", message or "Tracert 대상은 등록된 IPv4 주소 중 하나여야 합니다.")
            return

        self.current_target = target
        self.current_targets = targets
        self.session_log_path = None
        self.route_log_path = None
        self.alert_action_log_path = None
        self.snapshots = []
        self.target_snapshot = None
        self.target_snapshots = []
        self.observations = []
        self.target_history = []
        self.selected_hop_index = None
        self.analysis = []
        self.route_changes = []
        self.alert_events = []
        self.alert_event_actions = {}
        self.pending_alert_image_keys = set()
        self.active_alert_keys = set()
        self._clear_focus_state()
        self._clear_timeline_state()
        self._sync_focus_controls()
        self._sync_alerts_box()
        self._sync_route_changes_box()
        self._sync_sessions_box()
        self.table.setRowCount(0)
        self.target_table.setRowCount(0)
        update_target_table(self.target_table, [])
        self.analysis_box.clear()
        self.graph.set_points([])
        self._update_graph_detail()
        self._update_target_summary(None)
        self._set_state_chip("탐색", "active")

        max_cycles = None if self.unlimited_check.isChecked() else self.count_spin.value()
        measurement_mode = self.measurement_mode_combo.currentData() or MEASUREMENT_MODE_FULL_ROUTE
        probe_engine = self.probe_engine_combo.currentData()
        self.worker = self.worker_factory(
            target=target,
            interval_seconds=int(self.interval_combo.currentText()),
            max_cycles=max_cycles,
            targets=targets,
            measurement_mode=measurement_mode,
            probe_engine=probe_engine,
            tcp_port=self.tcp_port_spin.value(),
        )
        self.worker.trace_completed.connect(self.on_trace_completed)
        if hasattr(self.worker, "route_changed"):
            self.worker.route_changed.connect(self.on_route_changed)
        self.worker.measurement_updated.connect(self.on_measurement_updated)
        if hasattr(self.worker, "session_log_ready"):
            self.worker.session_log_ready.connect(self.on_session_log_ready)
        if hasattr(self.worker, "diagnostics_updated"):
            self.worker.diagnostics_updated.connect(self.on_diagnostics_updated)
        self.worker.status_message.connect(self.on_status_message)
        self.worker.error_message.connect(self.on_worker_error)
        self.worker.finished.connect(self.on_worker_finished)
        self._set_running(True)
        self.worker.start()

    def stop_measurement(self) -> None:
        if self.worker:
            self.worker.request_stop()
            self.on_status_message("중지 요청 중...")
            self.stop_button.setEnabled(False)

    def pause_selected_targets(self) -> None:
        targets = self._selected_target_addresses()
        if not targets:
            self.status_label.setText("No selected targets to pause")
            return
        self._pause_targets(targets)

    def resume_selected_targets(self) -> None:
        targets = self._selected_target_addresses()
        if not targets:
            self.status_label.setText("No selected targets to resume")
            return
        self._resume_targets(targets)

    def pause_all_targets(self) -> None:
        self._pause_targets(list(self.current_targets))

    def resume_all_targets(self) -> None:
        self._resume_targets(list(self.current_targets))

    def apply_runtime_interval(self) -> None:
        if not self.worker or not hasattr(self.worker, "set_interval_seconds"):
            return
        interval = int(self.interval_combo.currentText())
        self.worker.set_interval_seconds(interval)
        self.status_label.setText(f"Runtime interval applied: {interval}s")

    def _pause_targets(self, targets: list[str]) -> None:
        if not targets or not self.worker or not hasattr(self.worker, "pause_targets"):
            return
        self.worker.pause_targets(targets)
        self.status_label.setText(f"Paused {len(targets)} target(s)")

    def _resume_targets(self, targets: list[str]) -> None:
        if not targets or not self.worker or not hasattr(self.worker, "resume_targets"):
            return
        self.worker.resume_targets(targets)
        self.status_label.setText(f"Resumed {len(targets)} target(s)")

    def _selected_target_addresses(self) -> list[str]:
        selected_rows = sorted({item.row() for item in self.target_table.selectedItems()})
        addresses: list[str] = []
        for row in selected_rows:
            item = self.target_table.item(row, 0)
            if item is not None and item.text():
                addresses.append(item.text())
        return addresses

    def on_target_double_clicked(self, row: int, _column: int) -> None:
        item = self.target_table.item(row, 0)
        if item is None or not item.text():
            return
        self.select_summary_target(item.text())

    def select_summary_target(self, target: str) -> None:
        if target not in {snapshot.address for snapshot in self.target_snapshots if snapshot.address}:
            return
        self.current_target = target
        if self.trace_target_combo.findText(target) >= 0:
            self.trace_target_combo.setCurrentText(target)
        selected_snapshot = self._target_snapshot_for_address(target)
        if selected_snapshot is not None:
            self.target_snapshot = selected_snapshot
        self.target_history = self._target_history_from_observations(self.observations)
        self.analysis = analyze_path(self.snapshots, self.target_snapshot)
        if self.focus_range is not None:
            self._rebuild_focus_view(show_status=True)
        else:
            self._render_current_view()
        self.status_label.setText(f"Summary target selected: {target}")

    def _target_snapshot_for_address(self, target: str) -> MetricSnapshot | None:
        return next((snapshot for snapshot in self.target_snapshots if snapshot.address == target), None)

    def _on_problem_sort_toggled(self, checked: bool) -> None:
        if checked:
            self._apply_target_problem_sort()
        else:
            self.target_table.sortItems(0, Qt.AscendingOrder)

    def _apply_target_problem_sort(self) -> None:
        self.target_table.sortItems(TARGET_SCORE_COLUMN, Qt.DescendingOrder)

    def on_status_message(self, message: str) -> None:
        self.status_label.setText(message)
        if "경로" in message:
            self._set_state_chip("탐색", "active")
        elif "측정 중" in message:
            self._set_state_chip("측정", "success")
        elif "중지" in message:
            self._set_state_chip("중지", "warning")
        elif "완료" in message:
            self._set_state_chip("완료", "success")

    def on_trace_completed(self, hops: object) -> None:
        populate_trace_table(self.table, hops)

    def on_route_changed(self, change: object) -> None:
        if not isinstance(change, RouteChange):
            return
        self._merge_route_changes([change])
        self._sync_route_changes_box()
        self.graph.set_annotations(self._timeline_annotations())
        self._update_graph_detail()
        self._save_pending_alert_images()

    def on_hop_selection_changed(self) -> None:
        selected_items = self.table.selectedItems()
        if not selected_items:
            return
        row = selected_items[0].row()
        hop_item = self.table.item(row, 0)
        if hop_item is None:
            return
        try:
            self.selected_hop_index = int(hop_item.text())
        except ValueError:
            return
        if self.graph_detail_window is not None:
            self.graph_detail_window.set_selected_hop_index(self.selected_hop_index)

    def on_hop_double_clicked(self, row: int, _column: int) -> None:
        hop_item = self.table.item(row, 0)
        if hop_item is None:
            return
        try:
            hop_index = int(hop_item.text())
        except ValueError:
            return
        self.selected_hop_index = hop_index
        was_visible = (
            self.graph_detail_window.is_hop_visible(hop_index)
            if self.graph_detail_window is not None
            else False
        )
        self.open_graph_detail()
        if self.graph_detail_window is not None:
            self.graph_detail_window.set_hop_visibility(hop_index, not was_visible)

    def on_measurement_updated(
        self,
        snapshots: object,
        target_snapshot: object,
        target_snapshots: object,
        analysis: object,
        observations: object,
        target_history: object,
    ) -> None:
        self.snapshots = list(snapshots)
        self.target_snapshots = list(target_snapshots)
        self.target_snapshot = self._target_snapshot_for_address(self.current_target) or target_snapshot
        self.analysis = analyze_path(self.snapshots, self.target_snapshot) if self.target_snapshot is not target_snapshot else list(analysis)
        self.observations = list(observations)
        self.target_history = self._target_history_from_observations(self.observations) or list(target_history)
        self._record_metric_alerts()

        if self.focus_range is not None:
            self._rebuild_focus_view(show_status=False)
        else:
            self._render_current_view()

    def _render_current_view(self) -> None:
        snapshots = self._display_snapshots()
        target_snapshots = self._display_target_snapshots()
        target_snapshot = self._display_target_snapshot()
        analysis = self._display_analysis()

        update_hop_table(self.table, snapshots)
        update_target_table(self.target_table, target_snapshots)
        if getattr(self, "problem_sort_check", None) is not None and self.problem_sort_check.isChecked():
            self._apply_target_problem_sort()
        self._update_all_targets_summary(target_snapshots)
        self._update_target_summary(target_snapshot)
        self.graph.set_points(self.target_history)
        self.graph.set_annotations(self._timeline_annotations())
        self._update_graph_detail()
        self._save_pending_alert_images()
        self.analysis_box.setPlainText("\n".join(f"- {line}" for line in analysis))
        self._set_export_enabled(self._has_export_data())

    def _display_snapshots(self) -> list[MetricSnapshot]:
        return self.focus_snapshots if self.focus_range is not None else self.snapshots

    def _display_target_snapshot(self) -> MetricSnapshot | None:
        return self.focus_target_snapshot if self.focus_range is not None else self.target_snapshot

    def _display_target_snapshots(self) -> list[MetricSnapshot]:
        return self.focus_target_snapshots if self.focus_range is not None else self.target_snapshots

    def _display_analysis(self) -> list[str]:
        if self.focus_range is None:
            return self.analysis
        return [self._focus_period_line(), *self.focus_analysis]

    def _update_all_targets_summary(self, snapshots: list[MetricSnapshot]) -> None:
        if not hasattr(self, "target_summary_status_label"):
            return
        self.target_summary_status_label.setText(_all_targets_summary_line(snapshots))

    def on_session_log_ready(self, path: str) -> None:
        self.session_log_path = Path(path)
        self.route_log_path = route_log_path_for_session(self.session_log_path)
        self.alert_action_log_path = alert_action_log_path_for_session(self.session_log_path)
        self.session_index_store = SessionIndexStore.create(session_index_root_for_sample_path(self.session_log_path))
        self.timeline_status = "Timeline source: session log ready"
        self._update_graph_detail()
        self._sync_sessions_box()
        self._set_export_enabled(self._has_export_data())

    def on_diagnostics_updated(self, diagnostics: object) -> None:
        lines = [
            f"target probe: {getattr(diagnostics, 'target_probe_engine', 'ICMP')}",
            f"route probe: {getattr(diagnostics, 'route_probe_engine', 'tracert/ICMP')}",
            f"tcp port: {getattr(diagnostics, 'tcp_port', '-') or '-'}",
            f"active ping: {getattr(diagnostics, 'active_ping_count', 0)}",
            f"pending ping: {getattr(diagnostics, 'pending_ping_count', 0)}",
            f"timeout targets: {getattr(diagnostics, 'timeout_target_count', 0)}",
            f"backoff targets: {getattr(diagnostics, 'backoff_target_count', 0)}",
            f"paused targets: {getattr(diagnostics, 'paused_target_count', 0)}",
            f"log queue: {getattr(diagnostics, 'log_queue_depth', 0)}",
            f"avg loop delay: {getattr(diagnostics, 'average_loop_delay_ms', 0.0):.1f} ms",
            f"last update: {getattr(diagnostics, 'last_update_iso', '-')}",
            f"tracert: {getattr(diagnostics, 'tracert_status', '-')}",
        ]
        self.diagnostics_box.setPlainText("\n".join(lines))

    def on_worker_error(self, message: str) -> None:
        QMessageBox.warning(self, "측정 오류", message)
        self.status_label.setText(message)
        self._set_state_chip("오류", "danger")

    def on_worker_finished(self) -> None:
        self._set_running(False)
        current_state = self.session_state_label.text()
        if current_state not in {"완료", "중지", "오류"}:
            if self.observations:
                self._set_state_chip("완료", "success")
            else:
                self._set_state_chip("대기", "neutral")
        self.worker = None
        self._sync_sessions_box()

    def _update_target_summary(self, target_snapshot: MetricSnapshot | None) -> None:
        if target_snapshot is None or target_snapshot.sent == 0:
            values = {
                "current": "-",
                "avg": "-",
                "loss": "-",
                "jitter": "-",
                "samples": "0",
            }
        else:
            values = {
                "current": f"{fmt_ms(target_snapshot.current_latency_ms)} ms" if target_snapshot.current_latency_ms is not None else "-",
                "avg": f"{fmt_ms(target_snapshot.avg_latency_ms)} ms" if target_snapshot.avg_latency_ms is not None else "-",
                "loss": f"{target_snapshot.loss_percent:.1f}%",
                "jitter": f"{fmt_ms(target_snapshot.jitter_ms)} ms" if target_snapshot.jitter_ms is not None else "-",
                "samples": str(target_snapshot.samples),
            }
        for key, value in values.items():
            self.metric_value_labels[key].setText(value)

    def load_timeline_range(self, seconds: int) -> None:
        end = self._timeline_end_time()
        if end is None:
            self.timeline_status = "Timeline source: no samples yet"
            self._update_graph_detail()
            self.status_label.setText("No timeline samples are available yet")
            return
        start = end - timedelta(seconds=seconds)
        observations = self._observations_for_range(start, end)
        self.timeline_range = (start, end)
        self.timeline_observations = observations
        self.timeline_target_history = self._target_history_from_observations(observations)
        focus_set = build_focus_snapshots(observations, current_target=self.current_target)
        self.timeline_snapshots = focus_set.hop_snapshots
        self.timeline_target_snapshot = focus_set.target_snapshot
        self._load_route_changes_for_range(start, end)
        source = "session log" if self.session_log_path else "live buffer"
        self.timeline_status = (
            f"Timeline: last {_format_duration(seconds)} from {source}, "
            f"{len(observations)} samples"
        )
        self._update_graph_detail()
        self.status_label.setText(self.timeline_status)

    def clear_timeline_range(self) -> None:
        self._clear_timeline_state()
        self._update_graph_detail()
        self.status_label.setText("Timeline restored to live buffer")

    def _timeline_end_time(self) -> datetime | None:
        bounds = session_log_bounds(self.session_log_path)
        if bounds is not None:
            return bounds[1]
        timestamps = [observation.timestamp for observation in [*self.observations, *self.target_history]]
        return max(timestamps) if timestamps else None

    def _observations_for_range(self, start: datetime, end: datetime) -> list[HopObservation]:
        if self.session_log_path is not None:
            observations = list(iter_observations_in_range(self.session_log_path, start, end))
            if observations:
                return observations
        return observations_in_range(self.observations, start, end)

    def _target_history_from_observations(self, observations: list[HopObservation]) -> list[HopObservation]:
        direct_target = [
            observation
            for observation in observations
            if observation.hop_index == 0 and (not self.current_target or observation.address == self.current_target)
        ]
        if direct_target:
            return direct_target
        return [
            observation
            for observation in observations
            if observation.is_target and (not self.current_target or observation.address == self.current_target)
        ]

    def apply_focus_range(self, selection: object) -> None:
        if not selection:
            return
        start, end = selection
        if not isinstance(start, datetime) or not isinstance(end, datetime):
            return
        if end < start:
            start, end = end, start
        self.focus_range = (start, end)
        self._rebuild_focus_view(show_status=True)

    def clear_focus_range(self, *_args, render: bool = True) -> None:
        self._clear_focus_state()
        self._sync_focus_controls()
        if render:
            self._render_current_view()
            self.status_label.setText("Live view restored")

    def _rebuild_focus_view(self, *, show_status: bool) -> None:
        if self.focus_range is None:
            return
        start, end = self.focus_range
        self.focus_observations = self._observations_for_range(start, end)
        focus_set = build_focus_snapshots(self.focus_observations, current_target=self.current_target)
        self.focus_snapshots = focus_set.hop_snapshots
        self.focus_target_snapshots = focus_set.target_snapshots
        self.focus_target_snapshot = focus_set.target_snapshot
        self.focus_analysis = analyze_path(self.focus_snapshots, self.focus_target_snapshot)
        self._sync_focus_controls()
        self._render_current_view()
        if show_status:
            self.status_label.setText(
                f"Focus period applied: {start.strftime('%H:%M:%S')} - {end.strftime('%H:%M:%S')}"
            )

    def _clear_focus_state(self) -> None:
        self.focus_range = None
        self.focus_observations = []
        self.focus_snapshots = []
        self.focus_target_snapshot = None
        self.focus_target_snapshots = []
        self.focus_analysis = []

    def _clear_timeline_state(self) -> None:
        self.timeline_range = None
        self.timeline_observations = []
        self.timeline_target_history = []
        self.timeline_snapshots = []
        self.timeline_target_snapshot = None
        self.timeline_status = "Timeline source: live buffer"

    def _timeline_annotations(self) -> list[TimelineAnnotation]:
        return [*self._route_timeline_annotations(), *self._alert_timeline_annotations()]

    def _route_timeline_annotations(self) -> list[TimelineAnnotation]:
        return [
            TimelineAnnotation(
                change.timestamp,
                change.timestamp,
                "Route changed",
                None,
            )
            for change in self.route_changes
        ]

    def _load_route_changes_for_range(self, start: datetime, end: datetime) -> None:
        if self.route_log_path is None:
            self.route_log_path = route_log_path_for_session(self.session_log_path)
        loaded = route_changes_in_range(self.route_log_path, start, end)
        if loaded:
            self._merge_route_changes(loaded, record_alert_actions=False)
            self._sync_route_changes_box()
            self.graph.set_annotations(self._timeline_annotations())

    def _merge_route_changes(self, changes: list[RouteChange], *, record_alert_actions: bool = True) -> None:
        if not changes:
            return
        existing_keys = {
            (change.timestamp.isoformat(timespec="seconds"), change.summary)
            for change in self.route_changes
        }
        for change in sorted(changes, key=lambda item: item.timestamp):
            key = (change.timestamp.isoformat(timespec="seconds"), change.summary)
            if key in existing_keys:
                continue
            self.route_changes.append(change)
            existing_keys.add(key)
            self._append_alert_event(route_change_alert(change.timestamp, change.summary), record_actions=record_alert_actions)
        self.route_changes = sorted(self.route_changes, key=lambda item: item.timestamp)[-100:]

    def _alert_timeline_annotations(self) -> list[TimelineAnnotation]:
        return [
            TimelineAnnotation(
                event.start,
                event.end,
                event.title,
                event.series_key,
            )
            for event in self.alert_events
            if not event.key.startswith("route_changed:")
            and self._alert_event_has_action(event, "timeline_annotation")
        ]

    def _record_metric_alerts(self) -> None:
        previous_active_keys = set(self.active_alert_keys)
        active_keys, events = evaluate_target_alerts(
            self.target_history,
            current_target=self.current_target,
            config=self._alert_rule_config(),
        )
        for event in events:
            if event.key not in previous_active_keys:
                self._append_alert_event(event)
        ended_keys = previous_active_keys - active_keys
        if ended_keys:
            timestamp = self.target_history[-1].timestamp if self.target_history else datetime.now()
            for key in sorted(ended_keys):
                self._append_alert_event(alert_recovery_event(key, timestamp))
        self.active_alert_keys = active_keys

    def _alert_rule_config(self) -> AlertRuleConfig:
        loss_threshold = float(self.loss_threshold_spin.value()) if hasattr(self, "loss_threshold_spin") else 20.0
        loss_window_minutes = self.loss_window_spin.value() if hasattr(self, "loss_window_spin") else 3
        latency_threshold = float(self.latency_threshold_spin.value()) if hasattr(self, "latency_threshold_spin") else 100.0
        jitter_threshold = float(self.jitter_threshold_spin.value()) if hasattr(self, "jitter_threshold_spin") else 30.0
        sample_window = self.sample_window_spin.value() if hasattr(self, "sample_window_spin") else 10
        sample_bad = self.sample_bad_spin.value() if hasattr(self, "sample_bad_spin") else 10
        return AlertRuleConfig(
            loss_threshold_percent=loss_threshold,
            loss_window_seconds=int(loss_window_minutes) * 60,
            latency_threshold_ms=latency_threshold,
            jitter_threshold_ms=jitter_threshold,
            sample_window_count=int(sample_window),
            sample_failure_count=int(sample_bad),
        )

    def _append_alert_event(
        self,
        event: AlertEvent,
        *,
        record_actions: bool = True,
        actions: list[str] | None = None,
    ) -> None:
        if any(existing.key == event.key for existing in self.alert_events):
            if actions is not None:
                self.alert_event_actions[event.key] = actions
            return
        self.alert_events.append(event)
        self.alert_events = self.alert_events[-100:]
        if actions is not None:
            self.alert_event_actions[event.key] = actions
        elif record_actions:
            self.alert_event_actions[event.key] = self._record_alert_actions(event)
        self._sync_alerts_box()

    def _record_alert_actions(self, event: AlertEvent) -> list[str]:
        actions = self._selected_alert_actions()
        if "beep" in actions:
            QApplication.beep()
        if "image" in actions:
            self.pending_alert_image_keys.add(event.key)
        if not actions:
            return []
        append_alert_action(
            self.alert_action_log_path,
            event,
            actions=actions,
        )
        return actions

    def _selected_alert_actions(self) -> list[str]:
        if not hasattr(self, "alert_timeline_action_check"):
            return ["timeline_annotation", "comment"]
        actions: list[str] = []
        if self.alert_timeline_action_check.isChecked():
            actions.append("timeline_annotation")
        if self.alert_comment_action_check.isChecked():
            actions.append("comment")
        if self.alert_beep_action_check.isChecked():
            actions.append("beep")
        if self.alert_image_action_check.isChecked():
            actions.append("image")
        return actions

    def _save_pending_alert_images(self) -> None:
        if not self.pending_alert_image_keys:
            return
        for key in list(self.pending_alert_image_keys):
            event = next((item for item in self.alert_events if item.key == key), None)
            if event is None or not self._alert_event_has_action(event, "image"):
                self.pending_alert_image_keys.discard(key)
                continue
            try:
                saved_path = self._save_graph_png(self._alert_image_path(event))
            except (OSError, RuntimeError) as exc:
                self.status_label.setText(f"Alert image save failed: {exc}")
            else:
                self.status_label.setText(f"Alert image saved: {saved_path}")
            finally:
                self.pending_alert_image_keys.discard(key)

    def _alert_image_path(self, event: AlertEvent) -> Path:
        if self.alert_action_log_path is not None:
            base = self.alert_action_log_path.parent / "alert_images"
        else:
            base = Path.cwd() / "exports" / "alert_images"
        stamp = event.timestamp.strftime("%Y%m%d_%H%M%S")
        target = safe_target_name(self.current_target or "target")
        title = safe_target_name(event.title)
        key = safe_target_name(event.key)[:80]
        return base / f"alert_{target}_{stamp}_{title}_{key}.png"

    def _alert_event_has_action(self, event: AlertEvent, action: str) -> bool:
        actions = self.alert_event_actions.get(event.key)
        if actions is None:
            return True
        return action in actions

    def _sync_alerts_box(self) -> None:
        if not hasattr(self, "alerts_box"):
            return
        if not self.alert_events:
            self.alerts_box.setPlainText("No alert events.")
            return
        lines = [
            f"{event.timestamp.strftime('%H:%M:%S')} | {event.severity.upper()} | {event.title}: {event.message}"
            for event in reversed(self.alert_events[-8:])
        ]
        self.alerts_box.setPlainText("\n".join(lines))

    def _sync_route_changes_box(self) -> None:
        if not hasattr(self, "route_changes_box"):
            return
        if not self.route_changes:
            self.route_changes_box.setPlainText("No route changes detected.")
            return
        blocks: list[str] = []
        for change in reversed(self.route_changes[-5:]):
            blocks.append(
                "\n".join(
                    [
                        f"{change.timestamp.strftime('%H:%M:%S')} | {change.summary}",
                        f"Before: {route_path(change.previous)}",
                        f"After:  {route_path(change.current)}",
                        self._route_change_impact_line(change),
                    ]
                )
            )
        self.route_changes_box.setPlainText("\n\n".join(blocks))

    def _sync_sessions_box(self) -> None:
        if not hasattr(self, "sessions_box"):
            return
        if not bool(self.worker and self.worker.isRunning()):
            self.session_index_store.recover_stale_active_sessions(
                stale_after=timedelta(seconds=STALE_ACTIVE_SESSION_RECOVERY_SECONDS)
            )
            self.session_index_store.reconcile_missing_session_files()
        sessions = self.session_index_store.list_sessions()[:6]
        self._sync_session_combo(sessions)
        if not sessions:
            self.sessions_box.setPlainText("No saved sessions.")
            return
        lines = []
        for session in sessions:
            end = session.end.strftime("%H:%M:%S") if session.end is not None else "running"
            lines.append(
                " | ".join(
                    [
                        session.state,
                        session.start.strftime("%Y-%m-%d %H:%M:%S"),
                        f"end {end}",
                        session.target,
                        f"samples {session.samples}",
                        session.measurement_mode or "-",
                    ]
                )
            )
        self.sessions_box.setPlainText("\n".join(lines))

    def refresh_saved_sessions(self) -> None:
        self.session_index_store.recover_missing_sessions()
        self._sync_sessions_box()
        self.status_label.setText("Session list refreshed from saved logs")

    def _sync_session_combo(self, sessions: list[TraceSessionRecord]) -> None:
        if not hasattr(self, "session_combo"):
            return
        selected_session_id = self.session_combo.currentData()
        self.session_combo.blockSignals(True)
        self.session_combo.clear()
        for session in sessions:
            label = f"{session.target} | {session.start.strftime('%m-%d %H:%M')} | {session.samples}"
            self.session_combo.addItem(label, session.session_id)
        if selected_session_id:
            selected_index = self.session_combo.findData(selected_session_id)
            if selected_index >= 0:
                self.session_combo.setCurrentIndex(selected_index)
        self.session_combo.blockSignals(False)
        has_sessions = bool(sessions)
        self.session_combo.setEnabled(has_sessions)
        can_switch_session = has_sessions and not bool(self.worker and self.worker.isRunning())
        self.open_session_button.setEnabled(can_switch_session)
        self.resume_session_button.setEnabled(can_switch_session)
        self.export_session_button.setEnabled(has_sessions)
        self.delete_session_button.setEnabled(has_sessions and not bool(self.worker and self.worker.isRunning()))

    def open_selected_session(self) -> None:
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "Session", "Stop the current measurement before opening a saved session.")
            return
        record = self._selected_session_record()
        if record is None:
            return
        self._open_session_record(record)

    def resume_selected_session(self) -> None:
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "Session", "Stop the current measurement before resuming a saved session.")
            return
        record = self._selected_session_record()
        if record is None:
            return
        self._prepare_session_resume(record)

    def export_selected_session(self) -> None:
        if self.export_worker and self.export_worker.isRunning():
            QMessageBox.information(self, "Export", "An export is already running.")
            return
        record = self._selected_session_record()
        if record is None:
            return
        if not record.sample_path.exists():
            self.status_label.setText(f"Session log missing: {record.sample_path}")
            return
        path = self._select_save_path("session.csv", "CSV Files (*.csv)", target=record.target)
        if not path:
            return
        observations = list(iter_observations(record.sample_path))
        snapshot_set = build_focus_snapshots(observations, current_target=record.target)
        snapshots = [*snapshot_set.hop_snapshots, *snapshot_set.target_snapshots]
        analysis = analyze_path(snapshot_set.hop_snapshots, snapshot_set.target_snapshot)
        self.export_worker = ExportWorker(
            kind="csv",
            path=path,
            target=record.target,
            session_log_path=record.sample_path,
            snapshots=snapshots,
            analysis=analysis,
            annotations=[],
            focus_range=None,
        )
        self.export_worker.status_message.connect(self.on_export_status)
        self.export_worker.export_completed.connect(self.on_export_completed)
        self.export_worker.error_message.connect(self.on_export_error)
        self.export_worker.finished.connect(self.on_export_finished)
        self._set_exporting(True)
        self.export_worker.start()

    def delete_selected_session(self) -> None:
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "Session", "Stop the current measurement before deleting a saved session.")
            return
        if self.export_worker and self.export_worker.isRunning():
            QMessageBox.information(self, "Session", "Wait for the current export to finish before deleting a session.")
            return
        record = self._selected_session_record()
        if record is None:
            return
        reply = QMessageBox.question(
            self,
            "Delete Session",
            f"Delete saved session for {record.target}?\n\nThis removes the session log, route log, and alert log files.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        deleted = self.session_index_store.delete_session(record.session_id)
        if deleted is None:
            self.status_label.setText("Selected session is no longer available")
        else:
            self._clear_deleted_session_paths(deleted)
            self.status_label.setText(f"Deleted session: {deleted.target}")
        self._sync_sessions_box()
        self._set_export_enabled(self._has_export_data())

    def _selected_session_record(self) -> TraceSessionRecord | None:
        session_id = self.session_combo.currentData() if hasattr(self, "session_combo") else None
        if not session_id:
            self.status_label.setText("No saved session selected")
            return None
        record = self.session_index_store.find_session(str(session_id))
        if record is None:
            self.status_label.setText("Selected session is no longer available")
            self._sync_sessions_box()
            return None
        return record

    def _clear_deleted_session_paths(self, record: TraceSessionRecord) -> None:
        if self.session_log_path != record.sample_path:
            return
        self.session_log_path = None
        self.route_log_path = None
        self.alert_action_log_path = None
        self.timeline_status = "Timeline source: live buffer"

    def _open_session_record(self, record: TraceSessionRecord) -> None:
        if not record.sample_path.exists():
            self.status_label.setText(f"Session log missing: {record.sample_path}")
            return
        observations = list(iter_observations(record.sample_path))
        self.current_target = record.target
        self.current_targets = [record.target]
        self.target_input.setPlainText(record.target)
        self.refresh_trace_targets()
        self.trace_target_combo.setCurrentText(record.target)
        self.session_log_path = record.sample_path
        self.route_log_path = record.route_path or route_log_path_for_session(record.sample_path)
        self.alert_action_log_path = alert_action_log_path_for_session(record.sample_path)
        self.session_index_store = SessionIndexStore.create(session_index_root_for_sample_path(record.sample_path))
        self.observations = observations
        self.target_history = self._target_history_from_observations(observations)
        focus_set = build_focus_snapshots(observations, current_target=record.target)
        self.snapshots = focus_set.hop_snapshots
        self.target_snapshot = focus_set.target_snapshot
        self.target_snapshots = focus_set.target_snapshots
        self.analysis = analyze_path(self.snapshots, self.target_snapshot)
        self.route_changes = []
        self.alert_events = []
        self.alert_event_actions = {}
        self.pending_alert_image_keys = set()
        self.active_alert_keys = set()
        self._clear_focus_state()
        self._clear_timeline_state()
        bounds = session_log_bounds(self.session_log_path)
        if bounds is not None:
            self._load_route_changes_for_range(*bounds)
        self._load_saved_alert_actions()
        self.timeline_status = f"Timeline source: opened session, {len(observations)} samples"
        self._sync_alerts_box()
        self._sync_route_changes_box()
        self._sync_focus_controls()
        self._render_current_view()
        self._set_state_chip("Loaded", "active")
        self.status_label.setText(f"Loaded session: {record.target}, samples {len(observations)}")
        self._sync_sessions_box()

    def _prepare_session_resume(self, record: TraceSessionRecord) -> None:
        targets = self._targets_for_session_resume(record)
        if not targets:
            self.status_label.setText("No saved session target is available")
            return
        self.current_target = record.target if record.target in targets else targets[0]
        self.current_targets = targets
        self.target_input.setPlainText("\n".join(targets))
        self.refresh_trace_targets()
        if self.trace_target_combo.findText(self.current_target) >= 0:
            self.trace_target_combo.setCurrentText(self.current_target)
        self._restore_session_runtime_controls(record)
        self.status_label.setText(
            f"Resume prepared: {len(targets)} target(s), press Start to create a new session"
        )

    def _targets_for_session_resume(self, record: TraceSessionRecord) -> list[str]:
        targets: list[str] = []
        for target in [record.target, *self._targets_from_session_log(record.sample_path)]:
            if not target or target in targets:
                continue
            targets.append(target)
            if len(targets) >= MAX_IPV4_TARGETS:
                break
        return targets

    def _targets_from_session_log(self, path: Path) -> list[str]:
        if not path.exists():
            return []
        targets: list[str] = []
        for observation in iter_observations(path):
            if observation.hop_index != 0 and not observation.is_target:
                continue
            if not observation.address or observation.address in targets:
                continue
            targets.append(observation.address)
            if len(targets) >= MAX_IPV4_TARGETS:
                break
        return targets

    def _restore_session_runtime_controls(self, record: TraceSessionRecord) -> None:
        if record.interval_seconds and record.interval_seconds > 0:
            interval_text = str(record.interval_seconds)
            interval_index = self.interval_combo.findText(interval_text)
            if interval_index < 0:
                self.interval_combo.addItem(interval_text)
                interval_index = self.interval_combo.findText(interval_text)
            self.interval_combo.setCurrentIndex(interval_index)

        mode, probe_engine, tcp_port = _parse_session_measurement_mode(record.measurement_mode)
        mode_index = self.measurement_mode_combo.findData(mode)
        if mode_index >= 0:
            self.measurement_mode_combo.setCurrentIndex(mode_index)
        probe_index = self.probe_engine_combo.findData(probe_engine)
        if probe_index >= 0:
            self.probe_engine_combo.setCurrentIndex(probe_index)
        if tcp_port is not None:
            self.tcp_port_spin.setValue(tcp_port)
        self._on_probe_engine_changed()

    def _load_saved_alert_actions(self) -> None:
        for index, row in enumerate(read_alert_actions(self.alert_action_log_path)):
            event = _alert_event_from_action_row(row, index)
            if event is None:
                continue
            actions = [action for action in row.get("actions", "").split(";") if action]
            self._append_alert_event(event, record_actions=False, actions=actions)

    def _route_change_impact_line(self, change: RouteChange) -> str:
        points = self._target_points_for_route_impact()
        if not points:
            return "Impact: target samples not available"
        window = timedelta(seconds=120)
        before = [
            point
            for point in points
            if change.timestamp - window <= point.timestamp < change.timestamp
        ]
        after = [
            point
            for point in points
            if change.timestamp <= point.timestamp <= change.timestamp + window
        ]
        return f"Impact: before {self._format_impact_points(before)} | after {self._format_impact_points(after)}"

    def _target_points_for_route_impact(self) -> list[HopObservation]:
        source = self.timeline_target_history if self.timeline_range is not None else self.target_history
        if source:
            return list(source)
        observations = self.timeline_observations if self.timeline_range is not None else self.observations
        return self._target_history_from_observations(observations)

    def _format_impact_points(self, points: list[HopObservation]) -> str:
        if not points:
            return "no samples"
        failures = sum(1 for point in points if not point.success)
        loss_percent = failures / len(points) * 100
        latencies = [point.latency_ms for point in points if point.success and point.latency_ms is not None]
        avg_latency = (sum(latencies) / len(latencies)) if latencies else None
        max_latency = max(latencies) if latencies else None
        return (
            f"loss {loss_percent:.1f}% "
            f"avg {fmt_ms(avg_latency) or '-'} ms "
            f"max {fmt_ms(max_latency) or '-'} ms"
        )

    def _sync_focus_controls(self) -> None:
        if not hasattr(self, "focus_label"):
            return
        focused = self.focus_range is not None
        self.focus_label.setText(self._focus_period_line() if focused else "Live")
        self.focus_label.setProperty("tone", "active" if focused else "neutral")
        self.focus_label.style().unpolish(self.focus_label)
        self.focus_label.style().polish(self.focus_label)
        self.clear_focus_button.setEnabled(focused)

    def _focus_period_line(self) -> str:
        if self.focus_range is None:
            return "Live"
        start, end = self.focus_range
        return f"Focus period: {start.strftime('%H:%M:%S')} - {end.strftime('%H:%M:%S')}"

    def open_graph_detail(self) -> None:
        if self.graph_detail_window is None:
            self.graph_detail_window = GraphDetailWindow()
            self.graph_detail_window.focus_applied.connect(self.apply_focus_range)
            self.graph_detail_window.focus_cleared.connect(self.clear_focus_range)
            self.graph_detail_window.timeline_range_requested.connect(self.load_timeline_range)
            self.graph_detail_window.timeline_live_requested.connect(self.clear_timeline_range)
        self._update_graph_detail()
        self.graph_detail_window.show()
        self.graph_detail_window.raise_()
        self.graph_detail_window.activateWindow()

    def _update_graph_detail(self) -> None:
        if self.graph_detail_window is None:
            return
        if self.timeline_range is None:
            target_snapshot = self._display_target_snapshot()
            target_history = self.target_history
            observations = self.observations
            snapshots = self._display_snapshots()
        else:
            target_snapshot = self.timeline_target_snapshot
            target_history = self.timeline_target_history
            observations = self.timeline_observations
            snapshots = self.timeline_snapshots
        self.graph_detail_window.set_data(
            self.current_target,
            target_snapshot,
            target_history,
            observations,
            snapshots,
            self.selected_hop_index,
        )
        self.graph_detail_window.set_external_annotations(self._timeline_annotations())
        self.graph_detail_window.set_timeline_status(self.timeline_status)

    def save_csv(self) -> None:
        self._start_export("csv", "csv", "CSV Files (*.csv)")

    def save_xlsx(self) -> None:
        self._start_export("xlsx", "xlsx", "Excel Files (*.xlsx)")

    def save_report(self) -> None:
        self._start_export("txt", "txt", "Text Files (*.txt)")

    def save_graph_png(self) -> None:
        if self.export_worker and self.export_worker.isRunning():
            QMessageBox.information(self, "Export", "An export is already running.")
            return
        path = self._select_save_path("png", "PNG Files (*.png)")
        if not path:
            return
        try:
            saved_path = self._save_graph_png(path)
        except RuntimeError as exc:
            QMessageBox.warning(self, "Export error", str(exc))
            self.status_label.setText(str(exc))
            return
        self.status_label.setText(f"PNG saved: {saved_path}")

    def _save_graph_png(self, path: Path) -> Path:
        if path.suffix.lower() != ".png":
            path = path.with_suffix(".png")
        path.parent.mkdir(parents=True, exist_ok=True)
        pixmap = self.graph.grab()
        if pixmap.isNull():
            raise RuntimeError(f"PNG capture failed: {path}")
        if not pixmap.save(str(path), "PNG"):
            raise RuntimeError(f"PNG save failed: {path}")
        return path

    def save_target_summary_csv(self) -> None:
        if self.export_worker and self.export_worker.isRunning():
            QMessageBox.information(self, "Export", "An export is already running.")
            return
        snapshots = list(self._display_target_snapshots())
        if not snapshots:
            self.status_label.setText("No target summary data to export")
            return
        if getattr(self, "problem_sort_check", None) is not None and self.problem_sort_check.isChecked():
            snapshots.sort(key=target_problem_score, reverse=True)
        path = self._select_save_path("target_summary.csv", "CSV Files (*.csv)", target="all_targets")
        if not path:
            return
        try:
            saved_path = export_target_summary_csv(path, self._target_summary_export_rows(snapshots))
        except OSError as exc:
            QMessageBox.warning(self, "Export error", str(exc))
            self.status_label.setText(str(exc))
            return
        self.status_label.setText(f"Target summary CSV saved: {saved_path}")

    def _target_summary_export_rows(self, snapshots: list[MetricSnapshot]) -> list[TargetSummaryExportRow]:
        rows: list[TargetSummaryExportRow] = []
        for snapshot in snapshots:
            failed = snapshot.sent - snapshot.received
            rows.append(
                TargetSummaryExportRow(
                    target=snapshot.address or "",
                    status=display_status(snapshot),
                    current_latency_ms=snapshot.current_latency_ms,
                    avg_latency_ms=snapshot.avg_latency_ms,
                    min_latency_ms=snapshot.min_latency_ms,
                    max_latency_ms=snapshot.max_latency_ms,
                    loss_percent=snapshot.loss_percent,
                    recent_loss_percent=snapshot.recent_loss_percent,
                    sent=snapshot.sent,
                    received=snapshot.received,
                    failed=failed,
                    timeout_count=snapshot.timeout_count,
                    jitter_ms=snapshot.jitter_ms,
                    samples=snapshot.samples,
                    score=target_problem_score(snapshot),
                )
            )
        return rows

    def save_statistics_csv(self) -> None:
        self._start_export(
            "stats_csv",
            "stats.csv",
            "CSV Files (*.csv)",
            statistics_options=self._statistics_export_options(),
            export_range=self._statistics_export_range(),
        )

    def save_statistics_xlsx(self) -> None:
        self._start_export(
            "stats_xlsx",
            "stats.xlsx",
            "Excel Files (*.xlsx)",
            statistics_options=self._statistics_export_options(),
            export_range=self._statistics_export_range(),
        )

    def _statistics_export_options(self) -> StatisticsExportOptions:
        return StatisticsExportOptions(
            grouping_seconds=int(self.statistics_group_combo.currentData() or 300),
            timezone_mode=str(self.statistics_timezone_combo.currentData() or TIMEZONE_LOCAL),
        )

    def _statistics_export_range(self) -> tuple[datetime, datetime] | None:
        scope = self.statistics_scope_combo.currentData() if hasattr(self, "statistics_scope_combo") else STATISTICS_SCOPE_ALL
        if scope == STATISTICS_SCOPE_VISIBLE:
            return self.timeline_range
        if scope == STATISTICS_SCOPE_FOCUS:
            return self.focus_range
        if scope == STATISTICS_SCOPE_CUSTOM:
            return self._statistics_custom_range()
        return None

    def _statistics_custom_range(self) -> tuple[datetime, datetime]:
        start = _datetime_from_qdatetime(self.statistics_start_edit.dateTime())
        end = _datetime_from_qdatetime(self.statistics_end_edit.dateTime())
        if end < start:
            start, end = end, start
        return start, end

    def _set_statistics_custom_range(self, start: datetime, end: datetime) -> None:
        self.statistics_start_edit.setDateTime(_qdatetime_from_datetime(start))
        self.statistics_end_edit.setDateTime(_qdatetime_from_datetime(end))

    def _sync_statistics_range_controls(self, *_args, exporting: bool | None = None) -> None:
        if not hasattr(self, "statistics_scope_combo"):
            return
        custom = self.statistics_scope_combo.currentData() == STATISTICS_SCOPE_CUSTOM
        is_exporting = bool(self.export_worker and self.export_worker.isRunning()) if exporting is None else exporting
        self.statistics_start_edit.setEnabled(custom and not is_exporting)
        self.statistics_end_edit.setEnabled(custom and not is_exporting)

    def _start_export(
        self,
        kind: str,
        extension: str,
        file_filter: str,
        *,
        statistics_options: StatisticsExportOptions | None = None,
        export_range: tuple[datetime, datetime] | None = None,
    ) -> None:
        if self.export_worker and self.export_worker.isRunning():
            QMessageBox.information(self, "저장 진행 중", "이미 저장 작업이 진행 중입니다.")
            return
        path = self._select_save_path(extension, file_filter)
        if not path:
            return
        self.export_worker = ExportWorker(
            kind=kind,
            path=path,
            target=self.current_target,
            session_log_path=self.session_log_path,
            snapshots=self.snapshots_for_export(),
            analysis=self.analysis_for_export(),
            annotations=self.annotations_for_export(),
            focus_range=export_range if statistics_options is not None else self.focus_range,
            observations_override=(
                self._observations_override_for_export(export_range if statistics_options is not None else self.focus_range)
                if self.session_log_path is None
                else None
            ),
            statistics_options=statistics_options,
        )
        self.export_worker.status_message.connect(self.on_export_status)
        self.export_worker.export_completed.connect(self.on_export_completed)
        self.export_worker.error_message.connect(self.on_export_error)
        self.export_worker.finished.connect(self.on_export_finished)
        self._set_exporting(True)
        self.export_worker.start()

    def on_export_status(self, message: str) -> None:
        self.status_label.setText(message)

    def on_export_completed(self, path: str) -> None:
        self.status_label.setText(f"저장 완료: {path}")

    def on_export_error(self, message: str) -> None:
        QMessageBox.warning(self, "저장 오류", message)
        self.status_label.setText(message)

    def on_export_finished(self) -> None:
        self.export_worker = None
        self._set_exporting(False)

    def _observations_override_for_export(
        self,
        export_range: tuple[datetime, datetime] | None,
    ) -> list[HopObservation] | None:
        if export_range is None:
            return None
        if export_range == self.focus_range and self.focus_observations:
            return list(self.focus_observations)
        start, end = export_range
        return self._observations_for_range(start, end)

    def snapshots_for_export(self) -> list[MetricSnapshot]:
        snapshots = list(self._display_snapshots())
        snapshots.extend(self._display_target_snapshots())
        return snapshots

    def analysis_for_export(self) -> list[str]:
        return list(self._display_analysis())

    def annotations_for_export(self) -> list[ExportAnnotation]:
        annotations: list[ExportAnnotation] = []
        for event in self.alert_events:
            if not self._alert_event_should_export(event):
                continue
            source = "route" if event.key.startswith("route_changed:") else "alert"
            annotations.append(
                ExportAnnotation(
                    start=event.start,
                    end=event.end,
                    source=source,
                    severity=event.severity,
                    title=event.title,
                    message=event.message,
                )
            )
        if self.graph_detail_window is not None:
            for annotation in self.graph_detail_window._annotations:
                annotations.append(
                    ExportAnnotation(
                        start=annotation.start,
                        end=annotation.end,
                        source="manual",
                        severity="",
                        title=annotation.label,
                        message=annotation.label,
                    )
                )
        return annotations_in_range(annotations, self.focus_range)

    def _alert_event_should_export(self, event: AlertEvent) -> bool:
        actions = self.alert_event_actions.get(event.key)
        if actions is None:
            return True
        return bool({"timeline_annotation", "comment"}.intersection(actions))

    def _select_save_path(self, extension: str, file_filter: str, *, target: str | None = None) -> Path | None:
        default = default_export_path(target or self.current_target or "target", extension, Path.cwd() / "exports")
        default.parent.mkdir(parents=True, exist_ok=True)
        selected, _ = QFileDialog.getSaveFileName(self, "저장", str(default), file_filter)
        return Path(selected) if selected else None

    def _on_unlimited_toggled(self, checked: bool) -> None:
        self.count_spin.setDisabled(checked or bool(self.worker and self.worker.isRunning()))

    def _on_probe_engine_changed(self, *_args) -> None:
        tcp_selected = self._is_tcp_probe_selected()
        running = bool(self.worker and self.worker.isRunning())
        if hasattr(self, "tcp_port_spin"):
            self.tcp_port_spin.setEnabled(tcp_selected and not running)
        if hasattr(self, "engine_note_label"):
            if tcp_selected:
                self.engine_note_label.setText(
                    "TCP Connect measures the final target service port. Full Route still uses Windows tracert/ICMP."
                )
            else:
                self.engine_note_label.setText("ICMP uses Windows ICMP echo for target checks and tracert/ICMP for routes.")

    def _is_tcp_probe_selected(self) -> bool:
        return (
            hasattr(self, "probe_engine_combo")
            and self.probe_engine_combo.currentData() == PROBE_ENGINE_TCP_CONNECT
        )

    def _set_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.target_input.setEnabled(not running)
        self.trace_target_combo.setEnabled(not running)
        self.refresh_targets_button.setEnabled(not running)
        self.measurement_mode_combo.setEnabled(not running)
        self.probe_engine_combo.setEnabled(not running)
        self.tcp_port_spin.setEnabled(not running and self._is_tcp_probe_selected())
        if hasattr(self, "session_combo"):
            has_sessions = self.session_combo.count() > 0
            self.session_combo.setEnabled(has_sessions and not running)
            self.open_session_button.setEnabled(has_sessions and not running)
            self.resume_session_button.setEnabled(has_sessions and not running)
            self.delete_session_button.setEnabled(has_sessions and not running)
        self.interval_combo.setEnabled(True)
        self.count_spin.setEnabled(not running and not self.unlimited_check.isChecked())
        self.unlimited_check.setEnabled(not running)
        for button_name in (
            "pause_selected_targets_button",
            "resume_selected_targets_button",
            "pause_all_targets_button",
            "resume_all_targets_button",
            "apply_interval_button",
        ):
            button = getattr(self, button_name, None)
            if button is not None:
                button.setEnabled(running)
        self._set_export_enabled(self._has_export_data())

    def _set_export_enabled(self, enabled: bool) -> None:
        if self.export_worker and self.export_worker.isRunning():
            enabled = False
        self.csv_button.setEnabled(enabled)
        self.xlsx_button.setEnabled(enabled)
        self.report_button.setEnabled(enabled)
        self.graph_png_button.setEnabled(enabled)
        self.stats_csv_button.setEnabled(enabled)
        self.stats_xlsx_button.setEnabled(enabled)
        if hasattr(self, "export_target_summary_button"):
            self.export_target_summary_button.setEnabled(enabled and self._has_target_summary_data())

    def _set_exporting(self, exporting: bool) -> None:
        self.csv_button.setEnabled(not exporting and self._has_export_data())
        self.xlsx_button.setEnabled(not exporting and self._has_export_data())
        self.report_button.setEnabled(not exporting and self._has_export_data())
        self.graph_png_button.setEnabled(not exporting and self._has_export_data())
        self.stats_csv_button.setEnabled(not exporting and self._has_export_data())
        self.stats_xlsx_button.setEnabled(not exporting and self._has_export_data())
        if hasattr(self, "export_target_summary_button"):
            self.export_target_summary_button.setEnabled(not exporting and self._has_target_summary_data())
        self.statistics_scope_combo.setEnabled(not exporting)
        self.statistics_group_combo.setEnabled(not exporting)
        self.statistics_timezone_combo.setEnabled(not exporting)
        self._sync_statistics_range_controls(exporting=exporting)
        if hasattr(self, "export_session_button"):
            self.export_session_button.setEnabled(not exporting and self.session_combo.count() > 0)
        if hasattr(self, "resume_session_button"):
            self.resume_session_button.setEnabled(
                not exporting
                and self.session_combo.count() > 0
                and not bool(self.worker and self.worker.isRunning())
            )
        if hasattr(self, "delete_session_button"):
            self.delete_session_button.setEnabled(
                not exporting
                and self.session_combo.count() > 0
                and not bool(self.worker and self.worker.isRunning())
            )
        self.start_button.setEnabled(not exporting and not bool(self.worker and self.worker.isRunning()))

    def _has_export_data(self) -> bool:
        return bool(
            self.observations
            or self.session_log_path
            or self.snapshots
            or self.target_snapshots
            or self.focus_observations
            or self.focus_snapshots
            or self.focus_target_snapshots
        )

    def _has_target_summary_data(self) -> bool:
        return bool(self._display_target_snapshots())

    def _set_state_chip(self, text: str, tone: str) -> None:
        self.session_state_label.setText(text)
        self.session_state_label.setProperty("tone", tone)
        self.session_state_label.style().unpolish(self.session_state_label)
        self.session_state_label.style().polish(self.session_state_label)

    def refresh_trace_targets(self) -> None:
        targets, invalid = parse_ipv4_targets(self.target_input.toPlainText())
        over_limit = len(targets) > MAX_IPV4_TARGETS
        self._apply_trace_targets(targets[:MAX_IPV4_TARGETS] if over_limit else targets)
        if invalid:
            self.status_label.setText(f"{IPV4_ONLY_MESSAGE} 제외: {', '.join(invalid[:5])}")
        elif over_limit:
            self.status_label.setText(f"IPv4 {len(targets)}개 입력됨. 시작 시 최대 {MAX_IPV4_TARGETS}개 사용 여부를 확인합니다.")
        else:
            self.status_label.setText(f"등록된 IPv4 {len(targets)}개")

    def _confirm_target_limit(self, targets: list[str]) -> list[str] | None:
        if len(targets) <= MAX_IPV4_TARGETS:
            return targets
        answer = QMessageBox.question(
            self,
            "대상 수 제한",
            f"IPv4 대상은 최대 {MAX_IPV4_TARGETS}개까지 측정합니다.\n"
            f"입력된 {len(targets)}개 중 처음 {MAX_IPV4_TARGETS}개만 사용할까요?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer != QMessageBox.Yes:
            self.status_label.setText("대상 수 초과로 측정을 시작하지 않았습니다.")
            return None
        return targets[:MAX_IPV4_TARGETS]

    def _apply_trace_targets(self, targets: list[str]) -> None:
        current = self.trace_target_combo.currentText()
        self.trace_target_combo.blockSignals(True)
        self.trace_target_combo.clear()
        self.trace_target_combo.addItems(targets)
        if current in targets:
            self.trace_target_combo.setCurrentText(current)
        self.trace_target_combo.blockSignals(False)

    def closeEvent(self, event) -> None:
        if self.worker and self.worker.isRunning():
            self.worker.request_stop()
            self.worker.wait(3000)
        if self.export_worker and self.export_worker.isRunning():
            self.export_worker.wait(3000)
        if self.graph_detail_window is not None:
            self.graph_detail_window.close()
        super().closeEvent(event)


def _panel(name: str) -> QFrame:
    panel = QFrame()
    panel.setObjectName(name)
    panel.setFrameShape(QFrame.NoFrame)
    return panel


def _field_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("fieldLabel")
    return label


def _chip(text: str, tone: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("chip")
    label.setProperty("tone", tone)
    label.setAlignment(Qt.AlignCenter)
    return label


def _format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds <= 172800:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _all_targets_summary_line(snapshots: list[MetricSnapshot]) -> str:
    if not snapshots:
        return "Targets: 0"
    statuses = [display_status(snapshot) for snapshot in snapshots]
    critical = statuses.count("CRITICAL")
    warning = statuses.count("WARNING")
    paused = statuses.count("PAUSED")
    ok = statuses.count("OK")
    other = len(snapshots) - critical - warning - paused - ok
    parts = [
        f"Targets: {len(snapshots)}",
        f"OK {ok}",
        f"Warning {warning}",
        f"Critical {critical}",
        f"Paused {paused}",
    ]
    if other:
        parts.append(f"Other {other}")
    worst_loss = max(snapshot.loss_percent for snapshot in snapshots)
    max_latency = max(
        (snapshot.current_latency_ms for snapshot in snapshots if snapshot.current_latency_ms is not None),
        default=None,
    )
    max_latency_text = f"{fmt_ms(max_latency)} ms" if max_latency is not None else "-"
    parts.extend([f"worst loss {worst_loss:.1f}%", f"max latency {max_latency_text}"])
    return " | ".join(parts)


def _apply_default_font() -> None:
    app = QApplication.instance()
    if app is None:
        return

    family = "Malgun Gothic"
    if family not in set(QFontDatabase.families()):
        font_path = Path("C:/Windows/Fonts/malgun.ttf")
        if font_path.exists():
            font_id = QFontDatabase.addApplicationFont(str(font_path))
            families = QFontDatabase.applicationFontFamilies(font_id)
            if families:
                family = families[0]
    app.setFont(QFont(family, 9))


def _parse_session_measurement_mode(value: str) -> tuple[str, str, int | None]:
    parts = [part for part in value.split(":") if part]
    mode = (
        parts[0]
        if parts and parts[0] in {MEASUREMENT_MODE_FULL_ROUTE, MEASUREMENT_MODE_FINAL_HOP_ONLY}
        else MEASUREMENT_MODE_FULL_ROUTE
    )
    probe_engine = PROBE_ENGINE_ICMP
    tcp_port: int | None = None
    for part in parts[1:]:
        if part in {PROBE_ENGINE_ICMP, PROBE_ENGINE_TCP_CONNECT}:
            probe_engine = part
        elif part.startswith("port"):
            try:
                tcp_port = int(part.removeprefix("port"))
            except ValueError:
                tcp_port = None
    return mode, probe_engine, tcp_port


def _alert_event_from_action_row(row: dict[str, str], index: int) -> AlertEvent | None:
    timestamp = _parse_iso_datetime(row.get("timestamp", ""))
    if timestamp is None:
        timestamp = _parse_iso_datetime(row.get("end", "")) or _parse_iso_datetime(row.get("start", ""))
    if timestamp is None:
        return None
    start = _parse_iso_datetime(row.get("start", "")) or timestamp
    end = _parse_iso_datetime(row.get("end", "")) or timestamp
    source = row.get("source", "") or "alert"
    title = row.get("title", "") or ("Route changed" if source == "route" else "Alert")
    message = row.get("message", "")
    severity = row.get("severity", "") or ("warning" if source == "route" else "info")
    key = _alert_event_key_from_action_row(source, timestamp, title, message, index)
    return AlertEvent(
        key=key,
        timestamp=timestamp,
        start=start,
        end=end,
        severity=severity,
        title=title,
        message=message,
        series_key=None if source == "route" else "target",
    )


def _alert_event_key_from_action_row(
    source: str,
    timestamp: datetime,
    title: str,
    message: str,
    index: int,
) -> str:
    if source == "route":
        return f"route_changed:{timestamp.isoformat(timespec='seconds')}"
    return f"saved_alert:{timestamp.isoformat(timespec='seconds')}:{index}:{title}:{message}"


def _parse_iso_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _qdatetime_from_datetime(value: datetime) -> QDateTime:
    return QDateTime(
        QDate(value.year, value.month, value.day),
        QTime(value.hour, value.minute, value.second),
    )


def _datetime_from_qdatetime(value: QDateTime) -> datetime:
    date = value.date()
    time_value = value.time()
    return datetime(
        date.year(),
        date.month(),
        date.day(),
        time_value.hour(),
        time_value.minute(),
        time_value.second(),
    )


APP_STYLE = """
QWidget {
    background: #f3f4f6;
    color: #111827;
    font-family: "Malgun Gothic", "Segoe UI", Arial, sans-serif;
    font-size: 12px;
}
QFrame#header,
QFrame#controls,
QFrame#metrics,
QFrame#tablePanel,
QFrame#graphPanel,
QFrame#rightPanel,
QFrame#footer {
    background: #ffffff;
    border: 1px solid #d9dee7;
    border-radius: 8px;
}
QFrame#metricBox {
    background: #ffffff;
    border: 1px solid #e5e7eb;
    border-radius: 6px;
}
QLabel#title {
    font-size: 19px;
    font-weight: 700;
    color: #111827;
}
QLabel#panelTitle {
    font-size: 15px;
    font-weight: 700;
    color: #111827;
}
QLabel#muted,
QLabel#statusText {
    color: #6b7280;
}
QLabel#mutedStrong {
    color: #4b5563;
    font-weight: 600;
}
QLabel#warningText {
    color: #92400e;
}
QLabel#fieldLabel,
QLabel#metricLabel {
    color: #6b7280;
    font-size: 11px;
    font-weight: 600;
}
QLabel#metricValue {
    color: #111827;
    font-size: 20px;
    font-weight: 700;
}
QLabel#chip {
    border-radius: 6px;
    padding: 5px 10px;
    font-weight: 700;
    min-width: 64px;
}
QLabel#chip[tone="neutral"] {
    background: #f3f4f6;
    color: #374151;
}
QLabel#chip[tone="active"] {
    background: #dbeafe;
    color: #1d4ed8;
}
QLabel#chip[tone="success"] {
    background: #dcfce7;
    color: #166534;
}
QLabel#chip[tone="warning"] {
    background: #fef3c7;
    color: #92400e;
}
QLabel#chip[tone="danger"] {
    background: #fee2e2;
    color: #991b1b;
}
QLineEdit,
QComboBox,
QSpinBox,
QTextEdit {
    background: #ffffff;
    border: 1px solid #d1d5db;
    border-radius: 6px;
    padding: 6px 8px;
}
QTextEdit#analysisBox,
QTextEdit#statesBox {
    background: #f9fafb;
}
QPushButton {
    background: #ffffff;
    border: 1px solid #d1d5db;
    border-radius: 6px;
    padding: 7px 12px;
    font-weight: 700;
}
QPushButton#primaryButton {
    background: #2563eb;
    border-color: #2563eb;
    color: #ffffff;
}
QPushButton#dangerButton {
    color: #b91c1c;
    border-color: #fecaca;
}
QPushButton:disabled {
    color: #9ca3af;
    background: #f3f4f6;
}
QTableWidget#hopTable {
    background: #ffffff;
    alternate-background-color: #fbfdff;
    border: 1px solid #e5e7eb;
    gridline-color: #eef2f7;
}
QHeaderView::section {
    background: #f3f4f6;
    color: #374151;
    border: 0;
    border-right: 1px solid #e5e7eb;
    padding: 6px;
    font-weight: 700;
}
"""
