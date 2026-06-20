from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.core.models import HopObservation, MetricSnapshot
from app.ui.latency_graph import LatencyGraphWidget, TimelineAnnotation, TimelineSeries, series_color_hex
from app.ui.table_panels import fmt_ms
from app.utils.filename import default_export_path


VIEW_TARGET = "target"
VIEW_ALL_HOPS = "all_hops"
VIEW_SELECTED_HOP = "selected_hop"
VIEW_VISIBLE_HOPS = "visible_hops"


@dataclass(frozen=True)
class RangeSummary:
    samples: int
    timeout_count: int
    loss_percent: float
    avg_latency_ms: float | None
    max_latency_ms: float | None


class GraphDetailWindow(QMainWindow):
    focus_applied = Signal(object)
    focus_cleared = Signal()
    timeline_range_requested = Signal(int)
    timeline_live_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        _apply_detail_font()
        self.setWindowTitle("Target Latency Graph")
        self.resize(1120, 700)
        self.metric_value_labels: dict[str, QLabel] = {}
        self._target = ""
        self._target_snapshot: MetricSnapshot | None = None
        self._target_history: list[HopObservation] = []
        self._all_observations: list[HopObservation] = []
        self._hop_snapshots: list[MetricSnapshot] = []
        self._selected_hop_index: int | None = None
        self._visible_hop_indices: set[int] = set()
        self._visible_hops_initialized = False
        self._hop_checkboxes: dict[int, QCheckBox] = {}
        self._annotations: list[TimelineAnnotation] = []
        self._external_annotations: list[TimelineAnnotation] = []

        central = QWidget(self)
        central.setStyleSheet(GRAPH_DETAIL_STYLE)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        title_row = QHBoxLayout()
        self.title_label = QLabel("Target Latency Timeline")
        self.title_label.setObjectName("title")
        self.target_label = QLabel("대상: -")
        self.target_label.setObjectName("muted")
        title_row.addWidget(self.title_label)
        title_row.addStretch(1)
        title_row.addWidget(self.target_label)

        layout.addLayout(title_row)
        layout.addWidget(self._build_metrics_strip())
        self.graph = LatencyGraphWidget()
        self.graph.setMinimumHeight(430)
        self.graph.selection_changed.connect(self._update_range_summary)
        layout.addWidget(self._build_controls())

        graph_frame = QFrame()
        graph_frame.setObjectName("graphFrame")
        graph_layout = QVBoxLayout(graph_frame)
        graph_layout.setContentsMargins(12, 12, 12, 12)
        graph_layout.addWidget(self.graph, 1)
        layout.addWidget(graph_frame, 1)

        self.range_summary_label = QLabel("No range selected")
        self.range_summary_label.setObjectName("rangeSummary")
        layout.addWidget(self.range_summary_label)

        self.setCentralWidget(central)
        self.set_data("", None, [], [], [], None)

    def set_data(
        self,
        target: str,
        target_snapshot: MetricSnapshot | None,
        target_history: list[HopObservation],
        all_observations: list[HopObservation] | None = None,
        hop_snapshots: list[MetricSnapshot] | None = None,
        selected_hop_index: int | None = None,
    ) -> None:
        self._target = target
        self._target_snapshot = target_snapshot
        self._target_history = list(target_history)
        self._all_observations = list(all_observations or [])
        self._hop_snapshots = list(hop_snapshots or [])
        if selected_hop_index is not None:
            self._selected_hop_index = selected_hop_index
        elif self._selected_hop_index is None and self._hop_snapshots:
            self._selected_hop_index = self._hop_snapshots[0].hop_index
        self._sync_visible_hops_with_snapshots()
        self.target_label.setText(f"대상: {target or '-'}")
        self._update_metrics(target_snapshot)
        self._refresh_hop_selector()
        self._refresh_graph()

    def set_timeline_status(self, message: str) -> None:
        self.timeline_status_label.setText(message)

    def set_external_annotations(self, annotations: list[TimelineAnnotation]) -> None:
        self._external_annotations = list(annotations)
        self._refresh_graph()

    def set_selected_hop_index(self, hop_index: int | None) -> None:
        self._selected_hop_index = hop_index
        self._refresh_hop_selector()
        if hop_index is not None:
            selected_index = self.view_combo.findData(VIEW_SELECTED_HOP)
            if selected_index >= 0:
                self.view_combo.setCurrentIndex(selected_index)
        self._refresh_graph()

    def toggle_hop_visibility(self, hop_index: int) -> None:
        self.set_hop_visibility(hop_index, hop_index not in self._visible_hop_indices)

    def set_hop_visibility(self, hop_index: int, visible: bool) -> None:
        available = {snapshot.hop_index for snapshot in self._hop_snapshots}
        if hop_index not in available:
            return
        if visible:
            self._visible_hop_indices.add(hop_index)
        else:
            self._visible_hop_indices.discard(hop_index)
        self._visible_hops_initialized = True
        self._refresh_hop_selector()
        self._select_visible_hops_view()

    def is_hop_visible(self, hop_index: int) -> bool:
        return hop_index in self._visible_hop_indices

    def _build_controls(self) -> QFrame:
        controls = QFrame()
        controls.setObjectName("controls")
        layout = QVBoxLayout(controls)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(6)
        top_row = QHBoxLayout()
        top_row.setSpacing(8)
        preset_row = QHBoxLayout()
        preset_row.setSpacing(8)

        self.view_combo = QComboBox()
        self.view_combo.addItem("최종 대상", VIEW_TARGET)
        self.view_combo.addItem("전체 Hop", VIEW_ALL_HOPS)
        self.view_combo.addItem("선택 Hop", VIEW_SELECTED_HOP)
        self.view_combo.addItem("Shown Hops", VIEW_VISIBLE_HOPS)
        self.view_combo.currentIndexChanged.connect(self._refresh_graph)

        self.hop_combo = QComboBox()
        self.hop_combo.currentIndexChanged.connect(self._on_hop_combo_changed)

        pan_left_button = QPushButton("Prev")
        pan_left_button.setToolTip("Move timeline earlier")
        pan_left_button.clicked.connect(self.graph.pan_left)
        pan_right_button = QPushButton("Next")
        pan_right_button.setToolTip("Move timeline later")
        pan_right_button.clicked.connect(self.graph.pan_right)
        current_button = QPushButton("Current")
        current_button.setToolTip("Return timeline to latest samples")
        current_button.clicked.connect(self.graph.reset_to_current)
        zoom_in_button = QPushButton("+")
        zoom_in_button.setToolTip("Zoom in")
        zoom_in_button.clicked.connect(self.graph.zoom_in)
        zoom_out_button = QPushButton("-")
        zoom_out_button.setToolTip("Zoom out")
        zoom_out_button.clicked.connect(self.graph.zoom_out)
        reset_button = QPushButton("Reset")
        reset_button.clicked.connect(self.graph.reset_zoom)

        self.annotation_input = QLineEdit()
        self.annotation_input.setPlaceholderText("메모")
        annotate_button = QPushButton("Add note")
        annotate_button.clicked.connect(self.add_annotation_from_selection)
        save_png_button = QPushButton("Save PNG")
        save_png_button.clicked.connect(lambda: self.save_png())
        clear_button = QPushButton("Clear range")
        clear_button.clicked.connect(self.graph.clear_selection)
        apply_focus_button = QPushButton("Apply focus")
        apply_focus_button.clicked.connect(self.apply_focus_from_selection)
        clear_focus_button = QPushButton("Clear focus")
        clear_focus_button.clicked.connect(lambda: self.focus_cleared.emit())

        top_row.addWidget(QLabel("View"))
        top_row.addWidget(self.view_combo)
        top_row.addWidget(QLabel("Hop"))
        top_row.addWidget(self.hop_combo, 1)
        top_row.addWidget(pan_left_button)
        top_row.addWidget(pan_right_button)
        top_row.addWidget(current_button)
        top_row.addWidget(zoom_in_button)
        top_row.addWidget(zoom_out_button)
        top_row.addWidget(reset_button)
        top_row.addWidget(self.annotation_input, 1)
        top_row.addWidget(annotate_button)
        top_row.addWidget(save_png_button)
        top_row.addWidget(apply_focus_button)
        top_row.addWidget(clear_button)
        top_row.addWidget(clear_focus_button)

        self.timeline_status_label = QLabel("Timeline source: live buffer")
        self.timeline_status_label.setObjectName("timelineStatus")
        preset_row.addWidget(QLabel("Scale"))
        for label, seconds in [
            ("60s", 60),
            ("10m", 600),
            ("1h", 3600),
            ("6h", 21600),
            ("24h", 86400),
        ]:
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, seconds=seconds: self.timeline_range_requested.emit(seconds))
            preset_row.addWidget(button)
        live_button = QPushButton("Live")
        live_button.clicked.connect(lambda: self.timeline_live_requested.emit())
        preset_row.addWidget(live_button)
        preset_row.addWidget(self.timeline_status_label, 1)

        layout.addLayout(top_row)
        layout.addLayout(preset_row)
        layout.addWidget(self._build_hop_toggle_scroll())
        return controls

    def _build_hop_toggle_scroll(self) -> QScrollArea:
        self.hop_toggle_scroll = QScrollArea()
        self.hop_toggle_scroll.setObjectName("hopToggleScroll")
        self.hop_toggle_scroll.setWidgetResizable(True)
        self.hop_toggle_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.hop_toggle_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.hop_toggle_scroll.setFixedHeight(46)

        self.hop_toggle_container = QWidget()
        self.hop_toggle_layout = QHBoxLayout(self.hop_toggle_container)
        self.hop_toggle_layout.setContentsMargins(0, 0, 0, 0)
        self.hop_toggle_layout.setSpacing(8)
        self.hop_toggle_scroll.setWidget(self.hop_toggle_container)
        return self.hop_toggle_scroll

    def _build_metrics_strip(self) -> QFrame:
        strip = QFrame()
        strip.setObjectName("metrics")
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
            box = QFrame()
            box.setObjectName("metricBox")
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

    def _refresh_graph(self) -> None:
        series = self._current_series()
        self.graph.set_series(series, [*self._external_annotations, *self._annotations])
        self._update_range_summary(self.graph.selection_datetime_range())

    def _current_series(self) -> list[TimelineSeries]:
        mode = self.view_combo.currentData()
        if mode == VIEW_ALL_HOPS:
            return self._all_hop_series()
        if mode == VIEW_SELECTED_HOP:
            selected = self._selected_hop_series()
            return selected or self._target_series()
        if mode == VIEW_VISIBLE_HOPS:
            return self._visible_hop_series()
        return self._target_series()

    def _target_series(self) -> list[TimelineSeries]:
        if not self._target_history:
            return []
        return [TimelineSeries("target", self._target or "Target", self._target_history)]

    def _all_hop_series(self) -> list[TimelineSeries]:
        grouped = self._observations_by_hop()
        series: list[TimelineSeries] = []
        for snapshot in sorted(self._hop_snapshots, key=lambda item: item.hop_index):
            hop_series = self._series_for_snapshot(snapshot, grouped)
            if hop_series is not None:
                series.append(hop_series)
        return series or self._target_series()

    def _visible_hop_series(self) -> list[TimelineSeries]:
        grouped = self._observations_by_hop()
        series: list[TimelineSeries] = []
        for snapshot in sorted(self._hop_snapshots, key=lambda item: item.hop_index):
            if snapshot.hop_index not in self._visible_hop_indices:
                continue
            hop_series = self._series_for_snapshot(snapshot, grouped)
            if hop_series is not None:
                series.append(hop_series)
        return series

    def _selected_hop_series(self) -> list[TimelineSeries]:
        if self._selected_hop_index is None:
            return []
        grouped = self._observations_by_hop()
        points = grouped.get(self._selected_hop_index, [])
        if not points:
            return []
        snapshot = next(
            (item for item in self._hop_snapshots if item.hop_index == self._selected_hop_index),
            None,
        )
        if snapshot is None:
            label = f"Hop {self._selected_hop_index}"
            return [TimelineSeries(f"hop-{self._selected_hop_index}", label, points)]
        hop_series = self._series_for_snapshot(snapshot, grouped)
        return [hop_series] if hop_series is not None else []

    def _series_for_snapshot(
        self,
        snapshot: MetricSnapshot,
        grouped: dict[int, list[HopObservation]],
    ) -> TimelineSeries | None:
        points = grouped.get(snapshot.hop_index, [])
        if not points:
            return None
        label = f"Hop {snapshot.hop_index} {snapshot.address or ''}".strip()
        return TimelineSeries(
            f"hop-{snapshot.hop_index}",
            label,
            points,
            self._hop_color(snapshot.hop_index),
        )

    def _observations_by_hop(self) -> dict[int, list[HopObservation]]:
        grouped: dict[int, list[HopObservation]] = defaultdict(list)
        for observation in self._all_observations:
            if observation.hop_index > 0:
                grouped[observation.hop_index].append(observation)
        return grouped

    def _refresh_hop_selector(self) -> None:
        self.hop_combo.blockSignals(True)
        self.hop_combo.clear()
        for snapshot in sorted(self._hop_snapshots, key=lambda item: item.hop_index):
            label = f"Hop {snapshot.hop_index} - {snapshot.address or 'Timeout'}"
            self.hop_combo.addItem(label, snapshot.hop_index)
            if snapshot.hop_index == self._selected_hop_index:
                self.hop_combo.setCurrentIndex(self.hop_combo.count() - 1)
        self.hop_combo.setEnabled(self.hop_combo.count() > 0)
        self.hop_combo.blockSignals(False)
        self._refresh_hop_toggles()

    def _on_hop_combo_changed(self) -> None:
        hop_index = self.hop_combo.currentData()
        self._selected_hop_index = int(hop_index) if hop_index is not None else None
        if self.view_combo.currentData() == VIEW_SELECTED_HOP:
            self._refresh_graph()

    def _refresh_hop_toggles(self) -> None:
        if not hasattr(self, "hop_toggle_layout"):
            return
        self._clear_layout(self.hop_toggle_layout)
        self._hop_checkboxes = {}
        if not self._hop_snapshots:
            empty = QLabel("No route hops")
            empty.setObjectName("muted")
            self.hop_toggle_layout.addWidget(empty)
            self.hop_toggle_layout.addStretch(1)
            return

        for snapshot in sorted(self._hop_snapshots, key=lambda item: item.hop_index):
            item = QFrame()
            item.setObjectName("hopToggle")
            item_layout = QHBoxLayout(item)
            item_layout.setContentsMargins(8, 3, 8, 3)
            item_layout.setSpacing(5)

            swatch = QFrame()
            swatch.setObjectName("seriesSwatch")
            swatch.setFixedSize(10, 10)
            swatch.setStyleSheet(f"background: {self._hop_color(snapshot.hop_index)}; border-radius: 3px;")

            checkbox = QCheckBox(f"Hop {snapshot.hop_index}")
            checkbox.setToolTip(snapshot.address or "Timeout")
            checkbox.blockSignals(True)
            checkbox.setChecked(snapshot.hop_index in self._visible_hop_indices)
            checkbox.blockSignals(False)
            checkbox.toggled.connect(
                lambda checked, hop_index=snapshot.hop_index: self._on_hop_visibility_toggled(hop_index, checked)
            )
            self._hop_checkboxes[snapshot.hop_index] = checkbox

            item_layout.addWidget(swatch)
            item_layout.addWidget(checkbox)
            self.hop_toggle_layout.addWidget(item)
        self.hop_toggle_layout.addStretch(1)

    def _on_hop_visibility_toggled(self, hop_index: int, visible: bool) -> None:
        if visible:
            self._visible_hop_indices.add(hop_index)
        else:
            self._visible_hop_indices.discard(hop_index)
        self._visible_hops_initialized = True
        self._select_visible_hops_view()

    def _select_visible_hops_view(self) -> None:
        visible_index = self.view_combo.findData(VIEW_VISIBLE_HOPS)
        if visible_index >= 0 and self.view_combo.currentIndex() != visible_index:
            self.view_combo.setCurrentIndex(visible_index)
        else:
            self._refresh_graph()

    def _sync_visible_hops_with_snapshots(self) -> None:
        available = {snapshot.hop_index for snapshot in self._hop_snapshots}
        if not available:
            self._visible_hop_indices = set()
            self._visible_hops_initialized = False
            return
        self._visible_hop_indices &= available
        if self._visible_hops_initialized:
            return
        default_hop = self._selected_hop_index if self._selected_hop_index in available else min(available)
        self._visible_hop_indices = {default_hop}
        self._visible_hops_initialized = True

    def _hop_color(self, hop_index: int) -> str:
        hop_indexes = [snapshot.hop_index for snapshot in sorted(self._hop_snapshots, key=lambda item: item.hop_index)]
        try:
            return series_color_hex(hop_indexes.index(hop_index))
        except ValueError:
            return series_color_hex(0)

    def _clear_layout(self, layout: QHBoxLayout) -> None:
        while True:
            item = layout.takeAt(0)
            if item is None:
                return
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def add_annotation_from_selection(self) -> None:
        selection = self.graph.selection_datetime_range()
        if selection is None:
            return
        label = self.annotation_input.text().strip() or "장애"
        start, end = selection
        self._annotations.append(TimelineAnnotation(start, end, label, self._current_annotation_series_key()))
        self.annotation_input.clear()
        self.graph.set_annotations(self._annotations)

    def apply_focus_from_selection(self) -> None:
        selection = self.graph.selection_datetime_range()
        if selection is not None:
            self.focus_applied.emit(selection)

    def save_png(self, path: Path | None = None) -> Path | None:
        selected_path = path or self._select_png_path()
        if selected_path is None:
            return None
        if selected_path.suffix.lower() != ".png":
            selected_path = selected_path.with_suffix(".png")
        selected_path.parent.mkdir(parents=True, exist_ok=True)
        pixmap = self.centralWidget().grab()
        if not pixmap.save(str(selected_path), "PNG"):
            raise RuntimeError(f"PNG save failed: {selected_path}")
        self.timeline_status_label.setText(f"PNG saved: {selected_path}")
        return selected_path

    def _select_png_path(self) -> Path | None:
        default = default_export_path(self._target or "target", "png", Path.cwd() / "exports")
        default.parent.mkdir(parents=True, exist_ok=True)
        selected, _ = QFileDialog.getSaveFileName(self, "Save PNG", str(default), "PNG Files (*.png)")
        return Path(selected) if selected else None

    def _current_annotation_series_key(self) -> str | None:
        mode = self.view_combo.currentData()
        if mode == VIEW_SELECTED_HOP and self._selected_hop_index is not None:
            return f"hop-{self._selected_hop_index}"
        if mode == VIEW_TARGET:
            return "target"
        return None

    def _update_range_summary(self, selection: object) -> None:
        if not selection:
            self.range_summary_label.setText("No range selected")
            return
        start, end = selection
        if not isinstance(start, datetime) or not isinstance(end, datetime):
            self.range_summary_label.setText("No range selected")
            return
        points = [
            point
            for series in self._current_series()
            for point in series.points
            if start <= point.timestamp <= end
        ]
        summary = summarize_points(points)
        self.range_summary_label.setText(
            f"{start.strftime('%H:%M:%S')} - {end.strftime('%H:%M:%S')} | "
            f"samples {summary.samples} | loss {summary.loss_percent:.1f}% | "
            f"timeouts {summary.timeout_count} | avg {fmt_ms(summary.avg_latency_ms) or '-'} ms | "
            f"max {fmt_ms(summary.max_latency_ms) or '-'} ms"
        )

    def _update_metrics(self, target_snapshot: MetricSnapshot | None) -> None:
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
                "current": f"{fmt_ms(target_snapshot.current_latency_ms)} ms"
                if target_snapshot.current_latency_ms is not None
                else "-",
                "avg": f"{fmt_ms(target_snapshot.avg_latency_ms)} ms"
                if target_snapshot.avg_latency_ms is not None
                else "-",
                "loss": f"{target_snapshot.loss_percent:.1f}%",
                "jitter": f"{fmt_ms(target_snapshot.jitter_ms)} ms" if target_snapshot.jitter_ms is not None else "-",
                "samples": str(target_snapshot.samples),
            }
        for key, value in values.items():
            self.metric_value_labels[key].setText(value)


