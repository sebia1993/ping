from __future__ import annotations

from datetime import datetime, timedelta

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QPushButton

from app.core.models import STATUS_OK, STATUS_TIMEOUT, HopObservation, MetricSnapshot
from app.ui.graph_detail_window import (
    GraphDetailWindow,
    VIEW_ALL_HOPS,
    VIEW_SELECTED_HOP,
    VIEW_VISIBLE_HOPS,
    summarize_points,
)
from app.ui.latency_graph import LatencyGraphWidget, TimelineAnnotation, TimelineSeries


def test_latency_graph_supports_zoom_selection_and_annotations(qt_app) -> None:
    graph = LatencyGraphWidget()
    now = datetime.now()
    points = [
        HopObservation(now + timedelta(seconds=index), 0, "198.51.100.10", "Target", True, float(index), STATUS_OK, True)
        for index in range(10)
    ]

    graph.set_series([TimelineSeries("target", "Target", points)])
    graph.zoom_in()
    graph.select_time_range(points[2].timestamp, points[5].timestamp)
    graph.set_annotations([TimelineAnnotation(points[2].timestamp, points[5].timestamp, "장애")])

    assert graph._zoom_fraction < 1.0
    assert graph.selection_datetime_range() is not None
    assert len(graph._annotations) == 1

    graph.zoom_out()
    graph.reset_zoom()

    assert graph._zoom_fraction == 1.0


def test_latency_graph_downsamples_long_series_without_losing_time_bounds(qt_app) -> None:
    graph = LatencyGraphWidget()
    now = datetime(2026, 1, 1, 12, 0, 0)
    points = [
        HopObservation(now + timedelta(seconds=index), 0, "198.51.100.10", "Target", True, 1.0, STATUS_OK, True)
        for index in range(2000)
    ]

    graph.set_series([TimelineSeries("target", "Target", points)])

    assert len(graph._series[0].points) <= 1200
    assert graph._series[0].points[0].timestamp == points[0].timestamp
    assert graph._series[0].points[-1].timestamp == points[-1].timestamp


def test_latency_graph_pans_visible_range_and_resets_current(qt_app) -> None:
    graph = LatencyGraphWidget()
    now = datetime(2026, 1, 1, 12, 0, 0)
    points = [
        HopObservation(now + timedelta(seconds=index), 0, "198.51.100.10", "Target", True, 1.0, STATUS_OK, True)
        for index in range(120)
    ]

    graph.set_series([TimelineSeries("target", "Target", points)])
    graph.zoom_in()
    current_range = graph.visible_datetime_range()
    assert current_range is not None

    graph.pan_left()
    earlier_range = graph.visible_datetime_range()
    assert earlier_range is not None
    assert earlier_range[1] < current_range[1]

    graph.pan_right()
    later_range = graph.visible_datetime_range()
    assert later_range is not None
    assert later_range[1] > earlier_range[1]

    graph.reset_to_current()
    reset_range = graph.visible_datetime_range()
    assert reset_range is not None
    assert reset_range[1] == points[-1].timestamp


def test_latency_graph_zoom_keeps_cursor_time_anchored(qt_app) -> None:
    graph = LatencyGraphWidget()
    graph.resize(800, 240)
    now = datetime(2026, 1, 1, 12, 0, 0)
    points = [
        HopObservation(now + timedelta(seconds=index), 0, "198.51.100.10", "Target", True, 1.0, STATUS_OK, True)
        for index in range(120)
    ]

    graph.set_series([TimelineSeries("target", "Target", points)])
    plot_rect = graph._plot_rect()
    anchor_x = plot_rect.left() + plot_rect.width() * 0.35
    anchor_time = graph._time_at_x(anchor_x)
    assert anchor_time is not None

    graph._zoom_at_x(anchor_x, 0.7)
    anchored_x = graph._x_for_time(anchor_time, graph._plot_rect())

    assert anchored_x is not None
    assert abs(anchored_x - anchor_x) <= 1.0


