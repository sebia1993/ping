from __future__ import annotations

import csv
import inspect
import io
import json
import os
import smtplib
import subprocess
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

from PySide6.QtCore import QDate, QDateTime, Qt, QTime, QTimer
from PySide6.QtGui import QFont, QFontDatabase, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.core.alerts import (
    AlertEvent,
    AlertRuleConfig,
    alert_recovery_event,
    evaluate_route_ip_alert,
    evaluate_target_alerts,
    is_route_alert_key,
    route_change_alert,
)
from app.core.analyzer import analyze_path
from app.core.models import HopObservation, MetricSnapshot
from app.core.observation_stats import build_focus_snapshots, observations_in_range
from app.core.route_history import RouteChange, route_path
from app.ui.control_panel import build_controls_panel
from app.ui.export_worker import ExportWorker
from app.ui.graph_detail_window import GraphDetailWindow
from app.ui.latency_graph import LatencyGraphWidget, TimelineAnnotation
from app.ui.table_panels import (
    ALERT_HEADERS,
    SESSION_HEADERS,
    SESSION_ID_ROLE,
    TABLE_HEADERS,
    TARGET_HEADERS,
    TARGET_SCORE_COLUMN,
    create_alert_table,
    create_hop_table,
    create_session_table,
    create_target_table,
    display_status,
    fmt_ms,
    populate_trace_table,
    target_problem_score,
    update_alert_table,
    update_hop_table,
    update_session_table,
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
from app.storage.session_index import (
    SessionIndexStore,
    SessionStorageBucket,
    TraceSessionRecord,
    session_data_paths,
    session_index_root_for_sample_path,
    session_storage_buckets,
    session_storage_summary,
)
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
SESSION_MANAGER_DISPLAY_LIMIT = 100
ALERT_REST_TIMEOUT_SECONDS = 3.0
ALERT_EMAIL_TIMEOUT_SECONDS = 5.0
DEFAULT_ALERT_EMAIL_FROM = "network-path-diagnostics@localhost"
ALERT_EMAIL_SECURITY_PLAIN = "plain"
ALERT_EMAIL_SECURITY_STARTTLS = "starttls"
ALERT_EMAIL_SECURITY_SSL = "ssl"
ALERT_EMAIL_SECURITY_MODES = {
    ALERT_EMAIL_SECURITY_PLAIN,
    ALERT_EMAIL_SECURITY_STARTTLS,
    ALERT_EMAIL_SECURITY_SSL,
}
SIMPLE_DEFAULT_INTERVAL_SECONDS = 1
SIMPLE_DEFAULT_TCP_PORT = 443
GRAPH_PNG_SCOPE_TIMELINE = "timeline"
GRAPH_PNG_SCOPE_TRACE = "trace"
GRAPH_PNG_SCOPE_BOTH = "both"
TARGET_GROUP_PRESET_VERSION = 2
ALERT_RULE_PRESET_VERSION = 2
GRAPH_RENDER_THROTTLE_TARGET_COUNT = 5
GRAPH_RENDER_THROTTLE_SECONDS = 2.0


@dataclass(frozen=True)
class AlertEmailConfig:
    host: str
    port: int
    sender: str
    recipient: str
    security: str = ALERT_EMAIL_SECURITY_PLAIN
    username: str = ""
    password_env: str = ""


class MainWindow(QMainWindow):
    """Windows GUI의 중심 컨트롤러입니다.

    이 클래스는 직접 ping을 하지 않습니다. 사용자가 누른 버튼과 입력값을 받아
    MeasurementWorker를 만들고, Worker가 보내는 결과를 표/그래프/세션/알림 화면에 반영합니다.
    """

    def __init__(self, worker_factory=None) -> None:
        super().__init__()
        _apply_default_font()
        self.setWindowTitle("네트워크 경로 진단")
        self.resize(1440, 900)

        self.worker: MeasurementWorker | None = None
        self.export_worker: ExportWorker | None = None
        self.graph_detail_window: GraphDetailWindow | None = None
        self.advanced_features_visible = False
        self.worker_factory = worker_factory or MeasurementWorker
        self.session_index_store = SessionIndexStore.create()

        # 현재 실행 중인 측정 대상과 저장 파일 위치입니다.
        # 세션이 시작되면 Worker가 CSV 경로를 알려 주고, 그때 export/복구 버튼이 활성화됩니다.
        self.current_target = ""
        self.current_targets: list[str] = []
        self.target_interval_overrides: dict[str, int] = {}
        self.pending_resume_session_id = ""
        self.pending_resume_targets: list[str] = []
        self.session_log_path: Path | None = None
        self.route_log_path: Path | None = None
        self.alert_action_log_path: Path | None = None

        # 라이브 측정 화면에 표시되는 최신 hop/target/분석 데이터입니다.
        self.snapshots: list[MetricSnapshot] = []
        self.target_snapshot: MetricSnapshot | None = None
        self.target_snapshots: list[MetricSnapshot] = []
        self.observations: list[HopObservation] = []
        self.target_history: list[HopObservation] = []
        self.selected_hop_index: int | None = None
        self.analysis: list[str] = []

        # focus_range는 사용자가 장애가 난 시간대만 좁혀 보는 기능입니다.
        # 원본 데이터는 유지하고, 아래 focus_* 값만 다시 계산해서 화면에 보여줍니다.
        self.focus_range: tuple[datetime, datetime] | None = None
        self.focus_observations: list[HopObservation] = []
        self.focus_snapshots: list[MetricSnapshot] = []
        self.focus_target_snapshot: MetricSnapshot | None = None
        self.focus_target_snapshots: list[MetricSnapshot] = []
        self.focus_analysis: list[str] = []

        # timeline_range는 전체 세션 중 그래프에 보이는 구간을 뜻합니다.
        # export 옵션의 "Visible timeline"도 이 값을 기준으로 동작합니다.
        self.timeline_range: tuple[datetime, datetime] | None = None
        self.timeline_observations: list[HopObservation] = []
        self.timeline_target_history: list[HopObservation] = []
        self.timeline_snapshots: list[MetricSnapshot] = []
        self.timeline_target_snapshot: MetricSnapshot | None = None
        self.timeline_status = "Timeline source: live buffer"

        # 알림과 경로 변경은 화면 표시뿐 아니라 export/report에 같이 쓰입니다.
        self.route_changes: list[RouteChange] = []
        self.alert_events: list[AlertEvent] = []
        self.alert_event_actions: dict[str, list[str]] = {}
        self.pending_alert_image_keys: set[str] = set()
        self.active_alert_keys: set[str] = set()
        self.metric_value_labels: dict[str, QLabel] = {}
        self.primary_graph_address: str | None = None
        self.target_graph_rows: dict[str, QFrame] = {}
        self.target_graph_widgets: dict[str, LatencyGraphWidget] = {}
        self.target_graph_title_labels: dict[str, QLabel] = {}
        self.target_graph_metric_labels: dict[str, QLabel] = {}
        self.target_graph_render_keys: dict[str, tuple[object, ...]] = {}
        self._last_graph_render_monotonic = 0.0
        self._pending_graph_render = False
        self._graph_render_timer = QTimer(self)
        self._graph_render_timer.setSingleShot(True)
        self._graph_render_timer.timeout.connect(self._render_pending_graph)
        self._syncing_session_selection = False

        self._build_ui()
        self._build_menu_bar()
        self._set_running(False)
        self.set_advanced_features_visible(False)
        self._set_state_chip("대기", "neutral")
        self._update_target_summary(None)
        self._sync_sessions_box()

    def _build_ui(self) -> None:
        """상단 입력, 왼쪽 그래프/표, 오른쪽 세션/알림 영역을 조립합니다."""

        central = QWidget(self)
        central.setStyleSheet(APP_STYLE)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        root.addWidget(self._build_controls())
        root.addWidget(self._build_main_area(), 1)
        self.footer_panel = self._build_footer()
        root.addWidget(self.footer_panel)

        self.setCentralWidget(central)

    def _build_menu_bar(self) -> None:
        # 현재 기본 화면은 IP 입력과 실시간 그래프만 노출합니다.
        # GraphDetailWindow 코드는 내부 검증/export 경로에서 계속 쓰이지만, 사용자 메뉴 진입점은 만들지 않습니다.
        return

    def _build_header(self) -> QFrame:
        header = _panel("header")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(12)

        title_group = QVBoxLayout()
        title_group.setSpacing(2)
        title = QLabel("네트워크 경로 진단")
        title.setObjectName("title")
        subtitle = QLabel("IP 입력 후 현재 상태와 실시간 지연 그래프를 확인합니다.")
        subtitle.setObjectName("muted")
        title_group.addWidget(title)
        title_group.addWidget(subtitle)

        layout.addLayout(title_group, 1)

        self.session_state_label = _chip("대기", "neutral")
        layout.addWidget(self.session_state_label)
        return header

    def _build_controls(self) -> QFrame:
        return build_controls_panel(self, _panel, _field_label)

    def _build_main_area(self) -> QSplitter:
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        self.main_splitter = splitter
        splitter.addWidget(self._build_left_work_area())
        self.right_panel = self._build_right_panel()
        splitter.addWidget(self.right_panel)
        splitter.setSizes([960, 420])
        return splitter

    def _build_left_work_area(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.metrics_strip_panel = self._build_metrics_strip()
        layout.addWidget(self.metrics_strip_panel)
        self._build_target_table_panel()

        self.table = create_hop_table()
        self.table.itemSelectionChanged.connect(self.on_hop_selection_changed)
        self.table.cellDoubleClicked.connect(self.on_hop_double_clicked)

        self.hop_table_panel = _panel("tablePanel")
        table_layout = QVBoxLayout(self.hop_table_panel)
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
        layout.addWidget(self.hop_table_panel, 1)

        graph_panel = _panel("graphPanel")
        graph_layout = QVBoxLayout(graph_panel)
        graph_layout.setContentsMargins(12, 10, 12, 12)
        graph_layout.setSpacing(8)
        graph_header = QHBoxLayout()
        graph_title = QLabel("실시간 그래프")
        graph_title.setObjectName("panelTitle")
        graph_hint = QLabel("측정 중인 모든 IP를 대상별 그래프 행으로 표시합니다.")
        graph_hint.setObjectName("muted")
        self.focus_label = _chip("Live", "neutral")
        self.timeline_label = _chip("Timeline: Live", "neutral")
        self.timeline_range_combo = QComboBox()
        self.timeline_range_combo.setToolTip("Visible timeline range")
        for label, seconds in [
            ("60s", 60),
            ("10m", 600),
            ("1h", 3600),
            ("6h", 21600),
            ("24h", 86400),
            ("48h", 172800),
        ]:
            self.timeline_range_combo.addItem(label, seconds)
        self.timeline_range_combo.setCurrentIndex(self.timeline_range_combo.findData(600))
        self.load_timeline_button = QPushButton("범위 불러오기")
        self.load_timeline_button.setToolTip("선택한 시간 범위를 메인 그래프에 표시합니다.")
        self.load_timeline_button.clicked.connect(self.load_selected_timeline_range)
        self.reset_current_button = QPushButton("현재")
        self.reset_current_button.setToolTip("포커스와 그래프 범위를 최신 측정으로 되돌립니다.")
        self.reset_current_button.clicked.connect(self.reset_focus_to_current)
        self.clear_focus_button = QPushButton("포커스 해제")
        self.clear_focus_button.setEnabled(False)
        self.clear_focus_button.clicked.connect(self.clear_focus_range)
        self.graph_advanced_controls = QWidget()
        graph_advanced_layout = QHBoxLayout(self.graph_advanced_controls)
        graph_advanced_layout.setContentsMargins(0, 0, 0, 0)
        graph_advanced_layout.setSpacing(6)
        graph_advanced_layout.addWidget(graph_hint)
        graph_advanced_layout.addWidget(self.focus_label)
        graph_advanced_layout.addWidget(self.timeline_label)
        graph_advanced_layout.addWidget(self.timeline_range_combo)
        graph_advanced_layout.addWidget(self.load_timeline_button)
        graph_advanced_layout.addWidget(self.reset_current_button)
        graph_advanced_layout.addWidget(self.clear_focus_button)
        graph_header.addWidget(graph_title)
        graph_header.addStretch(1)
        graph_header.addWidget(self.target_summary_status_label)
        graph_header.addWidget(self.graph_advanced_controls)
        self.graph = LatencyGraphWidget()
        self.graph.setMinimumHeight(112)
        self.target_graph_scroll = QScrollArea()
        self.target_graph_scroll.setWidgetResizable(True)
        self.target_graph_scroll.setFrameShape(QFrame.NoFrame)
        self.target_graph_container = QWidget()
        self.target_graph_container.setObjectName("targetGraphContainer")
        self.target_graph_layout = QVBoxLayout(self.target_graph_container)
        self.target_graph_layout.setContentsMargins(0, 0, 0, 0)
        self.target_graph_layout.setSpacing(8)
        self.target_graph_empty_label = QLabel("측정을 시작하면 IP별 그래프가 여기에 표시됩니다.")
        self.target_graph_empty_label.setObjectName("targetGraphEmpty")
        self.target_graph_empty_label.setAlignment(Qt.AlignCenter)
        self.target_graph_layout.addWidget(self.target_graph_empty_label, 1)
        self.target_graph_scroll.setWidget(self.target_graph_container)
        graph_layout.addLayout(graph_header)
        graph_layout.addWidget(self.target_graph_scroll, 1)
        layout.addWidget(graph_panel, 8)

        return container

    def _build_target_table_panel(self) -> QFrame:
        self.target_table = create_target_table()
        self.target_table.cellDoubleClicked.connect(self.on_target_double_clicked)
        self.target_table.itemSelectionChanged.connect(self._refresh_target_summary_selection)

        panel = QFrame(self)
        panel.setObjectName("targetPanelInline")
        self.target_table_panel = panel
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 8)
        layout.setSpacing(6)
        self.target_summary_status_label = QLabel("IP: 0")
        self.target_summary_status_label.setObjectName("muted")
        self.target_filter_edit = QLineEdit()
        self.target_filter_edit.setPlaceholderText("IP 필터")
        self.target_filter_edit.setClearButtonEnabled(True)
        self.target_filter_edit.setMinimumWidth(160)
        self.target_filter_edit.textChanged.connect(lambda *_args: self._sync_target_filter())
        self.target_status_filter_combo = QComboBox()
        self.target_status_filter_combo.addItem("전체 상태", "")
        self.target_status_filter_combo.addItem("문제", "problem")
        self.target_status_filter_combo.addItem("장애", "CRITICAL")
        self.target_status_filter_combo.addItem("주의", "WARNING")
        self.target_status_filter_combo.addItem("OK", "OK")
        self.target_status_filter_combo.addItem("중지", "PAUSED")
        self.target_status_filter_combo.currentIndexChanged.connect(lambda *_args: self._sync_target_filter())
        self.pause_selected_targets_button = QPushButton("선택 중지")
        self.pause_selected_targets_button.clicked.connect(self.pause_selected_targets)
        self.resume_selected_targets_button = QPushButton("선택 재개")
        self.resume_selected_targets_button.clicked.connect(self.resume_selected_targets)
        self.pause_visible_targets_button = QPushButton("표시 중지")
        self.pause_visible_targets_button.clicked.connect(self.pause_visible_targets)
        self.resume_visible_targets_button = QPushButton("표시 재개")
        self.resume_visible_targets_button.clicked.connect(self.resume_visible_targets)
        self.pause_problem_targets_button = QPushButton("문제 중지")
        self.pause_problem_targets_button.clicked.connect(self.pause_problem_targets)
        self.resume_problem_targets_button = QPushButton("문제 재개")
        self.resume_problem_targets_button.clicked.connect(self.resume_problem_targets)
        self.pause_all_targets_button = QPushButton("전체 중지")
        self.pause_all_targets_button.clicked.connect(self.pause_all_targets)
        self.resume_all_targets_button = QPushButton("전체 재개")
        self.resume_all_targets_button.clicked.connect(self.resume_all_targets)
        self.apply_interval_button = QPushButton("주기 적용")
        self.apply_interval_button.clicked.connect(self.apply_runtime_interval)
        self.apply_visible_interval_button = QPushButton("표시 적용")
        self.apply_visible_interval_button.clicked.connect(self.apply_visible_interval)
        self.apply_problem_interval_button = QPushButton("문제 적용")
        self.apply_problem_interval_button.clicked.connect(self.apply_problem_interval)
        self.export_target_summary_button = QPushButton("현황 내보내기")
        self.export_target_summary_button.clicked.connect(self.save_target_summary_csv)
        self.problem_sort_check = QCheckBox("문제 우선")
        self.problem_sort_check.toggled.connect(self._on_problem_sort_toggled)
        controls = QHBoxLayout()
        controls.setSpacing(6)
        controls.addWidget(self.target_filter_edit)
        controls.addWidget(self.target_status_filter_combo)
        controls.addStretch(1)
        controls.addWidget(self.pause_selected_targets_button)
        controls.addWidget(self.resume_selected_targets_button)
        controls.addWidget(self.pause_visible_targets_button)
        controls.addWidget(self.resume_visible_targets_button)
        controls.addWidget(self.pause_problem_targets_button)
        controls.addWidget(self.resume_problem_targets_button)
        controls.addWidget(self.pause_all_targets_button)
        controls.addWidget(self.resume_all_targets_button)
        controls.addWidget(self.apply_interval_button)
        controls.addWidget(self.apply_visible_interval_button)
        controls.addWidget(self.apply_problem_interval_button)
        controls.addWidget(self.problem_sort_check)
        controls.addWidget(self.export_target_summary_button)
        self.target_advanced_controls_panel = QWidget()
        self.target_advanced_controls_panel.setLayout(controls)
        layout.addWidget(self.target_advanced_controls_panel)
        self.target_table.setMaximumHeight(180)
        layout.addWidget(self.target_table)
        self._apply_simple_target_columns()
        self.target_advanced_controls_panel.setVisible(False)
        self.target_table.setVisible(False)
        self.target_table_panel.setVisible(False)
        return panel

    def _apply_simple_target_columns(self) -> None:
        if not hasattr(self, "target_table"):
            return
        visible_columns = {0, 1, 2, 6, 7}
        for column in range(self.target_table.columnCount()):
            self.target_table.setColumnHidden(column, column not in visible_columns)

    def _sync_target_columns_for_mode(self) -> None:
        if not hasattr(self, "target_table"):
            return
        self._apply_simple_target_columns()

    def _build_metrics_strip(self) -> QFrame:
        strip = _panel("metrics")
        layout = QHBoxLayout(strip)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)

        for key, label in [
            ("current", "현재 지연"),
            ("avg", "평균 지연"),
            ("loss", "손실률"),
            ("jitter", "지터"),
            ("samples", "샘플"),
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

        analysis_title = QLabel("분석 요약")
        analysis_title.setObjectName("panelTitle")
        self.analysis_box = QTextEdit()
        self.analysis_box.setReadOnly(True)
        self.analysis_box.setMinimumHeight(180)
        self.analysis_box.setObjectName("analysisBox")

        states_title = QLabel("상태 기준")
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

        diagnostics_title = QLabel("측정 엔진 상태")
        diagnostics_title.setObjectName("panelTitle")
        self.diagnostics_box = QTextEdit()
        self.diagnostics_box.setReadOnly(True)
        self.diagnostics_box.setMaximumHeight(150)
        self.diagnostics_box.setObjectName("statesBox")
        self.diagnostics_box.setPlainText("측정 시작 후 엔진 상태가 표시됩니다.")

        alerts_title = QLabel("알림 이벤트")
        alerts_title.setObjectName("panelTitle")
        alert_rule_row = QHBoxLayout()
        alert_rule_row.setSpacing(6)
        self.loss_alert_check = QCheckBox("손실")
        self.loss_alert_check.setChecked(True)
        self.loss_threshold_spin = QSpinBox()
        self.loss_threshold_spin.setRange(1, 100)
        self.loss_threshold_spin.setValue(20)
        self.loss_threshold_spin.setSuffix("%")
        self.loss_window_spin = QSpinBox()
        self.loss_window_spin.setRange(1, 60)
        self.loss_window_spin.setValue(3)
        self.loss_window_spin.setSuffix("m")
        self.latency_alert_check = QCheckBox("지연")
        self.latency_alert_check.setChecked(True)
        self.latency_threshold_spin = QSpinBox()
        self.latency_threshold_spin.setRange(1, 5000)
        self.latency_threshold_spin.setValue(100)
        self.latency_threshold_spin.setSuffix("ms")
        self.jitter_alert_check = QCheckBox("지터")
        self.jitter_alert_check.setChecked(True)
        self.jitter_threshold_spin = QSpinBox()
        self.jitter_threshold_spin.setRange(1, 1000)
        self.jitter_threshold_spin.setValue(30)
        self.jitter_threshold_spin.setSuffix("ms")
        self.mos_alert_check = QCheckBox("MOS")
        self.mos_alert_check.setToolTip("Alert when estimated voice quality drops below the threshold")
        self.mos_threshold_spin = QDoubleSpinBox()
        self.mos_threshold_spin.setRange(1.0, 4.5)
        self.mos_threshold_spin.setDecimals(1)
        self.mos_threshold_spin.setSingleStep(0.1)
        self.mos_threshold_spin.setValue(3.5)
        self.mos_window_spin = QSpinBox()
        self.mos_window_spin.setRange(1, 240)
        self.mos_window_spin.setValue(5)
        self.mos_window_spin.setSuffix("m")
        self.route_ip_alert_check = QCheckBox("경로 IP")
        self.route_ip_alert_check.setToolTip("Alert when the watched IPv4 address appears in the current route")
        self.route_ip_alert_edit = QLineEdit()
        self.route_ip_alert_edit.setPlaceholderText("192.0.2.1")
        self.route_ip_alert_edit.setMinimumWidth(105)
        self.sample_alert_check = QCheckBox("샘플")
        self.sample_alert_check.setChecked(True)
        self.sample_window_spin = QSpinBox()
        self.sample_window_spin.setRange(1, 100)
        self.sample_window_spin.setValue(10)
        self.sample_window_spin.setSuffix("개 샘플")
        self.sample_bad_spin = QSpinBox()
        self.sample_bad_spin.setRange(1, 100)
        self.sample_bad_spin.setValue(10)
        self.sample_bad_spin.setSuffix("개 불량")
        self.timer_alert_check = QCheckBox("지속시간")
        self.timer_alert_check.setChecked(True)
        self.timer_window_spin = QSpinBox()
        self.timer_window_spin.setRange(1, 240)
        self.timer_window_spin.setValue(5)
        self.timer_window_spin.setSuffix("m")
        self.alert_start_action_check = QCheckBox("시작")
        self.alert_start_action_check.setChecked(True)
        self.alert_start_action_check.setToolTip("Run selected actions when a new alert starts")
        self.alert_end_action_check = QCheckBox("종료")
        self.alert_end_action_check.setChecked(True)
        self.alert_end_action_check.setToolTip("Run selected actions when an active alert recovers")
        self.alert_route_adjust_action_check = QCheckBox("경로 조정")
        self.alert_route_adjust_action_check.setChecked(False)
        self.alert_route_adjust_action_check.setToolTip(
            "When Final Hop Only is active, switch to Full Route on matching target alerts"
        )
        self.alert_timeline_action_check = QCheckBox("타임라인")
        self.alert_timeline_action_check.setChecked(True)
        self.alert_comment_action_check = QCheckBox("코멘트")
        self.alert_comment_action_check.setChecked(True)
        self.alert_log_action_check = QCheckBox("로그")
        self.alert_beep_action_check = QCheckBox("소리")
        self.alert_image_action_check = QCheckBox("이미지")
        self.alert_email_action_check = QCheckBox("이메일")
        self.alert_email_server_edit = QLineEdit()
        self.alert_email_server_edit.setPlaceholderText("smtp.example:25")
        self.alert_email_server_edit.setMinimumWidth(130)
        self.alert_email_to_edit = QLineEdit()
        self.alert_email_to_edit.setPlaceholderText("to@example.com")
        self.alert_email_to_edit.setMinimumWidth(140)
        self.alert_email_from_edit = QLineEdit()
        self.alert_email_from_edit.setPlaceholderText("from@example.com")
        self.alert_email_from_edit.setMinimumWidth(140)
        self.alert_email_security_combo = QComboBox()
        self.alert_email_security_combo.addItem("Plain", ALERT_EMAIL_SECURITY_PLAIN)
        self.alert_email_security_combo.addItem("STARTTLS", ALERT_EMAIL_SECURITY_STARTTLS)
        self.alert_email_security_combo.addItem("SSL", ALERT_EMAIL_SECURITY_SSL)
        self.alert_email_user_edit = QLineEdit()
        self.alert_email_user_edit.setPlaceholderText("SMTP 사용자")
        self.alert_email_user_edit.setMinimumWidth(100)
        self.alert_email_password_env_edit = QLineEdit()
        self.alert_email_password_env_edit.setPlaceholderText("비밀번호 환경변수")
        self.alert_email_password_env_edit.setMinimumWidth(105)
        self.alert_rest_action_check = QCheckBox("REST")
        self.alert_rest_url_edit = QLineEdit()
        self.alert_rest_url_edit.setPlaceholderText("https://example/api/alert")
        self.alert_rest_url_edit.setMinimumWidth(170)
        self.alert_executable_action_check = QCheckBox("실행")
        self.alert_executable_path_edit = QLineEdit()
        self.alert_executable_path_edit.setPlaceholderText("C:\\path\\action.exe")
        self.alert_executable_path_edit.setMinimumWidth(165)
        self.save_alert_preset_button = QPushButton("프리셋 저장")
        self.save_alert_preset_button.clicked.connect(self.save_alert_rule_preset)
        self.load_alert_preset_button = QPushButton("프리셋 불러오기")
        self.load_alert_preset_button.clicked.connect(self.load_alert_rule_preset)
        for checkbox, fields in [
            (self.loss_alert_check, [self.loss_threshold_spin, self.loss_window_spin]),
            (self.latency_alert_check, [self.latency_threshold_spin]),
            (self.jitter_alert_check, [self.jitter_threshold_spin]),
            (self.sample_alert_check, [self.sample_window_spin, self.sample_bad_spin]),
            (self.timer_alert_check, [self.timer_window_spin]),
        ]:
            alert_rule_row.addWidget(checkbox)
            for field in fields:
                alert_rule_row.addWidget(field)
        alert_rule_row.addWidget(self.mos_alert_check)
        alert_rule_row.addWidget(QLabel("<"))
        alert_rule_row.addWidget(self.mos_threshold_spin)
        alert_rule_row.addWidget(QLabel("MOS Window"))
        alert_rule_row.addWidget(self.mos_window_spin)
        alert_rule_row.addWidget(self.route_ip_alert_check)
        alert_rule_row.addWidget(self.route_ip_alert_edit)
        alert_rule_row.addWidget(QLabel("동작"))
        alert_rule_row.addWidget(self.alert_start_action_check)
        alert_rule_row.addWidget(self.alert_end_action_check)
        alert_rule_row.addWidget(self.alert_route_adjust_action_check)
        alert_rule_row.addWidget(self.alert_timeline_action_check)
        alert_rule_row.addWidget(self.alert_comment_action_check)
        alert_rule_row.addWidget(self.alert_log_action_check)
        alert_rule_row.addWidget(self.alert_beep_action_check)
        alert_rule_row.addWidget(self.alert_image_action_check)
        alert_rule_row.addWidget(self.alert_email_action_check)
        alert_rule_row.addWidget(self.alert_email_server_edit)
        alert_rule_row.addWidget(self.alert_email_to_edit)
        alert_rule_row.addWidget(self.alert_email_from_edit)
        alert_rule_row.addWidget(self.alert_email_security_combo)
        alert_rule_row.addWidget(self.alert_email_user_edit)
        alert_rule_row.addWidget(self.alert_email_password_env_edit)
        alert_rule_row.addWidget(self.alert_rest_action_check)
        alert_rule_row.addWidget(self.alert_rest_url_edit)
        alert_rule_row.addWidget(self.alert_executable_action_check)
        alert_rule_row.addWidget(self.alert_executable_path_edit)
        alert_rule_row.addWidget(self.save_alert_preset_button)
        alert_rule_row.addWidget(self.load_alert_preset_button)
        alert_rule_row.addStretch(1)
        self.alert_table = create_alert_table()
        self.alerts_box = QTextEdit()
        self.alerts_box.setReadOnly(True)
        self.alerts_box.setMaximumHeight(120)
        self.alerts_box.setObjectName("statesBox")
        self.alerts_box.setPlainText("No alert events.")

        route_title = QLabel("경로 변경")
        route_title.setObjectName("panelTitle")
        self.route_changes_box = QTextEdit()
        self.route_changes_box.setReadOnly(True)
        self.route_changes_box.setMaximumHeight(130)
        self.route_changes_box.setObjectName("statesBox")
        self.route_changes_box.setPlainText("감지된 경로 변경이 없습니다.")

        sessions_title = QLabel("저장된 세션")
        sessions_title.setObjectName("panelTitle")
        sessions_header = QHBoxLayout()
        self.session_combo = QComboBox()
        self.session_combo.setMinimumWidth(170)
        self.session_filter_edit = QLineEdit()
        self.session_filter_edit.setPlaceholderText("세션 필터 예: month:2026-01")
        self.session_filter_edit.setClearButtonEnabled(True)
        self.session_filter_edit.setMinimumWidth(160)
        self.session_filter_edit.textChanged.connect(lambda *_args: self._sync_sessions_box())
        self.open_session_button = QPushButton("열기")
        self.open_session_button.clicked.connect(self.open_selected_session)
        self.resume_session_button = QPushButton("재개")
        self.resume_session_button.clicked.connect(self.resume_selected_session)
        self.export_session_button = QPushButton("내보내기")
        self.export_session_button.clicked.connect(self.export_selected_session)
        self.export_visible_sessions_button = QPushButton("표시 세션 내보내기")
        self.export_visible_sessions_button.clicked.connect(self.export_visible_sessions)
        self.delete_session_button = QPushButton("삭제")
        self.delete_session_button.clicked.connect(self.delete_selected_session)
        self.refresh_sessions_button = QPushButton("새로고침")
        self.refresh_sessions_button.clicked.connect(self.refresh_saved_sessions)
        self.session_retention_days_spin = QSpinBox()
        self.session_retention_days_spin.setRange(1, 3650)
        self.session_retention_days_spin.setValue(90)
        self.session_retention_days_spin.setSuffix("d")
        self.prune_sessions_button = QPushButton("오래된 세션 정리")
        self.prune_sessions_button.clicked.connect(self.prune_old_sessions)
        sessions_header.addWidget(sessions_title)
        sessions_header.addWidget(self.session_filter_edit)
        sessions_header.addStretch(1)
        sessions_header.addWidget(self.session_combo)
        sessions_header.addWidget(self.open_session_button)
        sessions_header.addWidget(self.resume_session_button)
        sessions_header.addWidget(self.export_session_button)
        sessions_header.addWidget(self.export_visible_sessions_button)
        sessions_header.addWidget(self.delete_session_button)
        sessions_header.addWidget(self.refresh_sessions_button)
        sessions_header.addWidget(QLabel("보관"))
        sessions_header.addWidget(self.session_retention_days_spin)
        sessions_header.addWidget(self.prune_sessions_button)
        self.session_combo.currentIndexChanged.connect(lambda *_args: self._select_session_table_row())
        self.session_table = create_session_table()
        self.session_table.itemSelectionChanged.connect(self.on_session_table_selection_changed)
        self.session_table.cellDoubleClicked.connect(lambda *_args: self.open_selected_session())
        self.sessions_box = QTextEdit()
        self.sessions_box.setReadOnly(True)
        self.sessions_box.setMaximumHeight(118)
        self.sessions_box.setObjectName("statesBox")
        self.sessions_box.setPlainText("저장된 세션이 없습니다.")

        export_title = QLabel("내보내기")
        export_title.setObjectName("panelTitle")
        export_row = QHBoxLayout()
        export_row.setSpacing(8)
        for button in (self.csv_button, self.xlsx_button, self.report_button):
            export_row.addWidget(button)
        self.graph_png_scope_combo = QComboBox()
        self.graph_png_scope_combo.addItem("실시간 그래프", GRAPH_PNG_SCOPE_TIMELINE)
        self.graph_png_scope_combo.addItem("경로 표", GRAPH_PNG_SCOPE_TRACE)
        self.graph_png_scope_combo.addItem("둘 다", GRAPH_PNG_SCOPE_BOTH)
        export_row.addWidget(QLabel("PNG 범위"))
        export_row.addWidget(self.graph_png_scope_combo)
        export_row.addWidget(self.graph_png_button)
        export_row.addStretch(1)

        self.statistics_group_combo = QComboBox()
        self.statistics_group_combo.addItem("5m", 300)
        self.statistics_group_combo.addItem("1h", 3600)
        self.statistics_group_combo.addItem("1d", 86400)
        self.statistics_group_combo.addItem("1w", 604800)
        self.statistics_timezone_combo = QComboBox()
        self.statistics_timezone_combo.addItem("로컬", TIMEZONE_LOCAL)
        self.statistics_timezone_combo.addItem("UTC", TIMEZONE_UTC)
        self.statistics_scope_combo = QComboBox()
        self.statistics_scope_combo.addItem("전체 기간", STATISTICS_SCOPE_ALL)
        self.statistics_scope_combo.addItem("보이는 그래프", STATISTICS_SCOPE_VISIBLE)
        self.statistics_scope_combo.addItem("포커스 기간", STATISTICS_SCOPE_FOCUS)
        self.statistics_scope_combo.addItem("직접 지정", STATISTICS_SCOPE_CUSTOM)
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
        statistics_export_row.addWidget(QLabel("통계 범위"))
        statistics_export_row.addWidget(self.statistics_scope_combo)
        statistics_export_row.addWidget(QLabel("시작"))
        statistics_export_row.addWidget(self.statistics_start_edit)
        statistics_export_row.addWidget(QLabel("종료"))
        statistics_export_row.addWidget(self.statistics_end_edit)
        statistics_export_row.addWidget(QLabel("통계 묶음"))
        statistics_export_row.addWidget(self.statistics_group_combo)
        statistics_export_row.addWidget(QLabel("시간대"))
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
        layout.addWidget(self.alert_table)
        layout.addWidget(self.alerts_box)
        layout.addWidget(route_title)
        layout.addWidget(self.route_changes_box)
        layout.addLayout(sessions_header)
        layout.addWidget(self.session_table)
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
        runtime = QLabel("측정 작업은 시작 후에만 실행되며 Windows 콘솔 창은 숨김 처리됩니다.")
        runtime.setObjectName("muted")
        safety = QLabel("명시적으로 요청하지 않는 한 민감한 원본 출력은 내보내지 않습니다.")
        safety.setObjectName("mutedStrong")
        layout.addWidget(runtime)
        layout.addStretch(1)
        layout.addWidget(safety)
        return footer

    def set_advanced_features_visible(self, visible: bool) -> None:
        self.advanced_features_visible = False
        for name in (
            "advanced_controls_panel",
            "target_advanced_controls_panel",
            "metrics_strip_panel",
            "hop_table_panel",
            "right_panel",
            "graph_advanced_controls",
            "footer_panel",
        ):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.setVisible(False)
        if hasattr(self, "main_splitter"):
            self.main_splitter.setSizes([1, 0])
        self._sync_target_columns_for_mode()
        if hasattr(self, "target_table_panel"):
            self.target_table_panel.setVisible(False)
        if hasattr(self, "target_table"):
            self.target_table.setVisible(False)
        if hasattr(self, "target_advanced_controls_panel"):
            self.target_advanced_controls_panel.setVisible(False)

    def save_target_group_preset(self) -> None:
        targets, invalid = parse_ipv4_targets(self.target_input.toPlainText())
        if invalid:
            QMessageBox.warning(self, "Target group", f"{IPV4_ONLY_MESSAGE}\n\n제외된 입력: {', '.join(invalid[:8])}")
            return
        if not targets:
            QMessageBox.warning(self, "Target group", "저장할 대상 IPv4 주소를 입력하세요.")
            return
        self._save_target_group_preset(targets, source="all")

    def save_selected_target_group_preset(self) -> None:
        targets = self._selected_target_addresses()
        if not targets:
            self.status_label.setText("No selected targets to save")
            return
        self._save_target_group_preset(targets, source="selected")

    def _save_target_group_preset(self, targets: list[str], *, source: str) -> None:
        path = self._select_save_path("target_group.json", "JSON Files (*.json)", target="target_group")
        if not path:
            return
        if path.suffix.lower() != ".json":
            path = path.with_suffix(".json")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            preset = self._target_group_preset(targets, name=path.stem, source=source)
            path.write_text(json.dumps(preset, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "Target group", str(exc))
            self.status_label.setText(str(exc))
            return
        self.status_label.setText(f"Target group saved: {path} ({len(targets)} target(s))")

    def load_target_group_preset(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "불러오기",
            str(Path.cwd() / "exports"),
            "JSON Files (*.json)",
        )
        if not selected:
            return
        path = Path(selected)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Target group JSON must contain an object.")
            targets = _target_group_targets(data)
            target_interval_overrides = _target_group_interval_overrides(data, targets)
            targets = self._apply_target_group_preset(data)
            self.target_interval_overrides = target_interval_overrides
            self._refresh_target_interval_view()
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            QMessageBox.warning(self, "Target group", str(exc))
            self.status_label.setText(str(exc))
            return
        group_name = _target_group_display_name(data)
        label = f" | {group_name}" if group_name else ""
        overrides = len(self.target_interval_overrides)
        override_label = f" | interval overrides {overrides}" if overrides else ""
        self.status_label.setText(f"Target group loaded: {len(targets)} target(s){label}{override_label}")

    def _target_group_preset(self, targets: list[str], *, name: str, source: str) -> dict[str, object]:
        trace_target = self.trace_target_combo.currentText().strip()
        if trace_target not in targets:
            trace_target = targets[0]
        measurement_mode = self.measurement_mode_combo.currentData() or MEASUREMENT_MODE_FULL_ROUTE
        probe_engine = self.probe_engine_combo.currentData() or PROBE_ENGINE_ICMP
        tcp_port = self.tcp_port_spin.value()
        target_interval_overrides = _filtered_target_interval_overrides(
            self.target_interval_overrides,
            targets,
            global_interval=int(self.interval_combo.currentText()),
        )
        return {
            "version": TARGET_GROUP_PRESET_VERSION,
            "name": name,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "source": source,
            "summary": {
                "target_count": len(targets),
                "trace_target": trace_target,
                "measurement_mode": measurement_mode,
                "probe_engine": probe_engine,
                "tcp_port": tcp_port,
                "target_interval_override_count": len(target_interval_overrides),
            },
            "targets": targets,
            "trace_target": trace_target,
            "target_interval_overrides": target_interval_overrides,
            "settings": {
                "interval_seconds": int(self.interval_combo.currentText()),
                "unlimited": self.unlimited_check.isChecked(),
                "count": self.count_spin.value(),
                "measurement_mode": measurement_mode,
                "probe_engine": probe_engine,
                "tcp_port": tcp_port,
            },
        }

    def _apply_target_group_preset(self, data: dict[str, object]) -> list[str]:
        _validate_target_group_preset_version(data.get("version"))
        targets = _target_group_targets(data)
        settings = data.get("settings", {})
        if settings is None:
            settings = {}
        if not isinstance(settings, dict):
            raise ValueError("Target group JSON has invalid settings.")
        self.target_input.setPlainText("\n".join(targets))
        self.refresh_trace_targets()
        trace_target = str(data.get("trace_target") or "")
        if self.trace_target_combo.findText(trace_target) >= 0:
            self.trace_target_combo.setCurrentText(trace_target)
        _set_interval_combo_value(self.interval_combo, settings.get("interval_seconds"))
        if isinstance(settings.get("unlimited"), bool):
            self.unlimited_check.setChecked(bool(settings["unlimited"]))
        _set_spin_value(self.count_spin, settings.get("count"))
        _set_combo_current_data(self.measurement_mode_combo, settings.get("measurement_mode"))
        _set_combo_current_data(self.probe_engine_combo, settings.get("probe_engine"))
        _set_spin_value(self.tcp_port_spin, settings.get("tcp_port"))
        self._on_probe_engine_changed()
        return targets

    def start_measurement(self) -> None:
        """사용자 입력을 검증한 뒤 MeasurementWorker를 만들고 측정을 시작합니다."""

        # 1단계: GUI 입력값을 실제 IPv4 목록으로 정리합니다.
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

        # 2단계: 단순 화면에서는 입력된 첫 번째 IP를 기준 대상으로 사용합니다.
        target = targets[0]
        valid, message = validate_target(target)
        if not valid:
            QMessageBox.warning(self, "입력 오류", message or "첫 번째 IPv4 주소를 확인하세요.")
            return

        # 3단계: 이전 측정의 화면 상태를 비우고 새 세션 상태로 바꿉니다.
        global_interval = SIMPLE_DEFAULT_INTERVAL_SECONDS
        initial_target_interval_overrides = _filtered_target_interval_overrides(
            self.target_interval_overrides,
            targets,
            global_interval=global_interval,
        )
        self.current_target = target
        self.current_targets = targets
        self.target_interval_overrides = dict(initial_target_interval_overrides)
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
        self._request_graph_render(force=True)
        self._update_graph_detail()
        self._update_target_summary(None)
        self._set_state_chip("탐색", "active")

        max_cycles = None
        measurement_mode = MEASUREMENT_MODE_FINAL_HOP_ONLY
        probe_engine = PROBE_ENGINE_ICMP
        resumed_from_session_id = self._consume_resume_source_for_targets(targets)

        # 4단계: 실제 측정은 별도 QThread인 Worker에서 진행합니다.
        # 아래 connect들은 Worker 결과가 도착했을 때 어떤 화면 함수를 호출할지 연결합니다.
        self.worker = self._create_measurement_worker(
            target=target,
            interval_seconds=global_interval,
            max_cycles=max_cycles,
            targets=targets,
            measurement_mode=measurement_mode,
            probe_engine=probe_engine,
            tcp_port=SIMPLE_DEFAULT_TCP_PORT,
            alert_rule_config=self._alert_rule_config(),
            auto_full_route_on_alert=self._route_adjustment_start_enabled(),
            auto_restore_final_hop_on_recovery=self._route_adjustment_recovery_enabled(),
        )
        setattr(self.worker, "resumed_from_session_id", resumed_from_session_id)
        self._apply_initial_target_interval_overrides(initial_target_interval_overrides)
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

    def _create_measurement_worker(self, **kwargs):
        try:
            signature = inspect.signature(self.worker_factory)
        except (TypeError, ValueError):
            return self.worker_factory(**kwargs)
        if any(parameter.kind == parameter.VAR_KEYWORD for parameter in signature.parameters.values()):
            return self.worker_factory(**kwargs)
        supported_kwargs = {key: value for key, value in kwargs.items() if key in signature.parameters}
        return self.worker_factory(**supported_kwargs)

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

    def pause_visible_targets(self) -> None:
        targets = self._visible_target_addresses()
        if not targets:
            self.status_label.setText("No visible targets to pause")
            return
        self._pause_targets(targets)

    def resume_visible_targets(self) -> None:
        targets = self._visible_target_addresses()
        if not targets:
            self.status_label.setText("No visible targets to resume")
            return
        self._resume_targets(targets)

    def pause_problem_targets(self) -> None:
        targets = self._problem_target_addresses()
        if not targets:
            self.status_label.setText("No problem targets to pause")
            return
        self._pause_targets(targets)

    def resume_problem_targets(self) -> None:
        targets = self._problem_target_addresses()
        if not targets:
            self.status_label.setText("No problem targets to resume")
            return
        self._resume_targets(targets)

    def apply_runtime_interval(self) -> None:
        if not self.worker or not hasattr(self.worker, "set_interval_seconds"):
            return
        interval = int(self.interval_combo.currentText())
        targets = self._selected_target_addresses()
        if targets and hasattr(self.worker, "set_target_interval_seconds"):
            self.worker.set_target_interval_seconds(targets, interval)
            self._record_target_interval_overrides(targets, interval)
            self.status_label.setText(f"Runtime interval applied to {len(targets)} target(s): {interval}s")
            return
        self.worker.set_interval_seconds(interval)
        self.target_interval_overrides.clear()
        self._refresh_target_interval_view()
        self.status_label.setText(f"Runtime interval applied: {interval}s")

    def apply_visible_interval(self) -> None:
        if not self.worker or not hasattr(self.worker, "set_target_interval_seconds"):
            return
        targets = self._visible_target_addresses()
        if not targets:
            self.status_label.setText("No visible targets for interval update")
            return
        interval = int(self.interval_combo.currentText())
        self.worker.set_target_interval_seconds(targets, interval)
        self._record_target_interval_overrides(targets, interval)
        self.status_label.setText(f"Runtime interval applied to visible {len(targets)} target(s): {interval}s")

    def apply_problem_interval(self) -> None:
        if not self.worker or not hasattr(self.worker, "set_target_interval_seconds"):
            return
        targets = self._problem_target_addresses()
        if not targets:
            self.status_label.setText("No problem targets for interval update")
            return
        interval = int(self.interval_combo.currentText())
        self.worker.set_target_interval_seconds(targets, interval)
        self._record_target_interval_overrides(targets, interval)
        self.status_label.setText(f"Runtime interval applied to problem {len(targets)} target(s): {interval}s")

    def _record_target_interval_overrides(self, targets: list[str], interval_seconds: int) -> None:
        for target in targets:
            if target in self.current_targets:
                self.target_interval_overrides[target] = interval_seconds
        self._refresh_target_interval_view()

    def _apply_initial_target_interval_overrides(self, overrides: dict[str, int]) -> None:
        if not overrides or not self.worker or not hasattr(self.worker, "set_target_interval_seconds"):
            return
        by_interval: dict[int, list[str]] = {}
        for target, interval_seconds in overrides.items():
            by_interval.setdefault(interval_seconds, []).append(target)
        for interval_seconds, targets in sorted(by_interval.items()):
            self.worker.set_target_interval_seconds(targets, interval_seconds)

    def _refresh_target_interval_view(self) -> None:
        if hasattr(self, "target_table") and self.target_snapshots:
            self._render_current_view()

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
        selection_model = self.target_table.selectionModel()
        selected_rows = (
            {index.row() for index in selection_model.selectedRows()}
            if selection_model is not None
            else set()
        )
        selected_rows.update(item.row() for item in self.target_table.selectedItems())
        selected_rows = sorted(selected_rows)
        addresses: list[str] = []
        for row in selected_rows:
            item = self.target_table.item(row, 0)
            if item is not None and item.text():
                addresses.append(item.text())
        return addresses

    def _visible_target_addresses(self) -> list[str]:
        return [snapshot.address for snapshot in self._visible_target_snapshots() if snapshot.address]

    def _problem_target_addresses(self) -> list[str]:
        return [
            snapshot.address
            for snapshot in self._display_target_snapshots()
            if snapshot.address and display_status(snapshot) in {"WARNING", "CRITICAL"}
        ]

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
            self._render_current_view(force_graph=True)
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

    def _sync_target_filter(self) -> None:
        if hasattr(self, "target_table"):
            self._render_current_view(force_graph=True)

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
        self._request_graph_render(force=True)
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
        """Worker가 보낸 최신 측정 결과를 화면 상태로 저장하고 다시 그립니다."""

        # Worker는 백그라운드 스레드에서 측정만 담당하고, 화면 위젯은 메인 스레드에서만 바꿉니다.
        # 그래서 여기서 Worker 결과를 MainWindow 상태 변수로 복사한 뒤 표와 그래프를 다시 그립니다.
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

    def _render_current_view(self, *, force_graph: bool = False) -> None:
        """현재 모드(전체/포커스)에 맞춰 표, 그래프, 분석 문구를 한 번에 갱신합니다."""

        # 화면에는 전체 데이터가 아니라 현재 사용자가 보고 있는 데이터만 표시합니다.
        # 예를 들어 필터가 걸려 있거나 포커스 구간이 있으면 아래 값들이 그 조건에 맞게 줄어듭니다.
        snapshots = self._display_snapshots()
        target_snapshots = self._visible_target_snapshots()
        target_snapshot = self._display_target_snapshot()
        analysis = self._display_analysis()
        interval_seconds_by_target, interval_source_by_target = self._target_interval_display_maps(target_snapshots)

        update_hop_table(self.table, snapshots)
        update_target_table(
            self.target_table,
            target_snapshots,
            interval_seconds_by_target=interval_seconds_by_target,
            interval_source_by_target=interval_source_by_target,
        )
        if getattr(self, "problem_sort_check", None) is not None and self.problem_sort_check.isChecked():
            self._apply_target_problem_sort()
        self._update_all_targets_summary(target_snapshots)
        self._update_target_summary(target_snapshot)
        self._sync_running_target_summary()
        self._request_graph_render(force=force_graph or bool(self.pending_alert_image_keys))
        self._save_pending_alert_images()
        self.analysis_box.setPlainText("\n".join(f"- {line}" for line in analysis))
        self._set_export_enabled(self._has_export_data())

    def _display_snapshots(self) -> list[MetricSnapshot]:
        return self.focus_snapshots if self.focus_range is not None else self.snapshots

    def _display_target_snapshot(self) -> MetricSnapshot | None:
        return self.focus_target_snapshot if self.focus_range is not None else self.target_snapshot

    def _display_target_snapshots(self) -> list[MetricSnapshot]:
        return self.focus_target_snapshots if self.focus_range is not None else self.target_snapshots

    def _visible_target_snapshots(self) -> list[MetricSnapshot]:
        snapshots = self._display_target_snapshots()
        terms = self._target_filter_text().casefold().split()
        state_filter = self._target_status_filter()
        if not terms and not state_filter:
            return snapshots
        return [
            snapshot
            for snapshot in snapshots
            if _target_snapshot_matches_filter(snapshot, terms, state_filter)
        ]

    def _target_interval_display_maps(
        self,
        snapshots: list[MetricSnapshot],
    ) -> tuple[dict[str, int | None], dict[str, str]]:
        intervals: dict[str, int | None] = {}
        sources: dict[str, str] = {}
        base_interval = self._runtime_base_interval_seconds()
        for snapshot in snapshots:
            address = snapshot.address or ""
            if not address:
                continue
            if address in self.target_interval_overrides:
                intervals[address] = self.target_interval_overrides[address]
                sources[address] = "target"
            else:
                intervals[address] = base_interval
                sources[address] = "global" if base_interval is not None else ""
        return intervals, sources

    def _runtime_base_interval_seconds(self) -> int | None:
        if self.worker is not None and hasattr(self.worker, "interval_seconds"):
            try:
                return int(getattr(self.worker, "interval_seconds"))
            except (TypeError, ValueError):
                return None
        if hasattr(self, "interval_combo"):
            try:
                return int(self.interval_combo.currentText())
            except ValueError:
                return None
        return None

    def _target_filter_text(self) -> str:
        if not hasattr(self, "target_filter_edit"):
            return ""
        return self.target_filter_edit.text().strip()

    def _target_status_filter(self) -> str:
        if not hasattr(self, "target_status_filter_combo"):
            return ""
        return str(self.target_status_filter_combo.currentData() or "")

    def _display_analysis(self) -> list[str]:
        if self.focus_range is None:
            return self.analysis
        return [self._focus_period_line(), *self.focus_analysis]

    def _update_all_targets_summary(self, snapshots: list[MetricSnapshot]) -> None:
        if not hasattr(self, "target_summary_status_label"):
            return
        total_count = len(self._display_target_snapshots())
        summary = _all_targets_summary_line(snapshots, total_count=total_count)
        updated_at = self._latest_visible_target_update_time(snapshots)
        if updated_at is not None:
            summary = f"{summary} | 갱신 {updated_at.strftime('%H:%M:%S')}"
        self.target_summary_status_label.setText(summary)

    def _refresh_target_summary_selection(self) -> None:
        if not hasattr(self, "target_summary_status_label"):
            return
        self._update_all_targets_summary(self._visible_target_snapshots())

    def _latest_visible_target_update_time(self, snapshots: list[MetricSnapshot]) -> datetime | None:
        visible_addresses = {snapshot.address for snapshot in snapshots if snapshot.address}
        if not visible_addresses:
            return None
        timestamps = [
            observation.timestamp
            for observation in self._graph_observations_for_rows()
            if observation.address in visible_addresses
        ]
        return max(timestamps) if timestamps else None

    def on_session_log_ready(self, path: str) -> None:
        """Worker가 만든 세션 CSV 경로를 받아 Session Manager와 export 기능을 연결합니다."""

        self.session_log_path = Path(path)
        self.route_log_path = route_log_path_for_session(self.session_log_path)
        self.alert_action_log_path = alert_action_log_path_for_session(self.session_log_path)
        self.session_index_store = SessionIndexStore.create(session_index_root_for_sample_path(self.session_log_path))
        self.timeline_status = "Timeline source: session log ready"
        self._sync_timeline_controls()
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
        self._request_graph_render(force=True)
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
            self._sync_timeline_controls()
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
        self._sync_timeline_controls()
        self._render_current_view(force_graph=True)
        self.status_label.setText(self.timeline_status)

    def load_selected_timeline_range(self) -> None:
        seconds = int(self.timeline_range_combo.currentData() or 600)
        self.load_timeline_range(seconds)

    def clear_timeline_range(self) -> None:
        self._clear_timeline_state()
        self._reset_target_graphs_to_current()
        self._render_current_view(force_graph=True)
        self.status_label.setText("Timeline restored to live buffer")

    def reset_focus_to_current(self) -> None:
        self._clear_focus_state()
        self._clear_timeline_state()
        self._reset_target_graphs_to_current()
        if self.graph_detail_window is not None:
            self.graph_detail_window.graph.reset_to_current()
        self._sync_focus_controls()
        self._sync_timeline_controls()
        self._render_current_view(force_graph=True)
        self.status_label.setText("Focus and timeline reset to current")

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

    def _graph_target_history(self) -> list[HopObservation]:
        if self.timeline_range is not None:
            return self.timeline_target_history
        return self.target_history

    def _graph_observations_for_rows(self) -> list[HopObservation]:
        if self.timeline_range is not None:
            return self.timeline_observations
        if self.focus_range is not None:
            return self.focus_observations
        return self.observations

    def _target_histories_by_address(self, observations: list[HopObservation]) -> dict[str, list[HopObservation]]:
        histories: dict[str, list[HopObservation]] = {}
        for observation in observations:
            if not observation.address:
                continue
            if observation.hop_index == 0 or observation.is_target:
                histories.setdefault(observation.address, []).append(observation)
        if not histories:
            current_history = self._graph_target_history()
            if current_history:
                address = self.current_target or current_history[-1].address
                histories[address] = current_history
        return histories

    def _request_graph_render(self, *, force: bool = False) -> None:
        if not hasattr(self, "target_graph_layout"):
            return
        target_count = len(self._visible_target_snapshots())
        if force or target_count < GRAPH_RENDER_THROTTLE_TARGET_COUNT or not self.target_graph_rows:
            self._render_graph_now()
            return

        elapsed = time.monotonic() - self._last_graph_render_monotonic
        if elapsed >= GRAPH_RENDER_THROTTLE_SECONDS:
            self._render_graph_now()
            return

        self._pending_graph_render = True
        remaining_ms = max(int((GRAPH_RENDER_THROTTLE_SECONDS - elapsed) * 1000), 1)
        remaining_timer_ms = self._graph_render_timer.remainingTime()
        if not self._graph_render_timer.isActive() or remaining_timer_ms < 0 or remaining_timer_ms > remaining_ms:
            self._graph_render_timer.start(remaining_ms)

    def _render_pending_graph(self) -> None:
        if self._pending_graph_render:
            self._render_graph_now()

    def _render_graph_now(self) -> None:
        if not hasattr(self, "target_graph_layout"):
            return
        if self._graph_render_timer.isActive():
            self._graph_render_timer.stop()
        self._pending_graph_render = False
        self._sync_target_graph_rows(self._visible_target_snapshots())
        self._update_graph_detail()
        self._last_graph_render_monotonic = time.monotonic()

    def _sync_target_graph_rows(self, target_snapshots: list[MetricSnapshot]) -> None:
        if not hasattr(self, "target_graph_layout"):
            return
        histories = self._target_histories_by_address(self._graph_observations_for_rows())
        addresses = _unique_addresses(snapshot.address for snapshot in target_snapshots)
        if not addresses:
            addresses = _unique_addresses(histories.keys())
        if not addresses:
            self._clear_target_graph_rows()
            self.graph.set_points([])
            self.graph.set_annotations([])
            self.target_graph_empty_label.setVisible(True)
            self.target_graph_render_keys.clear()
            return

        primary_address = self.current_target if self.current_target in addresses else addresses[0]
        if self.primary_graph_address != primary_address:
            self._clear_target_graph_rows()
            self.primary_graph_address = primary_address

        for address in list(self.target_graph_rows):
            if address not in addresses:
                self._remove_target_graph_row(address)

        for address in addresses:
            if address not in self.target_graph_rows:
                self._create_target_graph_row(address, use_primary_graph=address == primary_address)

        for address in addresses:
            self.target_graph_layout.addWidget(self.target_graph_rows[address])

        snapshot_by_address = {snapshot.address: snapshot for snapshot in target_snapshots if snapshot.address}
        for address in addresses:
            history = histories.get(address, [])
            graph = self.target_graph_widgets[address]
            render_key = self._target_graph_render_key(snapshot_by_address.get(address), history)
            if self.target_graph_render_keys.get(address) != render_key:
                graph.set_points(history)
                self.target_graph_title_labels[address].setText(address)
                self.target_graph_metric_labels[address].setText(
                    self._target_graph_metric_text(snapshot_by_address.get(address), history)
                )
                self.target_graph_render_keys[address] = render_key
            if graph is not self.graph:
                graph.set_annotations([])

        self.graph.set_annotations(self._timeline_annotations())
        self.target_graph_empty_label.setVisible(False)

    def _target_graph_render_key(
        self,
        snapshot: MetricSnapshot | None,
        history: list[HopObservation],
    ) -> tuple[object, ...]:
        latest = history[-1] if history else None
        return (
            len(history),
            latest.timestamp.isoformat(timespec="milliseconds") if latest is not None else "",
            latest.status if latest is not None else "",
            latest.latency_ms if latest is not None else None,
            display_status(snapshot) if snapshot is not None else "",
            snapshot.samples if snapshot is not None else 0,
            snapshot.current_latency_ms if snapshot is not None else None,
            snapshot.loss_percent if snapshot is not None else 0.0,
        )

    def _create_target_graph_row(self, address: str, *, use_primary_graph: bool) -> None:
        row = QFrame()
        row.setObjectName("targetGraphRow")
        row.setMinimumHeight(112)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(10)

        info = QVBoxLayout()
        info.setContentsMargins(0, 8, 0, 8)
        info.setSpacing(4)
        title = QLabel(address)
        title.setObjectName("targetGraphTitle")
        title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        metric = QLabel("대기")
        metric.setObjectName("targetGraphMeta")
        metric.setWordWrap(True)
        info.addWidget(title)
        info.addWidget(metric)
        info.addStretch(1)

        graph = self.graph if use_primary_graph else LatencyGraphWidget()
        graph.setMinimumHeight(112)
        row_layout.addLayout(info, 0)
        row_layout.addWidget(graph, 1)

        self.target_graph_rows[address] = row
        self.target_graph_widgets[address] = graph
        self.target_graph_title_labels[address] = title
        self.target_graph_metric_labels[address] = metric

    def _remove_target_graph_row(self, address: str) -> None:
        row = self.target_graph_rows.pop(address, None)
        graph = self.target_graph_widgets.pop(address, None)
        self.target_graph_title_labels.pop(address, None)
        self.target_graph_metric_labels.pop(address, None)
        self.target_graph_render_keys.pop(address, None)
        if graph is self.graph:
            self.graph.setParent(None)
        if row is not None:
            row.setParent(None)
            row.deleteLater()

    def _clear_target_graph_rows(self) -> None:
        for address in list(self.target_graph_rows):
            self._remove_target_graph_row(address)
        self.primary_graph_address = None
        self.target_graph_render_keys.clear()

    def _target_graph_metric_text(
        self,
        snapshot: MetricSnapshot | None,
        history: list[HopObservation],
    ) -> str:
        if snapshot is not None and snapshot.sent:
            current = f"{fmt_ms(snapshot.current_latency_ms)} ms" if snapshot.current_latency_ms is not None else "-"
            return (
                f"{display_status(snapshot)} | 현재 {current} | "
                f"손실 {snapshot.loss_percent:.1f}% | 샘플 {snapshot.samples}"
            )
        if history:
            latest = history[-1]
            current = f"{fmt_ms(latest.latency_ms)} ms" if latest.latency_ms is not None else "-"
            status = latest.status if latest.status else ("OK" if latest.success else "TIMEOUT")
            return f"{status} | 최근 {current} | 샘플 {len(history)}"
        return "대기 | 샘플 0"

    def _reset_target_graphs_to_current(self) -> None:
        seen: set[int] = set()
        for graph in [self.graph, *self.target_graph_widgets.values()]:
            graph_id = id(graph)
            if graph_id in seen:
                continue
            seen.add(graph_id)
            graph.reset_to_current()

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
            self._render_current_view(force_graph=True)
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
        self._render_current_view(force_graph=show_status)
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
        self._sync_timeline_controls()

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
        route_active_keys, route_events = evaluate_route_ip_alert(
            self.snapshots,
            self._watched_route_ip(),
            self._latest_alert_timestamp(),
        )
        active_keys.update(route_active_keys)
        events.extend(route_events)
        for event in events:
            if event.key not in previous_active_keys:
                self._append_alert_event(event)
        ended_keys = previous_active_keys - active_keys
        if ended_keys:
            timestamp = self.target_history[-1].timestamp if self.target_history else datetime.now()
            for key in sorted(ended_keys):
                self._append_alert_event(alert_recovery_event(key, timestamp))
        self.active_alert_keys = active_keys

    def _watched_route_ip(self) -> str:
        if not hasattr(self, "route_ip_alert_check") or not self.route_ip_alert_check.isChecked():
            return ""
        watched_ip = self.route_ip_alert_edit.text().strip()
        targets, invalid = parse_ipv4_targets(watched_ip)
        return targets[0] if len(targets) == 1 and not invalid else ""

    def _latest_alert_timestamp(self) -> datetime:
        if self.target_history:
            return self.target_history[-1].timestamp
        return datetime.now()

    def _alert_rule_config(self) -> AlertRuleConfig:
        loss_threshold = float(self.loss_threshold_spin.value()) if hasattr(self, "loss_threshold_spin") else 20.0
        loss_window_minutes = self.loss_window_spin.value() if hasattr(self, "loss_window_spin") else 3
        latency_threshold = float(self.latency_threshold_spin.value()) if hasattr(self, "latency_threshold_spin") else 100.0
        jitter_threshold = float(self.jitter_threshold_spin.value()) if hasattr(self, "jitter_threshold_spin") else 30.0
        mos_enabled = self.mos_alert_check.isChecked() if hasattr(self, "mos_alert_check") else False
        mos_threshold = float(self.mos_threshold_spin.value()) if hasattr(self, "mos_threshold_spin") else 3.5
        mos_window_minutes = self.mos_window_spin.value() if hasattr(self, "mos_window_spin") else 5
        sample_window = self.sample_window_spin.value() if hasattr(self, "sample_window_spin") else 10
        sample_bad = self.sample_bad_spin.value() if hasattr(self, "sample_bad_spin") else 10
        timer_window_minutes = self.timer_window_spin.value() if hasattr(self, "timer_window_spin") else 5
        return AlertRuleConfig(
            loss_enabled=self.loss_alert_check.isChecked() if hasattr(self, "loss_alert_check") else True,
            loss_threshold_percent=loss_threshold,
            loss_window_seconds=int(loss_window_minutes) * 60,
            latency_enabled=self.latency_alert_check.isChecked() if hasattr(self, "latency_alert_check") else True,
            latency_threshold_ms=latency_threshold,
            jitter_enabled=self.jitter_alert_check.isChecked() if hasattr(self, "jitter_alert_check") else True,
            jitter_threshold_ms=jitter_threshold,
            sample_enabled=self.sample_alert_check.isChecked() if hasattr(self, "sample_alert_check") else True,
            sample_window_count=int(sample_window),
            sample_failure_count=int(sample_bad),
            timer_enabled=self.timer_alert_check.isChecked() if hasattr(self, "timer_alert_check") else True,
            timer_window_seconds=int(timer_window_minutes) * 60,
            mos_enabled=mos_enabled,
            mos_threshold=mos_threshold,
            mos_window_seconds=int(mos_window_minutes) * 60,
        )

    def _route_adjustment_start_enabled(self) -> bool:
        return (
            hasattr(self, "alert_route_adjust_action_check")
            and self.alert_route_adjust_action_check.isChecked()
            and self.alert_start_action_check.isChecked()
        )

    def _route_adjustment_recovery_enabled(self) -> bool:
        return (
            hasattr(self, "alert_route_adjust_action_check")
            and self.alert_route_adjust_action_check.isChecked()
            and self.alert_end_action_check.isChecked()
        )

    def save_alert_rule_preset(self) -> None:
        path = self._select_save_path("alert_preset.json", "JSON Files (*.json)", target="alert_rules")
        if not path:
            return
        if path.suffix.lower() != ".json":
            path = path.with_suffix(".json")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            preset = self._alert_rule_preset(name=path.stem)
            path.write_text(json.dumps(preset, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "Alert preset", str(exc))
            self.status_label.setText(str(exc))
            return
        self.status_label.setText(f"Alert preset saved: {path}")

    def load_alert_rule_preset(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "불러오기",
            str(Path.cwd() / "exports"),
            "JSON Files (*.json)",
        )
        if not selected:
            return
        path = Path(selected)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Alert preset JSON must contain an object.")
            _validate_alert_rule_preset(data)
            self._apply_alert_rule_preset(data)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            QMessageBox.warning(self, "Alert preset", str(exc))
            self.status_label.setText(str(exc))
            return
        preset_name = _alert_preset_display_name(data)
        label = f" | {preset_name}" if preset_name else ""
        summary = _alert_preset_summary(data.get("rules", {}), data.get("actions", {}))
        self.status_label.setText(
            f"Alert preset loaded: {path}{label} | rules {summary['active_rule_count']} | "
            f"actions {summary['active_action_count']}"
        )

    def _alert_rule_preset(self, *, name: str) -> dict[str, object]:
        rules = {
            "loss_enabled": self.loss_alert_check.isChecked(),
            "loss_threshold_percent": self.loss_threshold_spin.value(),
            "loss_window_minutes": self.loss_window_spin.value(),
            "latency_enabled": self.latency_alert_check.isChecked(),
            "latency_threshold_ms": self.latency_threshold_spin.value(),
            "jitter_enabled": self.jitter_alert_check.isChecked(),
            "jitter_threshold_ms": self.jitter_threshold_spin.value(),
            "sample_enabled": self.sample_alert_check.isChecked(),
            "sample_window_count": self.sample_window_spin.value(),
            "sample_failure_count": self.sample_bad_spin.value(),
            "timer_enabled": self.timer_alert_check.isChecked(),
            "timer_window_minutes": self.timer_window_spin.value(),
            "mos_enabled": self.mos_alert_check.isChecked(),
            "mos_threshold": float(self.mos_threshold_spin.value()),
            "mos_window_minutes": self.mos_window_spin.value(),
            "route_ip_enabled": self.route_ip_alert_check.isChecked(),
            "route_ip": self.route_ip_alert_edit.text().strip(),
        }
        actions = {
            "start": self.alert_start_action_check.isChecked(),
            "end": self.alert_end_action_check.isChecked(),
            "route_adjustment": self.alert_route_adjust_action_check.isChecked(),
            "timeline": self.alert_timeline_action_check.isChecked(),
            "comment": self.alert_comment_action_check.isChecked(),
            "log": self.alert_log_action_check.isChecked(),
            "beep": self.alert_beep_action_check.isChecked(),
            "image": self.alert_image_action_check.isChecked(),
            "email": self.alert_email_action_check.isChecked(),
            "email_server": self.alert_email_server_edit.text().strip(),
            "email_to": self.alert_email_to_edit.text().strip(),
            "email_from": self.alert_email_from_edit.text().strip(),
            "email_security": self.alert_email_security_combo.currentData() or ALERT_EMAIL_SECURITY_PLAIN,
            "email_username": self.alert_email_user_edit.text().strip(),
            "email_password_env": self.alert_email_password_env_edit.text().strip(),
            "rest": self.alert_rest_action_check.isChecked(),
            "rest_url": self.alert_rest_url_edit.text().strip(),
            "executable": self.alert_executable_action_check.isChecked(),
            "executable_path": self.alert_executable_path_edit.text().strip(),
        }
        return {
            "version": ALERT_RULE_PRESET_VERSION,
            "name": name,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "summary": _alert_preset_summary(rules, actions),
            "rules": rules,
            "actions": actions,
        }

    def _apply_alert_rule_preset(self, data: dict[str, object]) -> None:
        _validate_alert_rule_preset(data)
        rules = data.get("rules", {})
        actions = data.get("actions", {})
        _set_check_value(self.loss_alert_check, rules.get("loss_enabled"))
        _set_spin_value(self.loss_threshold_spin, rules.get("loss_threshold_percent"))
        _set_spin_value(self.loss_window_spin, rules.get("loss_window_minutes"))
        _set_check_value(self.latency_alert_check, rules.get("latency_enabled"))
        _set_spin_value(self.latency_threshold_spin, rules.get("latency_threshold_ms"))
        _set_check_value(self.jitter_alert_check, rules.get("jitter_enabled"))
        _set_spin_value(self.jitter_threshold_spin, rules.get("jitter_threshold_ms"))
        _set_check_value(self.sample_alert_check, rules.get("sample_enabled"))
        _set_spin_value(self.sample_window_spin, rules.get("sample_window_count"))
        _set_spin_value(self.sample_bad_spin, rules.get("sample_failure_count"))
        _set_check_value(self.timer_alert_check, rules.get("timer_enabled"))
        _set_spin_value(self.timer_window_spin, rules.get("timer_window_minutes"))
        _set_check_value(self.mos_alert_check, rules.get("mos_enabled"))
        _set_double_spin_value(self.mos_threshold_spin, rules.get("mos_threshold"))
        _set_spin_value(self.mos_window_spin, rules.get("mos_window_minutes"))
        _set_check_value(self.route_ip_alert_check, rules.get("route_ip_enabled"))
        if "route_ip" in rules:
            self.route_ip_alert_edit.setText(str(rules.get("route_ip") or ""))
        _set_check_value(self.alert_start_action_check, actions.get("start"))
        _set_check_value(self.alert_end_action_check, actions.get("end"))
        _set_check_value(self.alert_route_adjust_action_check, actions.get("route_adjustment"))
        _set_check_value(self.alert_timeline_action_check, actions.get("timeline"))
        _set_check_value(self.alert_comment_action_check, actions.get("comment"))
        _set_check_value(self.alert_log_action_check, actions.get("log"))
        _set_check_value(self.alert_beep_action_check, actions.get("beep"))
        _set_check_value(self.alert_image_action_check, actions.get("image"))
        _set_check_value(self.alert_email_action_check, actions.get("email"))
        if "email_server" in actions:
            self.alert_email_server_edit.setText(str(actions.get("email_server") or ""))
        if "email_to" in actions:
            self.alert_email_to_edit.setText(str(actions.get("email_to") or ""))
        if "email_from" in actions:
            self.alert_email_from_edit.setText(str(actions.get("email_from") or ""))
        _set_combo_current_data(self.alert_email_security_combo, actions.get("email_security"))
        if "email_username" in actions:
            self.alert_email_user_edit.setText(str(actions.get("email_username") or ""))
        if "email_password_env" in actions:
            self.alert_email_password_env_edit.setText(str(actions.get("email_password_env") or ""))
        _set_check_value(self.alert_rest_action_check, actions.get("rest"))
        if "rest_url" in actions:
            self.alert_rest_url_edit.setText(str(actions.get("rest_url") or ""))
        _set_check_value(self.alert_executable_action_check, actions.get("executable"))
        if "executable_path" in actions:
            self.alert_executable_path_edit.setText(str(actions.get("executable_path") or ""))

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
                self._sync_alerts_box()
            return
        self.alert_events.append(event)
        self.alert_events = self.alert_events[-100:]
        if actions is not None:
            self.alert_event_actions[event.key] = actions
        elif record_actions:
            self.alert_event_actions[event.key] = self._record_alert_actions(event)
        self._sync_alerts_box()

    def _record_alert_actions(self, event: AlertEvent) -> list[str]:
        actions = self._selected_alert_actions(event)
        recorded_actions: list[str] = []
        for action in actions:
            if action == "beep":
                QApplication.beep()
                recorded_actions.append(action)
            elif action == "image":
                self.pending_alert_image_keys.add(event.key)
                recorded_actions.append(action)
            elif action == "email":
                recorded_actions.append("email" if self._send_alert_email_action(event) else "email_failed")
            elif action == "rest":
                recorded_actions.append("rest" if self._send_alert_rest_action(event) else "rest_failed")
            elif action == "executable":
                recorded_actions.append(
                    "executable" if self._run_alert_executable_action(event) else "executable_failed"
                )
            else:
                recorded_actions.append(action)
        if not recorded_actions:
            return []
        append_alert_action(
            self.alert_action_log_path,
            event,
            actions=recorded_actions,
        )
        return recorded_actions

    def _selected_alert_actions(self, event: AlertEvent | None = None) -> list[str]:
        if not hasattr(self, "alert_timeline_action_check"):
            return ["timeline_annotation", "comment"]
        if event is not None and not self._alert_action_phase_enabled(event):
            return []
        actions: list[str] = []
        if (
            event is not None
            and not self._is_alert_end_event(event)
            and hasattr(self, "alert_route_adjust_action_check")
            and self.alert_route_adjust_action_check.isChecked()
            and self.measurement_mode_combo.currentData() == MEASUREMENT_MODE_FINAL_HOP_ONLY
        ):
            actions.append("route_adjustment")
        if self.alert_timeline_action_check.isChecked():
            actions.append("timeline_annotation")
        if self.alert_comment_action_check.isChecked():
            actions.append("comment")
        if self.alert_log_action_check.isChecked():
            actions.append("log")
        if self.alert_beep_action_check.isChecked():
            actions.append("beep")
        if self.alert_image_action_check.isChecked():
            actions.append("image")
        if (
            hasattr(self, "alert_email_action_check")
            and self.alert_email_action_check.isChecked()
            and self._alert_email_config() is not None
        ):
            actions.append("email")
        if (
            hasattr(self, "alert_rest_action_check")
            and self.alert_rest_action_check.isChecked()
            and self._alert_rest_url()
        ):
            actions.append("rest")
        if (
            hasattr(self, "alert_executable_action_check")
            and self.alert_executable_action_check.isChecked()
            and self._alert_executable_path() is not None
        ):
            actions.append("executable")
        return actions

    def _alert_action_phase_enabled(self, event: AlertEvent) -> bool:
        if not hasattr(self, "alert_start_action_check") or not hasattr(self, "alert_end_action_check"):
            return True
        if self._is_alert_end_event(event):
            return self.alert_end_action_check.isChecked()
        return self.alert_start_action_check.isChecked()

    @staticmethod
    def _is_alert_end_event(event: AlertEvent) -> bool:
        return event.title == "Alert ended" or ":ended:" in event.key

    def _alert_email_config(self) -> AlertEmailConfig | None:
        if not hasattr(self, "alert_email_server_edit"):
            return None
        server = self.alert_email_server_edit.text().strip()
        recipient = self.alert_email_to_edit.text().strip()
        if not server or not recipient:
            return None
        host = server
        port = 25
        if ":" in server:
            candidate_host, candidate_port = server.rsplit(":", 1)
            if candidate_port.isdigit():
                host = candidate_host.strip()
                port = int(candidate_port)
        if not host or not (1 <= port <= 65535):
            return None
        sender = self.alert_email_from_edit.text().strip() or DEFAULT_ALERT_EMAIL_FROM
        security = str(self.alert_email_security_combo.currentData() or ALERT_EMAIL_SECURITY_PLAIN)
        if security not in ALERT_EMAIL_SECURITY_MODES:
            security = ALERT_EMAIL_SECURITY_PLAIN
        return AlertEmailConfig(
            host=host,
            port=port,
            sender=sender,
            recipient=recipient,
            security=security,
            username=self.alert_email_user_edit.text().strip(),
            password_env=self.alert_email_password_env_edit.text().strip(),
        )

    def _send_alert_email_action(self, event: AlertEvent) -> bool:
        config = self._alert_email_config()
        if config is None:
            return False
        subject = f"[NetworkPathDiagnostics] {event.severity.upper()} {event.title}"
        body = "\n".join(
            [
                f"Target: {self.current_target or '-'}",
                f"Severity: {event.severity}",
                f"Title: {event.title}",
                f"Message: {event.message}",
                f"Start: {event.start.isoformat(timespec='seconds')}",
                f"End: {event.end.isoformat(timespec='seconds')}",
                f"Key: {event.key}",
            ]
        )
        try:
            self._send_alert_email(
                config.host,
                config.port,
                config.sender,
                config.recipient,
                subject,
                body,
                security=config.security,
                username=config.username,
                password_env=config.password_env,
            )
        except (OSError, smtplib.SMTPException, ValueError) as exc:
            self.status_label.setText(f"Alert email action failed: {exc}")
            return False
        return True

    def _send_alert_email(
        self,
        host: str,
        port: int,
        sender: str,
        recipient: str,
        subject: str,
        body: str,
        *,
        security: str = ALERT_EMAIL_SECURITY_PLAIN,
        username: str = "",
        password_env: str = "",
    ) -> None:
        message = EmailMessage()
        message["From"] = sender
        message["To"] = recipient
        message["Subject"] = subject
        message.set_content(body)
        smtp_class = smtplib.SMTP_SSL if security == ALERT_EMAIL_SECURITY_SSL else smtplib.SMTP
        with smtp_class(host, port, timeout=ALERT_EMAIL_TIMEOUT_SECONDS) as smtp:
            if security == ALERT_EMAIL_SECURITY_STARTTLS:
                smtp.starttls()
            if username:
                if not password_env:
                    raise ValueError("SMTP password environment variable is not configured.")
                password = os.environ.get(password_env, "")
                if not password:
                    raise ValueError("SMTP password environment variable is not set.")
                smtp.login(username, password)
            smtp.send_message(message)

    def _alert_rest_url(self) -> str:
        if not hasattr(self, "alert_rest_url_edit"):
            return ""
        return self.alert_rest_url_edit.text().strip()

    def _send_alert_rest_action(self, event: AlertEvent) -> bool:
        url = self._alert_rest_url()
        if not url:
            return False
        payload = {
            "key": event.key,
            "timestamp": event.timestamp.isoformat(timespec="seconds"),
            "start": event.start.isoformat(timespec="seconds"),
            "end": event.end.isoformat(timespec="seconds"),
            "severity": event.severity,
            "title": event.title,
            "message": event.message,
            "target": self.current_target,
            "series_key": event.series_key,
        }
        try:
            self._post_alert_webhook(url, payload)
        except (OSError, ValueError, urllib.error.URLError):
            self.status_label.setText("Alert REST action failed")
            return False
        return True

    def _post_alert_webhook(self, url: str, payload: dict[str, object]) -> None:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=ALERT_REST_TIMEOUT_SECONDS):
            pass

    def _alert_executable_path(self) -> Path | None:
        if not hasattr(self, "alert_executable_path_edit"):
            return None
        value = self.alert_executable_path_edit.text().strip()
        if not value:
            return None
        path = Path(os.path.expandvars(value)).expanduser()
        return path if path.is_file() else None

    def _run_alert_executable_action(self, event: AlertEvent) -> bool:
        path = self._alert_executable_path()
        if path is None:
            return False
        env = dict(os.environ)
        env.update(
            {
                "NPD_ALERT_KEY": event.key,
                "NPD_ALERT_TITLE": event.title,
                "NPD_ALERT_MESSAGE": event.message,
                "NPD_ALERT_SEVERITY": event.severity,
                "NPD_ALERT_TARGET": self.current_target,
            }
        )
        try:
            self._launch_alert_executable(path, event, env)
        except (OSError, ValueError) as exc:
            self.status_label.setText(f"Alert executable action failed: {exc}")
            return False
        return True

    def _launch_alert_executable(self, path: Path, _event: AlertEvent, env: dict[str, str]) -> None:
        subprocess.Popen(
            [str(path)],
            cwd=str(path.parent),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

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
        """알림 이벤트 목록을 표와 텍스트 요약 영역에 반영합니다."""

        if not hasattr(self, "alerts_box"):
            return
        if hasattr(self, "alert_table"):
            update_alert_table(self.alert_table, self.alert_events, self.alert_event_actions)
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
        """저장된 세션 목록을 읽고, 끊긴 세션/누락 파일 상태를 정리해서 보여줍니다."""

        if not hasattr(self, "sessions_box"):
            return
        if not bool(self.worker and self.worker.isRunning()):
            self.session_index_store.recover_stale_active_sessions(
                stale_after=timedelta(seconds=STALE_ACTIVE_SESSION_RECOVERY_SECONDS)
            )
            self.session_index_store.reconcile_missing_session_files()
        sessions = self.session_index_store.list_sessions()
        filtered_sessions = self._filtered_session_records(sessions)
        visible_sessions = filtered_sessions[:SESSION_MANAGER_DISPLAY_LIMIT]
        self._sync_session_combo(visible_sessions)
        if not sessions:
            self.sessions_box.setPlainText("No saved sessions.")
            return
        lines = [self._session_manager_summary(sessions, filtered_sessions, visible_sessions)]
        lines.append(_session_storage_summary(filtered_sessions if self._session_filter_text() else sessions))
        if not filtered_sessions:
            lines.append("No saved sessions match filter.")
            self.sessions_box.setPlainText("\n".join(lines))
            return
        for session in visible_sessions:
            end = session.end.strftime("%H:%M:%S") if session.end is not None else "running"
            row_parts = [
                session.state,
                session.start.strftime("%Y-%m-%d %H:%M:%S"),
                f"end {end}",
                session.target,
                f"samples {session.samples}",
                _session_probe_summary(session),
            ]
            resume_summary = _session_resume_summary(session)
            if resume_summary:
                row_parts.append(resume_summary)
            lines.append(" | ".join(row_parts))
        self.sessions_box.setPlainText("\n".join(lines))

    def _session_manager_summary(
        self,
        sessions: list[TraceSessionRecord],
        filtered_sessions: list[TraceSessionRecord],
        visible_sessions: list[TraceSessionRecord],
    ) -> str:
        state_counts: dict[str, int] = {}
        filter_text = self._session_filter_text()
        summary_sessions = filtered_sessions if filter_text else sessions
        for session in summary_sessions:
            state_counts[session.state] = state_counts.get(session.state, 0) + 1
        state_summary = ", ".join(f"{state} {count}" for state, count in sorted(state_counts.items()))
        shown = len(visible_sessions)
        filtered_total = len(filtered_sessions)
        total = len(sessions)
        label = f"Sessions: {filtered_total}/{total}" if filter_text else f"Sessions: {total}"
        suffix = f" | {state_summary}" if state_summary else ""
        if shown < filtered_total:
            return f"{label} | showing latest {shown}{suffix}"
        return f"{label}{suffix}"

    def _filtered_session_records(self, sessions: list[TraceSessionRecord]) -> list[TraceSessionRecord]:
        terms = self._session_filter_text().casefold().split()
        if not terms:
            return sessions
        return [session for session in sessions if _session_matches_filter(session, terms)]

    def _session_filter_text(self) -> str:
        if not hasattr(self, "session_filter_edit"):
            return ""
        return self.session_filter_edit.text().strip()

    def _visible_session_records(self) -> list[TraceSessionRecord]:
        sessions = self.session_index_store.list_sessions()
        return self._filtered_session_records(sessions)[:SESSION_MANAGER_DISPLAY_LIMIT]

    def refresh_saved_sessions(self) -> None:
        self.session_index_store.recover_missing_sessions()
        self.session_index_store.reconcile_session_log_metadata()
        self._sync_sessions_box()
        self.status_label.setText("Session list refreshed from saved logs")

    def _sync_session_combo(self, sessions: list[TraceSessionRecord]) -> None:
        if not hasattr(self, "session_combo"):
            return
        self._syncing_session_selection = True
        try:
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
            self._sync_session_table(sessions)
        finally:
            self._syncing_session_selection = False
        has_sessions = bool(sessions)
        self.session_combo.setEnabled(has_sessions)
        can_switch_session = has_sessions and not bool(self.worker and self.worker.isRunning())
        self.open_session_button.setEnabled(can_switch_session)
        self.resume_session_button.setEnabled(can_switch_session)
        self.export_session_button.setEnabled(has_sessions)
        if hasattr(self, "export_visible_sessions_button"):
            self.export_visible_sessions_button.setEnabled(has_sessions)
        self.delete_session_button.setEnabled(has_sessions and not bool(self.worker and self.worker.isRunning()))
        if hasattr(self, "prune_sessions_button"):
            self.prune_sessions_button.setEnabled(has_sessions and not bool(self.worker and self.worker.isRunning()))

    def _sync_session_table(self, sessions: list[TraceSessionRecord]) -> None:
        if not hasattr(self, "session_table"):
            return
        update_session_table(self.session_table, sessions)
        self._select_session_table_row()

    def _select_session_table_row(self) -> None:
        if not hasattr(self, "session_table") or not hasattr(self, "session_combo"):
            return
        session_id = self.session_combo.currentData()
        self.session_table.blockSignals(True)
        try:
            self.session_table.clearSelection()
            if not session_id:
                return
            for row in range(self.session_table.rowCount()):
                item = self.session_table.item(row, 0)
                if item is not None and item.data(SESSION_ID_ROLE) == session_id:
                    self.session_table.selectRow(row)
                    self.session_table.scrollToItem(item)
                    return
        finally:
            self.session_table.blockSignals(False)

    def on_session_table_selection_changed(self) -> None:
        if self._syncing_session_selection or not hasattr(self, "session_table"):
            return
        item = self.session_table.item(self.session_table.currentRow(), 0)
        if item is None:
            return
        session_id = item.data(SESSION_ID_ROLE)
        if not session_id:
            return
        index = self.session_combo.findData(session_id)
        if index < 0:
            return
        self._syncing_session_selection = True
        try:
            self.session_combo.setCurrentIndex(index)
        finally:
            self._syncing_session_selection = False

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

    def export_visible_sessions(self) -> None:
        if self.export_worker and self.export_worker.isRunning():
            QMessageBox.information(self, "Export", "An export is already running.")
            return
        records = self._visible_session_records()
        if not records:
            self.status_label.setText("No visible saved sessions to export")
            return
        path = self._select_save_path("visible_sessions.zip", "ZIP Files (*.zip)", target="visible_sessions")
        if not path:
            return
        try:
            saved_path, file_count = self._write_visible_sessions_zip(path, records)
        except OSError as exc:
            QMessageBox.warning(self, "Export error", str(exc))
            self.status_label.setText(str(exc))
            return
        self.status_label.setText(f"Visible sessions ZIP saved: {saved_path} ({len(records)} session(s), {file_count} file(s))")

    def _write_visible_sessions_zip(
        self,
        path: Path,
        records: list[TraceSessionRecord],
    ) -> tuple[Path, int]:
        if path.suffix.lower() != ".zip":
            path = path.with_suffix(".zip")
        path.parent.mkdir(parents=True, exist_ok=True)
        manifest_buffer = io.StringIO()
        fieldnames = [
            "session_id",
            "target",
            "state",
            "start",
            "end",
            "samples",
            "interval_seconds",
            "measurement_mode",
            "probe_engine",
            "tcp_port",
            "route_probe_engine",
            "resumed_from_session_id",
            "target_count",
            "sample_path",
            "route_path",
            "last_error",
            "exported_file_count",
        ]
        writer = csv.DictWriter(manifest_buffer, fieldnames=fieldnames)
        writer.writeheader()
        file_count = 0
        with zipfile.ZipFile(path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
            for index, record in enumerate(records, start=1):
                folder = _session_export_folder(record, index)
                exported_for_session = 0
                for data_path in session_data_paths(record):
                    if not data_path.exists() or not data_path.is_file():
                        continue
                    archive.write(data_path, f"{folder}/{data_path.name}")
                    exported_for_session += 1
                file_count += exported_for_session
                writer.writerow(
                    {
                        "session_id": record.session_id,
                        "target": record.target,
                        "state": record.state,
                        "start": record.start.isoformat(),
                        "end": record.end.isoformat() if record.end is not None else "",
                        "samples": record.samples,
                        "interval_seconds": record.interval_seconds or "",
                        "measurement_mode": record.measurement_mode,
                        "probe_engine": record.probe_engine,
                        "tcp_port": record.tcp_port or "",
                        "route_probe_engine": record.route_probe_engine,
                        "resumed_from_session_id": record.resumed_from_session_id,
                        "target_count": record.target_count,
                        "sample_path": str(record.sample_path),
                        "route_path": str(record.route_path or ""),
                        "last_error": record.last_error,
                        "exported_file_count": exported_for_session,
                    }
                )
            archive.writestr("session_manifest.csv", manifest_buffer.getvalue())
        return path, file_count

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

    def prune_old_sessions(self) -> None:
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "Session", "Stop the current measurement before pruning saved sessions.")
            return
        if self.export_worker and self.export_worker.isRunning():
            QMessageBox.information(self, "Session", "Wait for the current export to finish before pruning sessions.")
            return
        days = int(self.session_retention_days_spin.value()) if hasattr(self, "session_retention_days_spin") else 90
        reply = QMessageBox.question(
            self,
            "Prune Old Sessions",
            (
                f"Delete saved sessions older than {days} day(s)?\n\n"
                "Active sessions are never pruned."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        pruned = self.session_index_store.prune_sessions_older_than(older_than=timedelta(days=days))
        for record in pruned:
            self._clear_deleted_session_paths(record)
        self._sync_sessions_box()
        self._set_export_enabled(self._has_export_data())
        self.status_label.setText(f"Pruned {len(pruned)} saved session(s) older than {days} day(s)")

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
        self._sync_timeline_controls()

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
        self._sync_timeline_controls()
        self._sync_alerts_box()
        self._sync_route_changes_box()
        self._sync_focus_controls()
        self._render_current_view(force_graph=True)
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
        self.pending_resume_session_id = record.session_id
        self.pending_resume_targets = list(targets)
        self.target_input.setPlainText("\n".join(targets))
        self.refresh_trace_targets()
        if self.trace_target_combo.findText(self.current_target) >= 0:
            self.trace_target_combo.setCurrentText(self.current_target)
        self._restore_session_runtime_controls(record)
        self.status_label.setText(
            f"Resume prepared: {len(targets)} target(s), press Start to create a new session"
        )

    def _consume_resume_source_for_targets(self, targets: list[str]) -> str:
        if not self.pending_resume_session_id:
            return ""
        source_session_id = self.pending_resume_session_id
        expected_targets = set(self.pending_resume_targets)
        self.pending_resume_session_id = ""
        self.pending_resume_targets = []
        if expected_targets and expected_targets == set(targets):
            return source_session_id
        return ""

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

        mode, probe_engine, tcp_port = _session_runtime_fields(record)
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

    def _sync_timeline_controls(self) -> None:
        if not hasattr(self, "timeline_label"):
            return
        visible = self.timeline_range is not None
        self.timeline_label.setText(self._timeline_period_line() if visible else "Timeline: Live")
        self.timeline_label.setToolTip(self.timeline_status)
        self.timeline_label.setProperty("tone", "active" if visible else "neutral")
        self.timeline_label.style().unpolish(self.timeline_label)
        self.timeline_label.style().polish(self.timeline_label)

    def _focus_period_line(self) -> str:
        if self.focus_range is None:
            return "Live"
        start, end = self.focus_range
        return f"Focus period: {start.strftime('%H:%M:%S')} - {end.strftime('%H:%M:%S')}"

    def _timeline_period_line(self) -> str:
        if self.timeline_range is None:
            return "Timeline: Live"
        start, end = self.timeline_range
        return f"Timeline: {start.strftime('%H:%M:%S')} - {end.strftime('%H:%M:%S')}"

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
        report_format = str(self.report_format_combo.currentData() or "txt") if hasattr(self, "report_format_combo") else "txt"
        if report_format == "html":
            self._start_export("html", "html", "HTML Files (*.html)")
        else:
            self._start_export("txt", "txt", "Text Files (*.txt)")

    def save_graph_png(self) -> None:
        if self.export_worker and self.export_worker.isRunning():
            QMessageBox.information(self, "Export", "An export is already running.")
            return
        path = self._select_save_path("png", "PNG Files (*.png)")
        if not path:
            return
        scope = (
            self.graph_png_scope_combo.currentData()
            if hasattr(self, "graph_png_scope_combo")
            else GRAPH_PNG_SCOPE_TIMELINE
        )
        try:
            saved_path = self._save_graph_png(path, scope=scope)
        except RuntimeError as exc:
            QMessageBox.warning(self, "Export error", str(exc))
            self.status_label.setText(str(exc))
            return
        self.status_label.setText(f"PNG saved: {saved_path}")

    def _save_graph_png(self, path: Path, *, scope: str = GRAPH_PNG_SCOPE_TIMELINE) -> Path:
        if path.suffix.lower() != ".png":
            path = path.with_suffix(".png")
        path.parent.mkdir(parents=True, exist_ok=True)
        pixmap = self._graph_png_pixmap(scope)
        if pixmap.isNull():
            raise RuntimeError(f"PNG capture failed: {path}")
        if not pixmap.save(str(path), "PNG"):
            raise RuntimeError(f"PNG save failed: {path}")
        return path

    def _graph_png_pixmap(self, scope: str) -> QPixmap:
        if scope == GRAPH_PNG_SCOPE_TRACE:
            return self.table.grab()
        if scope == GRAPH_PNG_SCOPE_BOTH:
            return _combine_pixmaps([self.table.grab(), self._timeline_graph_png_pixmap()])
        return self._timeline_graph_png_pixmap()

    def _timeline_graph_png_pixmap(self) -> QPixmap:
        if getattr(self, "target_graph_widgets", None) and len(self.target_graph_widgets) > 1:
            return self.target_graph_scroll.grab()
        return self.graph.grab()

    def save_target_summary_csv(self) -> None:
        if self.export_worker and self.export_worker.isRunning():
            QMessageBox.information(self, "Export", "An export is already running.")
            return
        snapshots = list(self._visible_target_snapshots())
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
        interval_seconds_by_target, interval_source_by_target = self._target_interval_display_maps(snapshots)
        for snapshot in snapshots:
            failed = snapshot.sent - snapshot.received
            address = snapshot.address or ""
            rows.append(
                TargetSummaryExportRow(
                    target=address,
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
                    interval_seconds=interval_seconds_by_target.get(address),
                    interval_source=interval_source_by_target.get(address, ""),
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
            source = "route" if is_route_alert_key(event.key) else "alert"
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

    def _running_target_summary_text(self) -> str:
        targets = list(self.current_targets)
        if not targets and hasattr(self, "target_input"):
            targets, _invalid = parse_ipv4_targets(self.target_input.toPlainText())
        primary = self.current_target or (targets[0] if targets else "-")
        return f"측정 IP {len(targets)}개 | 기준 IP {primary}"

    def _sync_running_target_summary(self) -> None:
        label = getattr(self, "running_target_summary_label", None)
        if label is not None:
            label.setText(self._running_target_summary_text())

    def _set_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.target_input.setEnabled(not running)
        self.target_input.setVisible(not running)
        if hasattr(self, "running_target_summary_label"):
            self._sync_running_target_summary()
            self.running_target_summary_label.setVisible(running)
        self.trace_target_combo.setEnabled(not running)
        self.refresh_targets_button.setEnabled(not running)
        if hasattr(self, "save_target_group_button"):
            self.save_target_group_button.setEnabled(not running)
        if hasattr(self, "load_target_group_button"):
            self.load_target_group_button.setEnabled(not running)
        self.measurement_mode_combo.setEnabled(not running)
        self.probe_engine_combo.setEnabled(not running)
        self.tcp_port_spin.setEnabled(not running and self._is_tcp_probe_selected())
        if hasattr(self, "session_combo"):
            has_sessions = self.session_combo.count() > 0
            self.session_combo.setEnabled(has_sessions and not running)
            self.open_session_button.setEnabled(has_sessions and not running)
            self.resume_session_button.setEnabled(has_sessions and not running)
            self.delete_session_button.setEnabled(has_sessions and not running)
            if hasattr(self, "export_visible_sessions_button"):
                self.export_visible_sessions_button.setEnabled(has_sessions)
            if hasattr(self, "prune_sessions_button"):
                self.prune_sessions_button.setEnabled(has_sessions and not running)
        self.interval_combo.setEnabled(True)
        self.count_spin.setEnabled(not running and not self.unlimited_check.isChecked())
        self.unlimited_check.setEnabled(not running)
        for button_name in (
            "pause_selected_targets_button",
            "resume_selected_targets_button",
            "pause_visible_targets_button",
            "resume_visible_targets_button",
            "pause_problem_targets_button",
            "resume_problem_targets_button",
            "pause_all_targets_button",
            "resume_all_targets_button",
            "apply_interval_button",
            "apply_visible_interval_button",
            "apply_problem_interval_button",
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
        if hasattr(self, "report_format_combo"):
            self.report_format_combo.setEnabled(enabled)
        self.graph_png_button.setEnabled(enabled)
        if hasattr(self, "graph_png_scope_combo"):
            self.graph_png_scope_combo.setEnabled(enabled)
        self.stats_csv_button.setEnabled(enabled)
        self.stats_xlsx_button.setEnabled(enabled)
        if hasattr(self, "export_target_summary_button"):
            self.export_target_summary_button.setEnabled(enabled and self._has_target_summary_data())

    def _set_exporting(self, exporting: bool) -> None:
        self.csv_button.setEnabled(not exporting and self._has_export_data())
        self.xlsx_button.setEnabled(not exporting and self._has_export_data())
        self.report_button.setEnabled(not exporting and self._has_export_data())
        if hasattr(self, "report_format_combo"):
            self.report_format_combo.setEnabled(not exporting and self._has_export_data())
        self.graph_png_button.setEnabled(not exporting and self._has_export_data())
        if hasattr(self, "graph_png_scope_combo"):
            self.graph_png_scope_combo.setEnabled(not exporting and self._has_export_data())
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
        if hasattr(self, "export_visible_sessions_button"):
            self.export_visible_sessions_button.setEnabled(not exporting and self.session_combo.count() > 0)
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
        if hasattr(self, "prune_sessions_button"):
            self.prune_sessions_button.setEnabled(
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
        return bool(self._visible_target_snapshots())

    def _set_state_chip(self, text: str, tone: str) -> None:
        self.session_state_label.setText(text)
        self.session_state_label.setProperty("tone", tone)
        self.session_state_label.style().unpolish(self.session_state_label)
        self.session_state_label.style().polish(self.session_state_label)

    def on_target_input_changed(self) -> None:
        self._sync_trace_targets_from_input(manual=False)

    def refresh_trace_targets(self) -> None:
        self._sync_trace_targets_from_input(manual=True)

    def _sync_trace_targets_from_input(self, *, manual: bool) -> None:
        # 붙여넣기 직후 "목록 반영"을 누르지 않아도 측정 기준 IP 콤보가 최신 입력을 보게 합니다.
        # 입력값 자체는 자르지 않고, 실제 측정 시작 시에만 최대 개수 제한을 다시 확인합니다.
        targets, invalid = parse_ipv4_targets(self.target_input.toPlainText())
        over_limit = len(targets) > MAX_IPV4_TARGETS
        self._apply_trace_targets(targets[:MAX_IPV4_TARGETS] if over_limit else targets)
        if not self.target_input.toPlainText().strip():
            self.status_label.setText("대기 중")
        elif invalid:
            preview = f" 제외: {', '.join(invalid[:5])}" if manual else ""
            self.status_label.setText(f"IPv4 {len(targets)}개 인식됨 | 확인 필요 {len(invalid)}개{preview}")
        elif over_limit:
            self.status_label.setText(f"IPv4 {len(targets)}개 인식됨. 시작 시 최대 {MAX_IPV4_TARGETS}개 사용 여부를 확인합니다.")
        else:
            prefix = "등록된" if manual else "인식된"
            self.status_label.setText(f"{prefix} IPv4 {len(targets)}개")

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
        if self._graph_render_timer.isActive():
            self._graph_render_timer.stop()
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


def _unique_addresses(values: object) -> list[str]:
    addresses: list[str] = []
    seen: set[str] = set()
    for value in values:
        address = str(value or "").strip()
        if not address or address in seen:
            continue
        addresses.append(address)
        seen.add(address)
    return addresses


def _all_targets_summary_line(snapshots: list[MetricSnapshot], *, total_count: int | None = None) -> str:
    total = len(snapshots) if total_count is None else total_count
    label = f"IP: {len(snapshots)}/{total}" if total != len(snapshots) else f"IP: {len(snapshots)}"
    if not snapshots:
        return label
    statuses = [display_status(snapshot) for snapshot in snapshots]
    critical = statuses.count("CRITICAL")
    warning = statuses.count("WARNING")
    paused = statuses.count("PAUSED")
    ok = statuses.count("OK")
    other = len(snapshots) - critical - warning - paused - ok
    parts = [
        label,
        f"정상 {ok}",
        f"주의 {warning}",
        f"장애 {critical}",
        f"중지 {paused}",
    ]
    if other:
        parts.append(f"기타 {other}")
    worst_loss = max(snapshot.loss_percent for snapshot in snapshots)
    max_latency = max(
        (snapshot.current_latency_ms for snapshot in snapshots if snapshot.current_latency_ms is not None),
        default=None,
    )
    max_latency_text = f"{fmt_ms(max_latency)} ms" if max_latency is not None else "-"
    parts.extend([f"최대 손실 {worst_loss:.1f}%", f"최대 지연 {max_latency_text}"])
    return " | ".join(parts)


def _target_snapshot_matches_filter(snapshot: MetricSnapshot, terms: list[str], state_filter: str) -> bool:
    status = display_status(snapshot)
    if state_filter == "problem":
        if status not in {"WARNING", "CRITICAL"}:
            return False
    elif state_filter and status != state_filter:
        return False
    values = [
        snapshot.address or "",
        snapshot.hostname or "",
        status,
        fmt_ms(snapshot.current_latency_ms),
        fmt_ms(snapshot.avg_latency_ms),
        f"{snapshot.loss_percent:.1f}",
        str(snapshot.sent),
        str(snapshot.received),
        str(snapshot.timeout_count),
    ]
    haystack = " ".join(value for value in values if value).casefold()
    return all(term in haystack for term in terms)


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


def _session_matches_filter(session: TraceSessionRecord, terms: list[str]) -> bool:
    return all(_session_matches_filter_term(session, term) for term in terms)


def _session_matches_filter_term(session: TraceSessionRecord, term: str) -> bool:
    if ":" in term:
        key, value = term.split(":", 1)
        key = key.strip().casefold()
        value = value.strip().casefold()
        if not value:
            return True
        if key in {"target", "ip"}:
            return value in session.target.casefold()
        if key in {"state", "status"}:
            return value in _filter_value(session.state)
        if key in {"month", "ym"}:
            return any(value in bucket.month.casefold() for bucket in session_storage_buckets([session]))
        if key == "bucket":
            return any(
                value in _filter_value(f"{bucket.target}/{bucket.month}")
                for bucket in session_storage_buckets([session])
            )
        if key in {"engine", "probe"}:
            return value in _filter_value(session.probe_engine) or value in _filter_value(_session_probe_summary(session))
        if key == "mode":
            return value in _filter_value(session.measurement_mode) or value in _filter_value(_session_probe_summary(session))
        if key == "port":
            return value in str(session.tcp_port or "").casefold()
        if key == "resume":
            return value in _filter_value(session.resumed_from_session_id)
        if key == "error":
            return value in _filter_value(session.last_error)
    return term in _session_filter_haystack(session)


def _session_filter_haystack(session: TraceSessionRecord) -> str:
    values = [
        session.session_id,
        session.target,
        session.state,
        session.measurement_mode,
        session.probe_engine,
        str(session.tcp_port or ""),
        session.route_probe_engine,
        session.resumed_from_session_id,
        _session_probe_summary(session),
        _session_resume_summary(session),
        str(session.interval_seconds or ""),
        str(session.target_count),
        str(session.sample_path),
        str(session.route_path or ""),
        session.last_error,
        " ".join(str(segment) for segment in session.segments),
        " ".join(f"{bucket.target}/{bucket.month}" for bucket in session_storage_buckets([session])),
    ]
    return " ".join(_filter_value(value) for value in values if value)


def _filter_value(value: object) -> str:
    return str(value).casefold().replace(" ", "_")


def _session_export_folder(record: TraceSessionRecord, index: int) -> str:
    timestamp = record.start.strftime("%Y%m%d_%H%M%S")
    target = safe_target_name(record.target)
    session_id = safe_target_name(record.session_id)
    return f"{index:03d}_{target}_{timestamp}_{session_id}"


def _session_storage_summary(sessions: list[TraceSessionRecord]) -> str:
    summary = session_storage_summary(sessions)
    line = (
        f"Storage: targets {summary.target_count} | "
        f"target-month buckets {summary.bucket_count} | segments {summary.segment_count} | "
        f"indexed samples {summary.sample_count}"
    )
    buckets = session_storage_buckets(sessions)
    if not buckets:
        return line
    bucket_parts = [_session_storage_bucket_summary(bucket) for bucket in buckets[:3]]
    if len(buckets) > 3:
        bucket_parts.append(f"+{len(buckets) - 3} more")
    return f"{line}\nRecent buckets: {', '.join(bucket_parts)}"


def _session_storage_bucket_summary(bucket: SessionStorageBucket) -> str:
    states = "; ".join(f"{state} {count}" for state, count in bucket.state_counts)
    state_suffix = f" states {states}" if states else ""
    return (
        f"{bucket.target}/{bucket.month} sessions {bucket.session_count} "
        f"segments {bucket.segment_count} indexed samples {bucket.sample_count}{state_suffix}"
    )


ALERT_RULE_ENABLED_KEYS = (
    "loss_enabled",
    "latency_enabled",
    "jitter_enabled",
    "sample_enabled",
    "timer_enabled",
    "mos_enabled",
    "route_ip_enabled",
)
ALERT_ACTION_ENABLED_KEYS = (
    "route_adjustment",
    "timeline",
    "comment",
    "log",
    "beep",
    "image",
    "email",
    "rest",
    "executable",
)
ALERT_ACTION_PHASE_KEYS = ("start", "end")
ALERT_EXTERNAL_ACTION_KEYS = ("email", "rest", "executable")


def _alert_preset_summary(rules: object, actions: object) -> dict[str, object]:
    rule_map = rules if isinstance(rules, dict) else {}
    action_map = actions if isinstance(actions, dict) else {}
    return {
        "active_rule_count": _count_truthy_keys(rule_map, ALERT_RULE_ENABLED_KEYS),
        "active_action_count": _count_truthy_keys(action_map, ALERT_ACTION_ENABLED_KEYS),
        "action_phase_count": _count_truthy_keys(action_map, ALERT_ACTION_PHASE_KEYS),
        "external_action_count": _count_truthy_keys(action_map, ALERT_EXTERNAL_ACTION_KEYS),
        "route_adjustment_enabled": bool(action_map.get("route_adjustment")),
    }


def _validate_alert_rule_preset(data: dict[str, object]) -> None:
    version = _alert_rule_preset_version(data.get("version"))
    rules = data.get("rules", {})
    actions = data.get("actions", {})
    if not isinstance(rules, dict) or not isinstance(actions, dict):
        raise ValueError("Alert preset JSON has invalid rules/actions.")
    if version >= ALERT_RULE_PRESET_VERSION:
        _validate_alert_preset_summary(data.get("summary"), rules=rules, actions=actions)


def _alert_rule_preset_version(value: object) -> int:
    if value in (None, ""):
        return 1
    try:
        version = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Alert preset JSON has an invalid version.") from exc
    if version < 1 or version > ALERT_RULE_PRESET_VERSION:
        raise ValueError(f"Alert preset JSON version is not supported: {version}")
    return version


def _validate_alert_preset_summary(summary: object, *, rules: dict[str, object], actions: dict[str, object]) -> None:
    if not isinstance(summary, dict):
        raise ValueError("Alert preset JSON has invalid summary metadata.")
    actual = _alert_preset_summary(rules, actions)
    for key in ("active_rule_count", "active_action_count", "action_phase_count", "external_action_count"):
        if key not in summary:
            raise ValueError(f"Alert preset JSON summary is missing {key}.")
        try:
            expected = int(summary[key])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Alert preset JSON summary has invalid {key}.") from exc
        if expected != actual[key]:
            raise ValueError(f"Alert preset JSON {key} does not match rules/actions.")
    if "route_adjustment_enabled" in summary and bool(summary["route_adjustment_enabled"]) != actual["route_adjustment_enabled"]:
        raise ValueError("Alert preset JSON route_adjustment_enabled does not match actions.")


def _alert_preset_display_name(data: dict[str, object]) -> str:
    return str(data.get("name") or "").strip()


def _count_truthy_keys(data: dict[str, object], keys: tuple[str, ...]) -> int:
    return sum(1 for key in keys if bool(data.get(key)))


def _validate_target_group_preset_version(value: object) -> None:
    if value in (None, ""):
        return
    try:
        version = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Target group JSON has an invalid version.") from exc
    if version < 1 or version > TARGET_GROUP_PRESET_VERSION:
        raise ValueError(f"Target group JSON version is not supported: {version}")


def _target_group_targets(data: dict[str, object]) -> list[str]:
    target_values = data.get("targets")
    if not isinstance(target_values, list):
        raise ValueError("Target group JSON must contain a targets list.")
    targets, invalid = parse_ipv4_targets("\n".join(str(target) for target in target_values))
    if invalid or not targets:
        raise ValueError("Target group JSON must contain valid IPv4 targets.")
    _validate_target_group_summary(data.get("summary"), target_count=len(targets))
    return targets


def _validate_target_group_summary(summary: object, *, target_count: int) -> None:
    if summary in (None, ""):
        return
    if not isinstance(summary, dict):
        raise ValueError("Target group JSON has invalid summary metadata.")
    expected_count = summary.get("target_count")
    if expected_count in (None, ""):
        return
    try:
        expected_count_int = int(expected_count)
    except (TypeError, ValueError) as exc:
        raise ValueError("Target group JSON has invalid target_count metadata.") from exc
    if expected_count_int != target_count:
        raise ValueError("Target group JSON target_count does not match the targets list.")


def _target_group_interval_overrides(data: dict[str, object], targets: list[str]) -> dict[str, int]:
    raw_overrides = data.get("target_interval_overrides", {})
    if raw_overrides in (None, ""):
        return {}
    if not isinstance(raw_overrides, dict):
        raise ValueError("Target group JSON has invalid target_interval_overrides.")
    target_set = set(targets)
    overrides: dict[str, int] = {}
    for target, value in raw_overrides.items():
        target_text = str(target)
        if target_text not in target_set:
            continue
        try:
            interval = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Target group JSON has invalid target interval override.") from exc
        if interval < 0:
            raise ValueError("Target group JSON has invalid target interval override.")
        overrides[target_text] = interval
    _validate_target_group_interval_override_summary(data.get("summary"), override_count=len(overrides))
    return overrides


def _validate_target_group_interval_override_summary(summary: object, *, override_count: int) -> None:
    if not isinstance(summary, dict):
        return
    expected_count = summary.get("target_interval_override_count")
    if expected_count in (None, ""):
        return
    try:
        expected_count_int = int(expected_count)
    except (TypeError, ValueError) as exc:
        raise ValueError("Target group JSON has invalid target_interval_override_count metadata.") from exc
    if expected_count_int != override_count:
        raise ValueError("Target group JSON target_interval_override_count does not match the overrides list.")


def _filtered_target_interval_overrides(
    overrides: dict[str, int],
    targets: list[str],
    *,
    global_interval: int,
) -> dict[str, int]:
    target_set = set(targets)
    filtered: dict[str, int] = {}
    for target, interval_seconds in overrides.items():
        if target not in target_set:
            continue
        interval = max(int(interval_seconds), 0)
        if interval == global_interval:
            continue
        filtered[target] = interval
    return filtered


def _target_group_display_name(data: dict[str, object]) -> str:
    name = str(data.get("name") or "").strip()
    if name:
        return name
    source = str(data.get("source") or "").strip()
    return source


def _set_spin_value(spin: QSpinBox, value: object) -> None:
    if value is None:
        return
    try:
        spin.setValue(int(value))
    except (TypeError, ValueError):
        return


def _set_double_spin_value(spin: QDoubleSpinBox, value: object) -> None:
    if value is None:
        return
    try:
        spin.setValue(float(value))
    except (TypeError, ValueError):
        return


def _set_check_value(check: QCheckBox, value: object) -> None:
    if value is None:
        return
    check.setChecked(bool(value))


def _set_combo_current_data(combo: QComboBox, value: object) -> None:
    if value is None:
        return
    index = combo.findData(value)
    if index >= 0:
        combo.setCurrentIndex(index)


def _combine_pixmaps(pixmaps: list[QPixmap]) -> QPixmap:
    valid = [pixmap for pixmap in pixmaps if not pixmap.isNull()]
    if not valid:
        return QPixmap()
    width = max(pixmap.width() for pixmap in valid)
    height = sum(pixmap.height() for pixmap in valid)
    combined = QPixmap(width, height)
    combined.fill(Qt.GlobalColor.white)
    painter = QPainter(combined)
    try:
        y = 0
        for pixmap in valid:
            painter.drawPixmap(0, y, pixmap)
            y += pixmap.height()
    finally:
        painter.end()
    return combined


def _set_interval_combo_value(combo: QComboBox, value: object) -> None:
    if value is None:
        return
    try:
        interval = str(int(value))
    except (TypeError, ValueError):
        return
    index = combo.findText(interval)
    if index < 0:
        combo.addItem(interval)
        index = combo.findText(interval)
    combo.setCurrentIndex(index)


def _session_runtime_fields(session: TraceSessionRecord) -> tuple[str, str, int | None]:
    mode, legacy_probe_engine, legacy_tcp_port = _parse_session_measurement_mode(session.measurement_mode)
    probe_engine = session.probe_engine or legacy_probe_engine
    tcp_port = session.tcp_port if session.tcp_port is not None else legacy_tcp_port
    return mode, probe_engine, tcp_port


def _session_probe_summary(session: TraceSessionRecord) -> str:
    mode, probe_engine, tcp_port = _session_runtime_fields(session)
    parts = [_session_mode_label(mode), _probe_engine_label(probe_engine)]
    if tcp_port is not None:
        parts.append(f"port {tcp_port}")
    if session.route_probe_engine:
        parts.append(f"route {session.route_probe_engine}")
    return " / ".join(part for part in parts if part and part != "-")


def _session_resume_summary(session: TraceSessionRecord) -> str:
    if not session.resumed_from_session_id:
        return ""
    return f"resumed from {session.resumed_from_session_id}"


def _session_mode_label(value: str) -> str:
    return {
        MEASUREMENT_MODE_FULL_ROUTE: "Full Route",
        MEASUREMENT_MODE_FINAL_HOP_ONLY: "Final Hop Only",
    }.get(value, value or "-")


def _probe_engine_label(value: str) -> str:
    return {
        PROBE_ENGINE_ICMP: "ICMP",
        PROBE_ENGINE_TCP_CONNECT: "TCP Connect",
    }.get(value, value or "-")


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
QLabel#commandTitle {
    font-size: 15px;
    font-weight: 700;
    color: #111827;
    background: transparent;
}
QLabel#commandSubtitle {
    color: #6b7280;
    font-size: 11px;
    background: transparent;
}
QLabel#panelTitle {
    font-size: 15px;
    font-weight: 700;
    color: #111827;
}
QLabel#muted,
QLabel#statusText {
    color: #6b7280;
    background: transparent;
}
QLabel#mutedStrong {
    color: #4b5563;
    font-weight: 600;
}
QLabel#runningTargetSummary {
    background: #eef2ff;
    border: 1px solid #c7d2fe;
    border-radius: 6px;
    color: #1f2937;
    font-weight: 700;
    padding: 7px 10px;
}
QFrame#targetPanelInline {
    background: transparent;
    border: 0;
}
QLabel#targetGraphEmpty {
    color: #6b7280;
    padding: 18px;
}
QLabel#targetGraphTitle {
    color: #111827;
    font-size: 12px;
    font-weight: 700;
    min-width: 132px;
}
QLabel#targetGraphMeta {
    color: #6b7280;
    font-size: 11px;
    min-width: 132px;
}
QLabel#warningText {
    color: #92400e;
}
QLabel#fieldLabel,
QLabel#metricLabel {
    color: #6b7280;
    background: transparent;
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
    max-height: 24px;
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
QPlainTextEdit,
QTextEdit {
    background: #ffffff;
    border: 1px solid #d1d5db;
    border-radius: 6px;
    padding: 5px 8px;
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
QPushButton#targetPanelToggle {
    padding: 6px 10px;
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
