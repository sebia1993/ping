from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QTextEdit,
)

from app.ui.worker import (
    MEASUREMENT_MODE_FINAL_HOP_ONLY,
    MEASUREMENT_MODE_FULL_ROUTE,
    PROBE_ENGINE_ICMP,
    PROBE_ENGINE_TCP_CONNECT,
)


def build_controls_panel(owner, panel_factory: Callable[[str], QFrame], field_label_factory: Callable[[str], QLabel]) -> QFrame:
    controls = panel_factory("controls")
    layout = QGridLayout(controls)
    layout.setContentsMargins(14, 12, 14, 12)
    layout.setHorizontalSpacing(10)
    layout.setVerticalSpacing(8)

    owner.target_input = QTextEdit()
    owner.target_input.setPlaceholderText("IPv4 주소를 입력하거나 엑셀에서 붙여넣으세요.\n예: 8.8.8.8  192.168.0.1")
    owner.target_input.setMaximumHeight(72)
    owner.trace_target_combo = QComboBox()
    owner.refresh_targets_button = QPushButton("목록 반영")
    owner.measurement_mode_combo = QComboBox()
    owner.measurement_mode_combo.addItem("Full Route", MEASUREMENT_MODE_FULL_ROUTE)
    owner.measurement_mode_combo.addItem("Final Hop Only", MEASUREMENT_MODE_FINAL_HOP_ONLY)
    owner.probe_engine_combo = QComboBox()
    owner.probe_engine_combo.addItem("ICMP", PROBE_ENGINE_ICMP)
    owner.probe_engine_combo.addItem("TCP Connect", PROBE_ENGINE_TCP_CONNECT)
    owner.tcp_port_spin = QSpinBox()
    owner.tcp_port_spin.setRange(1, 65535)
    owner.tcp_port_spin.setValue(443)
    owner.interval_combo = QComboBox()
    owner.interval_combo.addItems(["1", "2", "5"])
    owner.count_spin = QSpinBox()
    owner.count_spin.setRange(1, 99999)
    owner.count_spin.setValue(100)
    owner.unlimited_check = QCheckBox("무제한")
    owner.unlimited_check.setChecked(True)
    owner.unlimited_check.toggled.connect(owner._on_unlimited_toggled)
    owner.probe_engine_combo.currentIndexChanged.connect(owner._on_probe_engine_changed)
    owner.count_spin.setDisabled(True)

    owner.start_button = QPushButton("Start")
    owner.start_button.setObjectName("primaryButton")
    owner.stop_button = QPushButton("Stop")
    owner.stop_button.setObjectName("dangerButton")
    owner.save_target_group_button = QPushButton("Save group")
    owner.save_selected_target_group_button = QPushButton("Save selected")
    owner.load_target_group_button = QPushButton("Load group")
    owner.csv_button = QPushButton("CSV")
    owner.xlsx_button = QPushButton("XLSX")
    owner.report_format_combo = QComboBox()
    owner.report_format_combo.addItem("TXT Report", "txt")
    owner.report_format_combo.addItem("HTML Report", "html")
    owner.report_button = QPushButton("Report")
    owner.graph_png_button = QPushButton("Graph PNG")
    owner.stats_csv_button = QPushButton("Stats CSV")
    owner.stats_xlsx_button = QPushButton("Stats XLSX")

    owner.refresh_targets_button.clicked.connect(owner.refresh_trace_targets)
    owner.save_target_group_button.clicked.connect(owner.save_target_group_preset)
    owner.save_selected_target_group_button.clicked.connect(owner.save_selected_target_group_preset)
    owner.load_target_group_button.clicked.connect(owner.load_target_group_preset)
    owner.start_button.clicked.connect(owner.start_measurement)
    owner.stop_button.clicked.connect(owner.stop_measurement)
    owner.csv_button.clicked.connect(owner.save_csv)
    owner.xlsx_button.clicked.connect(owner.save_xlsx)
    owner.report_button.clicked.connect(owner.save_report)
    owner.graph_png_button.clicked.connect(owner.save_graph_png)
    owner.stats_csv_button.clicked.connect(owner.save_statistics_csv)
    owner.stats_xlsx_button.clicked.connect(owner.save_statistics_xlsx)

    layout.addWidget(field_label_factory("대상 IPv4"), 0, 0)
    layout.addWidget(owner.target_input, 0, 1, 2, 4)
    layout.addWidget(field_label_factory("Tracert 대상"), 0, 5)
    layout.addWidget(owner.trace_target_combo, 0, 6)
    layout.addWidget(owner.refresh_targets_button, 0, 7)
    layout.addWidget(field_label_factory("주기(초)"), 0, 8)
    layout.addWidget(owner.interval_combo, 0, 9)
    layout.addWidget(field_label_factory("횟수"), 0, 10)
    layout.addWidget(owner.count_spin, 0, 11)
    layout.addWidget(field_label_factory("Mode"), 1, 5)
    layout.addWidget(owner.measurement_mode_combo, 1, 6)
    layout.addWidget(owner.unlimited_check, 1, 7)
    layout.addWidget(owner.start_button, 1, 8)
    layout.addWidget(owner.stop_button, 1, 9)
    layout.addWidget(field_label_factory("Engine"), 1, 10)
    layout.addWidget(owner.probe_engine_combo, 1, 11)
    layout.addWidget(field_label_factory("TCP Port"), 2, 10)
    layout.addWidget(owner.tcp_port_spin, 2, 11)
    layout.addWidget(owner.save_target_group_button, 3, 0)
    layout.addWidget(owner.save_selected_target_group_button, 3, 1)
    layout.addWidget(owner.load_target_group_button, 3, 2)
    layout.addWidget(owner.report_format_combo, 3, 3)

    owner.status_label = QLabel("대기 중")
    owner.status_label.setObjectName("statusText")
    owner.warning_label = QLabel(
        "도메인과 IPv6은 등록하지 않습니다. 중간 Hop의 packet loss는 ICMP rate limit 또는 방화벽 정책일 수 있으므로 최종 대상 상태와 함께 판단하세요."
    )
    owner.warning_label.setObjectName("warningText")
    owner.warning_label.setWordWrap(True)
    owner.engine_note_label = QLabel("")
    owner.engine_note_label.setObjectName("warningText")
    owner.engine_note_label.setWordWrap(True)

    layout.addWidget(owner.status_label, 2, 0, 1, 4)
    layout.addWidget(owner.warning_label, 2, 4, 1, 6)
    layout.addWidget(owner.engine_note_label, 3, 4, 1, 8)
    layout.setColumnStretch(4, 1)
    owner._on_probe_engine_changed()
    return controls