def summarize_points(points: list[HopObservation]) -> RangeSummary:
    samples = len(points)
    if samples == 0:
        return RangeSummary(0, 0, 0.0, None, None)
    timeout_count = sum(1 for point in points if not point.success)
    latencies = [point.latency_ms for point in points if point.success and point.latency_ms is not None]
    avg_latency = sum(latencies) / len(latencies) if latencies else None
    max_latency = max(latencies) if latencies else None
    loss_percent = timeout_count / samples * 100
    return RangeSummary(samples, timeout_count, loss_percent, avg_latency, max_latency)


def _apply_detail_font() -> None:
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


GRAPH_DETAIL_STYLE = """
QWidget {
    background: #f3f4f6;
    color: #111827;
    font-family: "Malgun Gothic", "Segoe UI", Arial, sans-serif;
    font-size: 12px;
}
QLabel#title {
    font-size: 18px;
    font-weight: 700;
}
QLabel#muted,
QLabel#metricLabel {
    color: #6b7280;
}
QFrame#metrics,
QFrame#controls,
QFrame#graphFrame {
    background: #ffffff;
    border: 1px solid #d9dee7;
    border-radius: 8px;
}
QFrame#metricBox {
    background: #ffffff;
    border: 1px solid #e5e7eb;
    border-radius: 6px;
}
QFrame#hopToggle {
    background: #f8fafc;
    border: 1px solid #e5e7eb;
    border-radius: 6px;
}
QScrollArea#hopToggleScroll {
    background: #ffffff;
    border: 0;
}
QLabel#metricLabel {
    font-size: 11px;
    font-weight: 600;
}
QLabel#metricValue {
    color: #111827;
    font-size: 20px;
    font-weight: 700;
}
QLabel#rangeSummary {
    background: #ffffff;
    border: 1px solid #d9dee7;
    border-radius: 6px;
    padding: 8px;
    color: #374151;
}
QLabel#timelineStatus {
    color: #4b5563;
}
QPushButton {
    background: #ffffff;
    border: 1px solid #cbd5e1;
    border-radius: 6px;
    padding: 5px 10px;
}
QPushButton:hover {
    border-color: #2563eb;
}
QComboBox,
QLineEdit {
    background: #ffffff;
    border: 1px solid #cbd5e1;
    border-radius: 6px;
    padding: 5px 8px;
}
"""