def test_latency_graph_right_drag_pans_history_without_selection(qt_app) -> None:
    graph = LatencyGraphWidget()
    graph.resize(800, 240)
    now = datetime(2026, 1, 1, 12, 0, 0)
    points = [
        HopObservation(now + timedelta(seconds=index), 0, "198.51.100.10", "Target", True, 1.0, STATUS_OK, True)
        for index in range(120)
    ]

    graph.set_series([TimelineSeries("target", "Target", points)])
    graph.zoom_in()
    current_range = graph.visible_datetime_range()
    assert current_range is not None
    plot_rect = graph._plot_rect()
    start_pos = QPointF(plot_rect.center().x(), plot_rect.center().y())
    end_pos = QPointF(plot_rect.center().x() + 120, plot_rect.center().y())

    graph.mousePressEvent(
        QMouseEvent(
            QEvent.Type.MouseButtonPress,
            start_pos,
            Qt.MouseButton.RightButton,
            Qt.MouseButton.RightButton,
            Qt.KeyboardModifier.NoModifier,
        )
    )
    graph.mouseMoveEvent(
        QMouseEvent(
            QEvent.Type.MouseMove,
            end_pos,
            Qt.MouseButton.NoButton,
            Qt.MouseButton.RightButton,
            Qt.KeyboardModifier.NoModifier,
        )
    )
    graph.mouseReleaseEvent(
        QMouseEvent(
            QEvent.Type.MouseButtonRelease,
            end_pos,
            Qt.MouseButton.RightButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
    )

    dragged_range = graph.visible_datetime_range()
    assert dragged_range is not None
    assert dragged_range[1] < current_range[1]
    assert graph.selection_datetime_range() is None


def test_graph_detail_switches_between_target_all_hops_and_selected_hop(qt_app) -> None:
    detail = GraphDetailWindow()
    now = datetime.now()
    target_history = [
        HopObservation(now + timedelta(seconds=index), 0, "198.51.100.10", "Target", True, 20.0, STATUS_OK, True)
        for index in range(3)
    ]
    observations = [
        HopObservation(now, 1, "192.0.2.1", "gateway", True, 2.0, STATUS_OK),
        HopObservation(now + timedelta(seconds=1), 1, "192.0.2.1", "gateway", False, None, STATUS_TIMEOUT),
        HopObservation(now, 2, "198.51.100.10", "target", True, 20.0, STATUS_OK, True),
    ]
    snapshots = [
        _snapshot(1, "192.0.2.1", "gateway"),
        _snapshot(2, "198.51.100.10", "target", is_target=True),
    ]

    try:
        detail.set_data("198.51.100.10", snapshots[-1], target_history, observations, snapshots, 1)
        assert detail.graph._series[0].key == "target"

        detail.view_combo.setCurrentIndex(detail.view_combo.findData(VIEW_ALL_HOPS))
        assert [series.key for series in detail.graph._series] == ["hop-1", "hop-2"]

        detail.set_selected_hop_index(1)
        assert detail.view_combo.currentData() == VIEW_SELECTED_HOP
        assert [series.key for series in detail.graph._series] == ["hop-1"]

        detail.graph.select_time_range(now, now + timedelta(seconds=1))
        detail.annotation_input.setText("확인필요")
        detail.add_annotation_from_selection()

        assert len(detail._annotations) == 1
        assert "loss 50.0%" in detail.range_summary_label.text()
    finally:
        detail.close()


def test_graph_detail_toggles_visible_hop_series(qt_app) -> None:
    detail = GraphDetailWindow()
    now = datetime.now()
    observations = [
        HopObservation(now, 1, "192.0.2.1", "gateway", True, 2.0, STATUS_OK),
        HopObservation(now + timedelta(seconds=1), 1, "192.0.2.1", "gateway", True, 3.0, STATUS_OK),
        HopObservation(now, 2, "198.51.100.1", "edge", True, 8.0, STATUS_OK),
        HopObservation(now + timedelta(seconds=1), 2, "198.51.100.1", "edge", False, None, STATUS_TIMEOUT),
    ]
    snapshots = [
        _snapshot(1, "192.0.2.1", "gateway"),
        _snapshot(2, "198.51.100.1", "edge"),
    ]

    try:
        detail.set_data("198.51.100.10", None, [], observations, snapshots, 1)
        detail.view_combo.setCurrentIndex(detail.view_combo.findData(VIEW_VISIBLE_HOPS))

        assert [series.key for series in detail.graph._series] == ["hop-1"]
        assert detail._hop_checkboxes[1].isChecked() is True
        assert detail._hop_checkboxes[2].isChecked() is False

        detail._hop_checkboxes[2].click()

        assert detail.view_combo.currentData() == VIEW_VISIBLE_HOPS
        assert [series.key for series in detail.graph._series] == ["hop-1", "hop-2"]
        assert detail.graph._series[0].color != detail.graph._series[1].color

        detail._hop_checkboxes[1].click()

        assert [series.key for series in detail.graph._series] == ["hop-2"]
    finally:
        detail.close()


def test_graph_detail_emits_focus_range_from_selection(qt_app) -> None:
    detail = GraphDetailWindow()
    now = datetime.now()
    history = [
        HopObservation(now + timedelta(seconds=index), 0, "198.51.100.10", "Target", True, 20.0, STATUS_OK, True)
        for index in range(3)
    ]
    applied: list[object] = []

    try:
        detail.focus_applied.connect(applied.append)
        detail.set_data("198.51.100.10", _snapshot(0, "198.51.100.10", None, is_target=True), history)
        detail.graph.select_time_range(now, now + timedelta(seconds=2))
        detail.apply_focus_from_selection()

        assert applied
    finally:
        detail.close()


def test_graph_detail_saves_selected_range_png(qt_app, tmp_path) -> None:
    detail = GraphDetailWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    history = [
        HopObservation(now + timedelta(seconds=index), 0, "198.51.100.10", "Target", True, 20.0 + index, STATUS_OK, True)
        for index in range(5)
    ]
    path = tmp_path / "selected_range.png"

    try:
        detail.resize(900, 620)
        detail.set_data("198.51.100.10", _snapshot(0, "198.51.100.10", None, is_target=True), history)
        detail.graph.select_time_range(now + timedelta(seconds=1), now + timedelta(seconds=3))
        detail.annotation_input.setText("evidence")
        detail.add_annotation_from_selection()
        detail.show()
        qt_app.processEvents()

        saved = detail.save_png(path)

        assert saved == path
        assert path.exists()
        assert path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        assert "PNG saved:" in detail.timeline_status_label.text()
    finally:
        detail.close()


def test_graph_detail_time_scale_buttons_emit_seconds(qt_app) -> None:
    detail = GraphDetailWindow()
    requested: list[int] = []

    try:
        detail.timeline_range_requested.connect(requested.append)
        buttons = {button.text(): button for button in detail.findChildren(QPushButton)}
        buttons["10m"].click()
        buttons["24h"].click()

        assert requested == [600, 86400]
    finally:
        detail.close()


def test_graph_detail_timeline_navigation_buttons_move_visible_window(qt_app) -> None:
    detail = GraphDetailWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    history = [
        HopObservation(now + timedelta(seconds=index), 0, "198.51.100.10", "Target", True, 20.0, STATUS_OK, True)
        for index in range(120)
    ]

    try:
        detail.set_data("198.51.100.10", _snapshot(0, "198.51.100.10", None, is_target=True), history)
        detail.graph.zoom_in()
        current_range = detail.graph.visible_datetime_range()
        assert current_range is not None

        buttons = {button.text(): button for button in detail.findChildren(QPushButton)}
        buttons["Prev"].click()
        previous_range = detail.graph.visible_datetime_range()

        assert previous_range is not None
        assert previous_range[1] < current_range[1]

        buttons["Current"].click()
        latest_range = detail.graph.visible_datetime_range()

        assert latest_range is not None
        assert latest_range[1] == history[-1].timestamp
    finally:
        detail.close()


def test_summarize_points_counts_loss_and_latency() -> None:
    now = datetime.now()
    points = [
        HopObservation(now, 1, "192.0.2.1", None, True, 2.0, STATUS_OK),
        HopObservation(now + timedelta(seconds=1), 1, "192.0.2.1", None, False, None, STATUS_TIMEOUT),
    ]

    summary = summarize_points(points)

    assert summary.samples == 2
    assert summary.timeout_count == 1
    assert summary.loss_percent == 50.0
    assert summary.avg_latency_ms == 2.0
    assert summary.max_latency_ms == 2.0


def _snapshot(
    hop_index: int,
    address: str,
    hostname: str | None,
    *,
    is_target: bool = False,
) -> MetricSnapshot:
    return MetricSnapshot(
        hop_index=hop_index,
        address=address,
        hostname=hostname,
        samples=1,
        sent=1,
        received=1,
        timeout_count=0,
        current_latency_ms=10.0,
        avg_latency_ms=10.0,
        min_latency_ms=10.0,
        max_latency_ms=10.0,
        loss_percent=0.0,
        recent_loss_percent=0.0,
        jitter_ms=None,
        status=STATUS_OK,
        is_target=is_target,
    )
