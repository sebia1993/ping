from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.ui.worker import (
    MEASUREMENT_MODE_FINAL_HOP_ONLY,
    MEASUREMENT_MODE_FULL_ROUTE,
    PROBE_ENGINE_ICMP,
    PROBE_ENGINE_TCP_CONNECT,
)


class TargetInputEdit(QPlainTextEdit):
    """IP 목록을 붙여넣기 쉽게 만든 plain text 입력칸입니다.

    기존 테스트와 내부 코드 일부가 QTextEdit의 setText() 이름을 쓰고 있어서,
    초보자가 보기에도 의미가 분명한 setPlainText()로 연결해 호환성을 유지합니다.
    """

    def setText(self, text: str) -> None:
        self.setPlainText(text)


def build_controls_panel(owner, panel_factory: Callable[[str], QFrame], field_label_factory: Callable[[str], QLabel]) -> QFrame:
    controls = panel_factory("controls")
    root = QVBoxLayout(controls)
    root.setContentsMargins(12, 8, 12, 8)
    root.setSpacing(0)

    owner.target_input = TargetInputEdit()
    owner.target_input.setPlaceholderText("IP를 한 줄에 하나씩 입력하거나 붙여넣기")
    owner.target_input.setMinimumHeight(110)
    owner.target_input.setMaximumHeight(180)
    owner.target_input.setLineWrapMode(QPlainTextEdit.NoWrap)
    owner.trace_target_combo = QComboBox()
    owner.refresh_targets_button = QPushButton("목록 반영")
    owner.measurement_mode_combo = QComboBox()
    owner.measurement_mode_combo.addItem("전체 경로", MEASUREMENT_MODE_FULL_ROUTE)
    owner.measurement_mode_combo.addItem("최종 IP만", MEASUREMENT_MODE_FINAL_HOP_ONLY)
    owner.measurement_mode_combo.setCurrentIndex(owner.measurement_mode_combo.findData(MEASUREMENT_MODE_FINAL_HOP_ONLY))
    owner.probe_engine_combo = QComboBox()
    owner.probe_engine_combo.addItem("ICMP", PROBE_ENGINE_ICMP)
    owner.probe_engine_combo.addItem("TCP 연결", PROBE_ENGINE_TCP_CONNECT)
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

    owner.start_button = QPushButton("시작")
    owner.start_button.setObjectName("primaryButton")
    owner.stop_button = QPushButton("중지")
    owner.stop_button.setObjectName("dangerButton")
    owner.save_target_group_button = QPushButton("그룹 저장")
    owner.save_selected_target_group_button = QPushButton("선택 저장")
    owner.load_target_group_button = QPushButton("그룹 불러오기")
    owner.csv_button = QPushButton("CSV")
    owner.xlsx_button = QPushButton("XLSX")
    owner.report_format_combo = QComboBox()
    owner.report_format_combo.addItem("TXT 보고서", "txt")
    owner.report_format_combo.addItem("HTML 보고서", "html")
    owner.report_button = QPushButton("보고서")
    owner.graph_png_button = QPushButton("그래프 PNG")
    owner.stats_csv_button = QPushButton("통계 CSV")
    owner.stats_xlsx_button = QPushButton("통계 XLSX")

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

    owner.command_title_label = QLabel("멀티핑체크")
    owner.command_title_label.setObjectName("commandTitle")
    owner.command_title_label.setMinimumWidth(150)
    owner.command_title_label.setMaximumWidth(180)
    owner.command_subtitle_label = QLabel("IP 입력 후 시작하면 대상별 실시간 그래프를 표시합니다.")
    owner.command_subtitle_label.setObjectName("commandSubtitle")
    owner.command_subtitle_label.setVisible(False)
    owner.status_label = QLabel("대기 중")
    owner.status_label.setObjectName("statusText")
    owner.status_label.setMinimumWidth(110)
    owner.status_label.setMaximumWidth(170)
    owner.session_state_label = QLabel("대기")
    owner.session_state_label.setObjectName("chip")
    owner.session_state_label.setProperty("tone", "neutral")
    owner.target_input.textChanged.connect(owner.on_target_input_changed)
    owner.warning_label = QLabel(
        "도메인과 IPv6은 등록하지 않습니다. 중간 Hop의 패킷 손실은 ICMP 응답 제한 또는 방화벽 정책일 수 있으므로 최종 대상 상태와 함께 판단하세요."
    )
    owner.warning_label.setObjectName("warningText")
    owner.warning_label.setWordWrap(True)
    owner.engine_note_label = QLabel("")
    owner.engine_note_label.setObjectName("warningText")
    owner.engine_note_label.setWordWrap(True)
    owner.running_target_summary_label = QLabel("")
    owner.running_target_summary_label.setObjectName("runningTargetSummary")
    owner.running_target_summary_label.setWordWrap(True)
    owner.running_target_summary_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    owner.running_target_summary_label.setVisible(False)
    owner.runtime_target_input = QLineEdit()
    owner.runtime_target_input.setPlaceholderText("추가 IP")
    owner.runtime_target_input.setClearButtonEnabled(True)
    owner.runtime_target_input.setMinimumWidth(150)
    owner.runtime_target_input.setVisible(False)
    owner.runtime_target_input.returnPressed.connect(owner.add_runtime_targets)
    owner.add_runtime_target_button = QPushButton("추가")
    owner.add_runtime_target_button.setVisible(False)
    owner.add_runtime_target_button.clicked.connect(owner.add_runtime_targets)

    title_group = QVBoxLayout()
    title_group.setContentsMargins(0, 0, 0, 0)
    title_group.setSpacing(1)
    title_group.addWidget(owner.command_title_label)

    simple_row = QHBoxLayout()
    simple_row.setSpacing(10)
    simple_row.addLayout(title_group, 0)
    simple_row.addWidget(owner.target_input, 1)
    simple_row.addWidget(owner.running_target_summary_label, 1)
    simple_row.addWidget(owner.runtime_target_input, 1)
    simple_row.addWidget(owner.add_runtime_target_button, 0)
    simple_row.addWidget(owner.status_label, 0)
    simple_row.addWidget(owner.session_state_label, 0)
    simple_row.addWidget(owner.start_button)
    simple_row.addWidget(owner.stop_button)
    root.addLayout(simple_row)

    owner.advanced_controls_panel = QWidget()
    advanced = QGridLayout(owner.advanced_controls_panel)
    advanced.setContentsMargins(0, 2, 0, 0)
    advanced.setHorizontalSpacing(10)
    advanced.setVerticalSpacing(8)
    advanced.addWidget(field_label_factory("측정 기준 IP"), 0, 0)
    advanced.addWidget(owner.trace_target_combo, 0, 1)
    advanced.addWidget(owner.refresh_targets_button, 0, 2)
    advanced.addWidget(field_label_factory("주기(초)"), 0, 3)
    advanced.addWidget(owner.interval_combo, 0, 4)
    advanced.addWidget(field_label_factory("횟수"), 0, 5)
    advanced.addWidget(owner.count_spin, 0, 6)
    advanced.addWidget(field_label_factory("측정 방식"), 1, 0)
    advanced.addWidget(owner.measurement_mode_combo, 1, 1)
    advanced.addWidget(owner.unlimited_check, 1, 2)
    advanced.addWidget(field_label_factory("엔진"), 1, 3)
    advanced.addWidget(owner.probe_engine_combo, 1, 4)
    advanced.addWidget(field_label_factory("TCP 포트"), 1, 5)
    advanced.addWidget(owner.tcp_port_spin, 1, 6)
    advanced.addWidget(owner.save_target_group_button, 2, 0)
    advanced.addWidget(owner.save_selected_target_group_button, 2, 1)
    advanced.addWidget(owner.load_target_group_button, 2, 2)
    advanced.addWidget(owner.report_format_combo, 2, 3)
    advanced.addWidget(owner.warning_label, 3, 0, 1, 4)
    advanced.addWidget(owner.engine_note_label, 3, 4, 1, 3)
    advanced.setColumnStretch(7, 1)
    owner.advanced_controls_panel.setVisible(False)
    root.addWidget(owner.advanced_controls_panel)

    owner._on_probe_engine_changed()
    return controls
