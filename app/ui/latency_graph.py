from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from PySide6.QtCore import QPointF, QRect, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

from app.core.models import STATUS_ERROR, STATUS_TIMEOUT, STATUS_UNREACHABLE, HopObservation


MAX_DISPLAY_POINTS = 1200
SERIES_COLORS = ["#2563eb", "#059669", "#7c3aed", "#d97706", "#0f766e", "#be123c"]
FAILURE_BAR_COLOR = "#dc2626"
FAILURE_REGION_ALPHA = 48
FAILURE_STATUSES = {STATUS_TIMEOUT, STATUS_UNREACHABLE, STATUS_ERROR}
TIME_AXIS_MODE_RECENT = "recent"
TIME_AXIS_MODE_ALL = "all"


@dataclass(frozen=True)
class TimelineSeries:
    key: str
    label: str
    points: list[HopObservation]
    color: str | None = None


@dataclass(frozen=True)
class TimelineAnnotation:
    start: datetime
    end: datetime
    label: str
    series_key: str | None = None


class LatencyGraphWidget(QWidget):
    selection_changed = Signal(object)
    time_pan_requested = Signal(float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._points: list[HopObservation] = []
        self._series: list[TimelineSeries] = []
        self._annotations: list[TimelineAnnotation] = []
        self._selection: tuple[float, float] | None = None
        self._zoom_fraction = 1.0
        self._view_end_timestamp: float | None = None
        self._external_visible_range: tuple[float, float] | None = None
        self._full_time_range_cache: tuple[float | None, float | None] | None = None
        self._visible_points_cache: dict[tuple[str, float | None, float | None], list[HopObservation]] = {}
        self._failure_spans_cache: dict[tuple[str, float, float], list[tuple[float, float]]] = {}
        self._main_graph_mode = False
        self._time_axis_mode = TIME_AXIS_MODE_RECENT
        self._drag_started_at: float | None = None
        self._pan_drag_anchor_x: float | None = None
        self._pan_drag_anchor_view_end: float | None = None
        self._last_plot_rect = QRect()
        self.setMinimumHeight(180)
        self.setMouseTracking(True)

    def set_points(self, points: list[HopObservation]) -> None:
        self._points = list(points)
        self.set_series([TimelineSeries("target", "대상", self._points)])

    def set_main_graph_mode(self, enabled: bool) -> None:
        self._main_graph_mode = enabled

    def set_time_axis_mode(self, mode: str) -> None:
        next_mode = mode if mode in {TIME_AXIS_MODE_RECENT, TIME_AXIS_MODE_ALL} else TIME_AXIS_MODE_RECENT
        if self._time_axis_mode == next_mode:
            return
        self._time_axis_mode = next_mode
        self.update()

    def set_visible_time_range(self, start: datetime | None, end: datetime | None) -> None:
        if start is None or end is None:
            next_range = None
        else:
            start_value = start.timestamp()
            end_value = end.timestamp()
            next_range = (min(start_value, end_value), max(start_value, end_value))
        if self._external_visible_range == next_range:
            return
        self._external_visible_range = next_range
        self._clear_visible_render_caches()
        self.update()

    def set_series(
        self,
        series: list[TimelineSeries],
        annotations: list[TimelineAnnotation] | None = None,
    ) -> None:
        self._series = [
            TimelineSeries(item.key, item.label, _downsample_points(item.points, MAX_DISPLAY_POINTS), item.color)
            for item in series
            if item.points
        ]
        self._points = self._series[0].points if self._series else []
        self._annotations = list(annotations or [])
        self._clear_render_caches()
        self._view_end_timestamp = self._clamp_view_end(self._view_end_timestamp)
        self._selection = self._clamp_selection(self._selection)
        self.update()

    def set_annotations(self, annotations: list[TimelineAnnotation]) -> None:
        next_annotations = list(annotations)
        if self._annotations == next_annotations:
            return
        self._annotations = next_annotations
        self.update()

    def zoom_in(self) -> None:
        self._zoom_at_x(None, 0.7)

    def zoom_out(self) -> None:
        self._zoom_at_x(None, 1 / 0.7)

    def reset_zoom(self) -> None:
        self._zoom_fraction = 1.0
        self._view_end_timestamp = None
        self._clear_visible_render_caches()
        self.update()

    def pan_left(self) -> None:
        self._pan(-0.5)

    def pan_right(self) -> None:
        self._pan(0.5)

    def reset_to_current(self) -> None:
        self._view_end_timestamp = None
        self._clear_visible_render_caches()
        self.update()

    def visible_datetime_range(self) -> tuple[datetime, datetime] | None:
        start, end = self._visible_range()
        if start is None or end is None:
            return None
        return datetime.fromtimestamp(start), datetime.fromtimestamp(end)

    def select_time_range(self, start: datetime, end: datetime) -> None:
        start_value = start.timestamp()
        end_value = end.timestamp()
        self._selection = (min(start_value, end_value), max(start_value, end_value))
        self._selection = self._clamp_selection(self._selection)
        self.selection_changed.emit(self.selection_datetime_range())
        self.update()

    def clear_selection(self) -> None:
        self._selection = None
        self.selection_changed.emit(None)
        self.update()

    def selection_datetime_range(self) -> tuple[datetime, datetime] | None:
        if self._selection is None:
            return None
        start, end = self._selection
        return datetime.fromtimestamp(start), datetime.fromtimestamp(end)

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        if delta == 0:
            return
        if self._main_graph_mode:
            event.ignore()
            return
        if event.modifiers() & Qt.ShiftModifier:
            self._pan(0.5 if delta > 0 else -0.5)
        else:
            self._zoom_at_x(event.position().x(), 0.7 if delta > 0 else 1 / 0.7)
        event.accept()

    def mousePressEvent(self, event) -> None:
        if self._main_graph_mode:
            event.ignore()
            return
        if self._is_pan_drag_event(event):
            self._start_pan_drag(event.position().x())
            event.accept()
            return
        if event.button() != Qt.LeftButton:
            return
        time_value = self._time_at_x(event.position().x())
        if time_value is None:
            return
        self._drag_started_at = time_value
        self._selection = (time_value, time_value)
        self.update()

    def mouseMoveEvent(self, event) -> None:
        if self._pan_drag_anchor_x is not None:
            self._pan_drag_to_x(event.position().x())
            event.accept()
            return
        if self._drag_started_at is None:
            return
        time_value = self._time_at_x(event.position().x())
        if time_value is None:
            return
        self._selection = (
            min(self._drag_started_at, time_value),
            max(self._drag_started_at, time_value),
        )
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        if self._pan_drag_anchor_x is not None:
            self._finish_pan_drag()
            event.accept()
            return
        if event.button() != Qt.LeftButton or self._drag_started_at is None:
            return
        time_value = self._time_at_x(event.position().x())
        if time_value is not None:
            self._selection = (
                min(self._drag_started_at, time_value),
                max(self._drag_started_at, time_value),
            )
        self._drag_started_at = None
        self.selection_changed.emit(self.selection_datetime_range())
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setFont(self.font())
        painter.fillRect(self.rect(), QColor("#ffffff"))

        plot_rect = self._plot_rect()
        self._last_plot_rect = plot_rect
        painter.setPen(QPen(QColor("#c9d1d9"), 1))
        painter.drawRect(plot_rect)

        if not self._series:
            painter.setPen(QColor("#6b7280"))
            painter.drawText(self.rect(), Qt.AlignCenter, "실시간 그래프")
            return

        self._draw_grid(painter, plot_rect)
        self._draw_selection(painter, plot_rect)
        self._draw_annotations(painter, plot_rect)

        if len(self._series) == 1:
            self._draw_single_series(painter, plot_rect, self._series[0])
        else:
            self._draw_multi_series(painter, plot_rect)

        self._draw_overview(painter, plot_rect)
        self._draw_time_axis_labels(painter, plot_rect)

    def _plot_rect(self) -> QRect:
        left = 64 if len(self._series) > 1 else 42
        return self.rect().adjusted(left, 12, -14, -30)

    def _draw_grid(self, painter: QPainter, rect: QRect) -> None:
        painter.setPen(QPen(QColor("#e5e7eb"), 1))
        for ratio in (0.25, 0.5, 0.75):
            y = rect.bottom() - rect.height() * ratio
            painter.drawLine(rect.left(), int(y), rect.right(), int(y))

    def _draw_single_series(self, painter: QPainter, rect: QRect, series: TimelineSeries) -> None:
        visible = self._visible_points_for_series(series)
        successful = [point for point in visible if point.success and point.latency_ms is not None]
        max_latency = max((point.latency_ms or 0 for point in successful), default=1)
        max_latency = max(max_latency, 1)

        painter.setPen(QColor("#4b5563"))
        painter.drawText(4, rect.top() + 10, f"{max_latency:.0f} ms")
        painter.drawText(10, rect.bottom(), "0 ms")

        self._draw_failure_markers(painter, series, rect, rect.top() + 2, rect.bottom())
        line_points: list[QPointF] = []
        for point in visible:
            x = self._x_for_time(point.timestamp.timestamp(), rect)
            if x is None:
                continue
            if point.success and point.latency_ms is not None:
                y = rect.bottom() - (rect.height() * min(point.latency_ms, max_latency) / max_latency)
                line_points.append(QPointF(x, y))

        if len(line_points) >= 2:
            painter.setPen(QPen(_series_qcolor(series, 0), 2))
            for start, end in zip(line_points, line_points[1:]):
                painter.drawLine(start, end)

    def _draw_multi_series(self, painter: QPainter, rect: QRect) -> None:
        visible_by_series = [(series, self._visible_points_for_series(series)) for series in self._series]
        all_successful = [
            point
            for _series, points in visible_by_series
            for point in points
            if point.success and point.latency_ms is not None
        ]
        max_latency = max((point.latency_ms or 0 for point in all_successful), default=1)
        max_latency = max(max_latency, 1)
        lane_height = rect.height() / max(len(visible_by_series), 1)

        for index, (series, points) in enumerate(visible_by_series):
            top = rect.top() + index * lane_height
            bottom = top + lane_height - 4
            mid = (top + bottom) / 2
            painter.setPen(QColor("#4b5563"))
            painter.drawText(4, int(mid + 4), _short_label(series.label))
            painter.setPen(QPen(QColor("#edf0f4"), 1))
            painter.drawLine(rect.left(), int(bottom), rect.right(), int(bottom))

            self._draw_failure_markers(painter, series, rect, top + 2, bottom - 2)
            line_points: list[QPointF] = []
            for point in points:
                x = self._x_for_time(point.timestamp.timestamp(), rect)
                if x is None:
                    continue
                if point.success and point.latency_ms is not None:
                    y = bottom - ((bottom - top) * min(point.latency_ms, max_latency) / max_latency)
                    line_points.append(QPointF(x, y))

            if len(line_points) >= 2:
                painter.setPen(QPen(_series_qcolor(series, index), 2))
                for start, end in zip(line_points, line_points[1:]):
                    painter.drawLine(start, end)

    def _draw_failure_markers(
        self,
        painter: QPainter,
        series: TimelineSeries,
        rect: QRect,
        top: float,
        bottom: float,
    ) -> None:
        visible_start, visible_end = self._visible_range()
        if visible_start is None or visible_end is None:
            return
        for start, end in self._failure_marker_spans_for_series(series, visible_start, visible_end):
            start_x = self._x_for_time(start, rect)
            end_x = self._x_for_time(end, rect)
            if start_x is None or end_x is None:
                continue
            if start == end:
                _draw_failure_bar(painter, start_x, top, bottom)
            else:
                _draw_failure_region(painter, start_x, end_x, top, bottom)

    def _draw_selection(self, painter: QPainter, rect: QRect) -> None:
        if self._selection is None:
            return
        start, end = self._selection
        x1 = self._x_for_time(start, rect)
        x2 = self._x_for_time(end, rect)
        if x1 is None or x2 is None:
            return
        left = int(min(x1, x2))
        width = max(int(abs(x2 - x1)), 1)
        painter.fillRect(QRect(left, rect.top(), width, rect.height()), QColor(37, 99, 235, 38))
        painter.setPen(QPen(QColor("#2563eb"), 1))
        painter.drawLine(left, rect.top(), left, rect.bottom())
        painter.drawLine(left + width, rect.top(), left + width, rect.bottom())

    def _draw_annotations(self, painter: QPainter, rect: QRect) -> None:
        for index, annotation in enumerate(self._annotations):
            midpoint = (annotation.start.timestamp() + annotation.end.timestamp()) / 2
            x = self._x_for_time(midpoint, rect)
            if x is None:
                continue
            label_top = rect.top() + 4 + (index % 4) * 20
            label_left = min(max(int(x) + 4, rect.left() + 2), rect.right() - 96)
            painter.setPen(QPen(QColor("#b45309"), 1))
            painter.drawLine(int(x), rect.top(), int(x), rect.bottom())
            painter.fillRect(QRect(label_left, label_top, 92, 18), QColor("#fffbeb"))
            painter.setPen(QColor("#92400e"))
            painter.drawText(label_left + 4, label_top + 14, annotation.label[:18])

    def _draw_overview(self, painter: QPainter, rect: QRect) -> None:
        full_start, full_end = self._full_time_range()
        visible_start, visible_end = self._visible_range()
        if (
            full_start is None
            or full_end is None
            or visible_start is None
            or visible_end is None
            or full_end <= full_start
        ):
            return
        overview_rect = QRect(rect.left(), self.rect().bottom() - 23, rect.width(), 7)
        painter.fillRect(overview_rect, QColor("#e5e7eb"))
        painter.setPen(QPen(QColor("#cbd5e1"), 1))
        painter.drawRect(overview_rect)

        span = full_end - full_start
        start_ratio = min(max((visible_start - full_start) / span, 0.0), 1.0)
        end_ratio = min(max((visible_end - full_start) / span, 0.0), 1.0)
        left = overview_rect.left() + overview_rect.width() * start_ratio
        right = overview_rect.left() + overview_rect.width() * end_ratio
        viewport = QRect(
            int(left),
            overview_rect.top(),
            max(int(right - left), 2),
            overview_rect.height(),
        )
        painter.fillRect(viewport, QColor("#2563eb"))

    def _draw_time_axis_labels(self, painter: QPainter, rect: QRect) -> None:
        left_label, right_label = self._time_axis_labels()
        bottom = self.rect().bottom()
        painter.setPen(QColor("#374151"))
        painter.drawText(QRect(rect.left(), bottom - 16, 180, 14), Qt.AlignLeft | Qt.AlignVCenter, left_label)
        painter.drawText(QRect(rect.right() - 180, bottom - 16, 180, 14), Qt.AlignRight | Qt.AlignVCenter, right_label)

    def _time_axis_labels(self) -> tuple[str, str]:
        visible_start, visible_end = self._visible_range()
        left_prefix = "시작" if self._time_axis_mode == TIME_AXIS_MODE_ALL else "최근"
        if visible_start is None or visible_end is None:
            return left_prefix, "현재"
        if self._external_visible_range is not None:
            start = datetime.fromtimestamp(visible_start)
            end = datetime.fromtimestamp(visible_end)
            return f"{left_prefix} {start.strftime('%H:%M:%S')}", f"현재 {end.strftime('%H:%M:%S')}"
        visible_timestamps = [
            point.timestamp
            for series in self._series
            for point in self._visible_points_for_series(series)
        ]
        if visible_timestamps:
            start = min(visible_timestamps)
            end = max(visible_timestamps)
        else:
            start = datetime.fromtimestamp(visible_start)
            end = datetime.fromtimestamp(visible_end)
        return f"{left_prefix} {start.strftime('%H:%M:%S')}", f"현재 {end.strftime('%H:%M:%S')}"

    def _visible_points(self, points: list[HopObservation]) -> list[HopObservation]:
        visible_start, visible_end = self._visible_range()
        if visible_start is None or visible_end is None:
            return points
        return [
            point
            for point in points
            if visible_start <= point.timestamp.timestamp() <= visible_end
        ]

    def _visible_points_for_series(self, series: TimelineSeries) -> list[HopObservation]:
        visible_start, visible_end = self._visible_range()
        cache_key = (series.key, visible_start, visible_end)
        cached = self._visible_points_cache.get(cache_key)
        if cached is not None:
            return cached
        visible = self._visible_points(series.points)
        self._visible_points_cache[cache_key] = visible
        return visible

    def _failure_marker_spans_for_series(
        self,
        series: TimelineSeries,
        visible_start: float,
        visible_end: float,
    ) -> list[tuple[float, float]]:
        cache_key = (series.key, visible_start, visible_end)
        cached = self._failure_spans_cache.get(cache_key)
        if cached is not None:
            return cached
        spans = _failure_marker_spans(series.points, visible_start, visible_end)
        self._failure_spans_cache[cache_key] = spans
        return spans

    def _visible_range(self) -> tuple[float | None, float | None]:
        if self._external_visible_range is not None:
            return self._external_visible_range
        full_start, full_end = self._full_time_range()
        if full_start is None or full_end is None:
            return None, None
        full_span = max(full_end - full_start, 1.0)
        visible_span = min(max(full_span * self._zoom_fraction, 1.0), full_span)
        visible_end = self._view_end_timestamp if self._view_end_timestamp is not None else full_end
        visible_end = min(max(visible_end, full_start + visible_span), full_end)
        return visible_end - visible_span, visible_end

    def _full_time_range(self) -> tuple[float | None, float | None]:
        if self._full_time_range_cache is not None:
            return self._full_time_range_cache
        timestamps = [
            point.timestamp.timestamp()
            for series in self._series
            for point in series.points
        ]
        if not timestamps:
            self._full_time_range_cache = (None, None)
            return self._full_time_range_cache
        self._full_time_range_cache = (min(timestamps), max(timestamps))
        return self._full_time_range_cache

    def _pan(self, direction: float) -> None:
        visible_start, visible_end = self._visible_range()
        full_start, full_end = self._full_time_range()
        if (
            visible_start is None
            or visible_end is None
            or full_start is None
            or full_end is None
            or full_end <= full_start
        ):
            return
        visible_span = max(visible_end - visible_start, 1.0)
        if visible_span >= full_end - full_start:
            return
        current_end = self._view_end_timestamp if self._view_end_timestamp is not None else full_end
        next_end = current_end + (visible_span * direction)
        next_end = min(max(next_end, full_start + visible_span), full_end)
        self._view_end_timestamp = None if next_end >= full_end else next_end
        self._clear_visible_render_caches()
        self.update()

    def _zoom_at_x(self, x: float | None, factor: float) -> None:
        visible_start, visible_end = self._visible_range()
        full_start, full_end = self._full_time_range()
        if (
            visible_start is None
            or visible_end is None
            or full_start is None
            or full_end is None
            or full_end <= full_start
        ):
            return
        old_span = max(visible_end - visible_start, 1.0)
        anchor_time = self._time_at_x(x) if x is not None else None
        anchor_ratio = 1.0
        if anchor_time is not None:
            anchor_ratio = min(max((anchor_time - visible_start) / old_span, 0.0), 1.0)

        self._zoom_fraction = min(max(self._zoom_fraction * factor, 0.1), 1.0)
        full_span = max(full_end - full_start, 1.0)
        visible_span = min(max(full_span * self._zoom_fraction, 1.0), full_span)
        if anchor_time is not None:
            desired_end = anchor_time + visible_span * (1.0 - anchor_ratio)
            self._view_end_timestamp = self._clamp_view_end(desired_end)
        else:
            self._view_end_timestamp = self._clamp_view_end(self._view_end_timestamp)
        self._clear_visible_render_caches()
        self.update()

    def _is_pan_drag_event(self, event) -> bool:
        if event.button() == Qt.RightButton:
            return True
        return event.button() == Qt.LeftButton and bool(event.modifiers() & Qt.AltModifier)

    def _start_pan_drag(self, x: float) -> None:
        visible_start, visible_end = self._visible_range()
        full_start, full_end = self._full_time_range()
        if (
            visible_start is None
            or visible_end is None
            or full_start is None
            or full_end is None
            or visible_end - visible_start >= full_end - full_start
        ):
            return
        self._pan_drag_anchor_x = x
        self._pan_drag_anchor_view_end = self._view_end_timestamp if self._view_end_timestamp is not None else full_end

    def _pan_drag_to_x(self, x: float) -> None:
        if self._pan_drag_anchor_x is None or self._pan_drag_anchor_view_end is None:
            return
        visible_start, visible_end = self._visible_range()
        full_start, full_end = self._full_time_range()
        rect = self._last_plot_rect if not self._last_plot_rect.isNull() else self._plot_rect()
        if (
            visible_start is None
            or visible_end is None
            or full_start is None
            or full_end is None
            or rect.width() <= 0
        ):
            return
        seconds_per_pixel = max(visible_end - visible_start, 1.0) / rect.width()
        pixel_delta = x - self._pan_drag_anchor_x
        next_end = self._pan_drag_anchor_view_end - pixel_delta * seconds_per_pixel
        self._view_end_timestamp = self._clamp_view_end(next_end)
        self._clear_visible_render_caches()
        self.update()

    def _finish_pan_drag(self) -> None:
        self._pan_drag_anchor_x = None
        self._pan_drag_anchor_view_end = None

    def _clamp_view_end(self, value: float | None) -> float | None:
        if value is None:
            return None
        full_start, full_end = self._full_time_range()
        if full_start is None or full_end is None:
            return None
        full_span = max(full_end - full_start, 1.0)
        visible_span = min(max(full_span * self._zoom_fraction, 1.0), full_span)
        clamped = min(max(value, full_start + visible_span), full_end)
        return None if clamped >= full_end else clamped

    def _x_for_time(self, value: float, rect: QRect) -> float | None:
        visible_start, visible_end = self._visible_range()
        if visible_start is None or visible_end is None:
            return None
        if value < visible_start or value > visible_end:
            return None
        span = max(visible_end - visible_start, 1.0)
        return rect.left() + rect.width() * ((value - visible_start) / span)

    def _time_at_x(self, x: float) -> float | None:
        rect = self._last_plot_rect if not self._last_plot_rect.isNull() else self._plot_rect()
        if not rect.contains(int(x), rect.center().y()):
            return None
        visible_start, visible_end = self._visible_range()
        if visible_start is None or visible_end is None:
            return None
        ratio = min(max((x - rect.left()) / max(rect.width(), 1), 0.0), 1.0)
        return visible_start + (visible_end - visible_start) * ratio

    def _clear_render_caches(self) -> None:
        self._full_time_range_cache = None
        self._clear_visible_render_caches()

    def _clear_visible_render_caches(self) -> None:
        self._visible_points_cache.clear()
        self._failure_spans_cache.clear()

    def _clamp_selection(self, selection: tuple[float, float] | None) -> tuple[float, float] | None:
        if selection is None:
            return None
        full_start, full_end = self._full_time_range()
        if full_start is None or full_end is None:
            return None
        start, end = selection
        start = min(max(start, full_start), full_end)
        end = min(max(end, full_start), full_end)
        return min(start, end), max(start, end)


def _short_label(label: str) -> str:
    return label if len(label) <= 10 else f"{label[:9]}..."


def series_color_hex(index: int) -> str:
    return SERIES_COLORS[index % len(SERIES_COLORS)]


def _series_qcolor(series: TimelineSeries, index: int) -> QColor:
    return QColor(series.color or series_color_hex(index))


def _series_color(index: int) -> QColor:
    return QColor(series_color_hex(index))


def _is_failure_observation(point: HopObservation) -> bool:
    return not point.success and point.status in FAILURE_STATUSES


def _failure_runs(points: list[HopObservation]) -> list[list[HopObservation]]:
    runs: list[list[HopObservation]] = []
    current: list[HopObservation] = []
    for point in points:
        if _is_failure_observation(point):
            current.append(point)
            continue
        if current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    return runs


def _failure_marker_spans(
    points: list[HopObservation],
    visible_start: float,
    visible_end: float,
) -> list[tuple[float, float]]:
    if visible_end < visible_start:
        visible_start, visible_end = visible_end, visible_start
    latest_point = points[-1] if points else None
    spans: list[tuple[float, float]] = []
    for run in _failure_runs(points):
        run_start = run[0].timestamp.timestamp()
        run_end = run[-1].timestamp.timestamp()
        if run_end < visible_start or run_start > visible_end:
            continue
        if len(run) == 1:
            if visible_start <= run_start <= visible_end:
                spans.append((run_start, run_start))
            continue

        start = max(run_start, visible_start)
        end = min(run_end, visible_end)
        if latest_point is not None and run[-1] == latest_point:
            end = visible_end
        if end < visible_start or start > visible_end:
            continue
        spans.append((start, max(start, end)))
    return spans


def _draw_failure_bar(painter: QPainter, x: float, top: float, bottom: float) -> None:
    y1 = int(min(top, bottom))
    y2 = int(max(top, bottom))
    if y2 <= y1:
        return
    painter.setPen(QPen(QColor(FAILURE_BAR_COLOR), 3))
    painter.drawLine(int(x), y1, int(x), y2)


def _draw_failure_region(painter: QPainter, start_x: float, end_x: float, top: float, bottom: float) -> None:
    y1 = int(min(top, bottom))
    y2 = int(max(top, bottom))
    if y2 <= y1:
        return
    left = int(min(start_x, end_x))
    right = int(max(start_x, end_x))
    if right <= left:
        _draw_failure_bar(painter, start_x, top, bottom)
        return
    fill = QColor(FAILURE_BAR_COLOR)
    fill.setAlpha(FAILURE_REGION_ALPHA)
    painter.fillRect(QRect(left, y1, max(right - left, 1), y2 - y1), fill)
    painter.setPen(QPen(QColor(FAILURE_BAR_COLOR), 2))
    painter.drawLine(left, y1, left, y2)
    painter.drawLine(right, y1, right, y2)


def _downsample_points(points: list[HopObservation], limit: int) -> list[HopObservation]:
    if len(points) <= limit:
        return list(points)
    if limit <= 2:
        return [points[0], points[-1]]
    step = (len(points) - 1) / (limit - 1)
    indexes = sorted({round(index * step) for index in range(limit)})
    return [points[index] for index in indexes]
