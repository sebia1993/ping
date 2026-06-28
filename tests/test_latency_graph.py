from __future__ import annotations

import csv
from datetime import datetime, timedelta

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QPixmap
from PySide6.QtWidgets import QPushButton

from app.core.models import (
    STATUS_ERROR,
    STATUS_OK,
    STATUS_PAUSED,
    STATUS_TIMEOUT,
    STATUS_UNREACHABLE,
    HopObservation,
    MetricSnapshot,
)
from app.storage import atomic_write as atomic_write_module
from app.ui.graph_detail_window import (
    GraphDetailWindow,
    VIEW_ALL_HOPS,
    VIEW_SELECTED_HOP,
    VIEW_VISIBLE_HOPS,
    summarize_points,
)
from app.ui.latency_graph import (
    LatencyGraphWidget,
    TimelineAnnotation,
    TimelineSeries,
    _failure_marker_spans,
    _failure_runs,
    _is_failure_observation,
)


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


def test_latency_graph_reuses_and_clears_render_caches(qt_app) -> None:
    graph = LatencyGraphWidget()
    now = datetime(2026, 1, 1, 12, 0, 0)
    points = [
        HopObservation(now + timedelta(seconds=index), 0, "198.51.100.10", "Target", True, 1.0, STATUS_OK, True)
        for index in range(20)
    ]

    graph.set_series([TimelineSeries("target", "Target", points)])
    series = graph._series[0]

    first_visible = graph._visible_points_for_series(series)
    second_visible = graph._visible_points_for_series(series)
    first_full_range = graph._full_time_range()
    second_full_range = graph._full_time_range()

    assert first_visible is second_visible
    assert first_full_range == second_full_range
    assert graph._visible_points_cache
    assert graph._full_time_range_cache == first_full_range

    graph.set_visible_time_range(now + timedelta(seconds=5), now + timedelta(seconds=10))

    assert graph._visible_points_cache == {}
    assert graph._failure_spans_cache == {}
    assert graph._full_time_range_cache == first_full_range
    assert graph._visible_points_for_series(series) == points[5:11]

    next_points = points[:5]
    graph.set_series([TimelineSeries("target", "Target", next_points)])

    assert graph._visible_points_cache == {}
    assert graph._failure_spans_cache == {}
    assert graph._full_time_range_cache is None


def test_latency_graph_renders_explicit_series_color(qt_app) -> None:
    graph = LatencyGraphWidget()
    graph.resize(360, 200)
    now = datetime(2026, 1, 1, 12, 0, 0)
    points = [
        HopObservation(
            now + timedelta(seconds=index),
            0,
            "198.51.100.10",
            "Target",
            True,
            10.0 + index * 8,
            STATUS_OK,
            True,
        )
        for index in range(8)
    ]

    graph.set_series([TimelineSeries("target", "Target", points, "#16a34a")])

    assert graph._series[0].color == "#16a34a"

    pixmap = QPixmap(graph.size())
    pixmap.fill(Qt.GlobalColor.white)
    graph.render(pixmap)
    image = pixmap.toImage()

    assert any(
        (pixel := image.pixelColor(x, y)).green() > 120
        and pixel.red() < 100
        and pixel.blue() < 130
        for x in range(image.width())
        for y in range(image.height())
    )


def test_latency_graph_time_axis_labels_use_visible_sample_times(qt_app) -> None:
    graph = LatencyGraphWidget()
    now = datetime(2026, 1, 1, 12, 0, 0)
    points = [
        HopObservation(now + timedelta(seconds=index), 0, "198.51.100.10", "Target", True, 1.0, STATUS_OK, True)
        for index in range(120)
    ]

    graph.set_series([TimelineSeries("target", "Target", points)])

    assert graph._time_axis_labels() == ("최근 12:00:00", "현재 12:01:59")


def test_latency_graph_time_axis_labels_use_start_prefix_for_all_range(qt_app) -> None:
    graph = LatencyGraphWidget()
    now = datetime(2026, 1, 1, 12, 0, 0)
    points = [
        HopObservation(now + timedelta(seconds=index), 0, "198.51.100.10", "Target", True, 1.0, STATUS_OK, True)
        for index in range(120)
    ]

    graph.set_time_axis_mode("all")
    graph.set_series([TimelineSeries("target", "Target", points)])

    assert graph._time_axis_labels() == ("시작 12:00:00", "현재 12:01:59")


def test_latency_graph_failure_marker_policy_only_marks_response_failures(qt_app) -> None:
    now = datetime(2026, 1, 1, 12, 0, 0)
    timeout = HopObservation(now, 0, "198.51.100.10", "Target", False, None, STATUS_TIMEOUT, True)
    unreachable = HopObservation(now, 0, "198.51.100.10", "Target", False, None, STATUS_UNREACHABLE, True)
    error = HopObservation(now, 0, "198.51.100.10", "Target", False, None, STATUS_ERROR, True)
    paused = HopObservation(now, 0, "198.51.100.10", "Target", False, None, STATUS_PAUSED, True)
    high_latency = HopObservation(now, 0, "198.51.100.10", "Target", True, 2000.0, STATUS_OK, True)

    assert _is_failure_observation(timeout) is True
    assert _is_failure_observation(unreachable) is True
    assert _is_failure_observation(error) is True
    assert _is_failure_observation(paused) is False
    assert _is_failure_observation(high_latency) is False


def test_latency_graph_groups_only_consecutive_response_failures(qt_app) -> None:
    now = datetime(2026, 1, 1, 12, 0, 0)
    points = [
        HopObservation(now, 0, "198.51.100.10", "Target", True, 10.0, STATUS_OK, True),
        HopObservation(now + timedelta(seconds=1), 0, "198.51.100.10", "Target", False, None, STATUS_TIMEOUT, True),
        HopObservation(now + timedelta(seconds=2), 0, "198.51.100.10", "Target", False, None, STATUS_UNREACHABLE, True),
        HopObservation(now + timedelta(seconds=3), 0, "198.51.100.10", "Target", True, 12.0, STATUS_OK, True),
        HopObservation(now + timedelta(seconds=4), 0, "198.51.100.10", "Target", False, None, STATUS_TIMEOUT, True),
        HopObservation(now + timedelta(seconds=5), 0, "198.51.100.10", "Target", False, None, STATUS_PAUSED, True),
        HopObservation(now + timedelta(seconds=6), 0, "198.51.100.10", "Target", False, None, STATUS_ERROR, True),
        HopObservation(now + timedelta(seconds=7), 0, "198.51.100.10", "Target", False, None, STATUS_ERROR, True),
    ]

    runs = _failure_runs(points)

    assert [[point.timestamp.second for point in run] for run in runs] == [[1, 2], [4], [6, 7]]


def test_latency_graph_failure_region_extends_to_live_visible_end(qt_app) -> None:
    now = datetime(2026, 1, 1, 12, 0, 0)
    points = [
        HopObservation(now + timedelta(seconds=index), 0, "198.51.100.10", "Target", False, None, STATUS_TIMEOUT, True)
        for index in range(3)
    ]
    visible_start = now.timestamp()
    visible_end = (now + timedelta(seconds=10)).timestamp()

    spans = _failure_marker_spans(points, visible_start, visible_end)

    assert spans == [(visible_start, visible_end)]


def test_latency_graph_failure_region_extends_to_left_visible_boundary(qt_app) -> None:
    now = datetime(2026, 1, 1, 12, 0, 0)
    points = [
        HopObservation(now + timedelta(seconds=index), 0, "198.51.100.10", "Target", False, None, STATUS_TIMEOUT, True)
        for index in range(5)
    ]
    visible_start = (now + timedelta(seconds=2, milliseconds=500)).timestamp()
    visible_end = (now + timedelta(seconds=4)).timestamp()

    spans = _failure_marker_spans(points, visible_start, visible_end)

    assert spans == [(visible_start, visible_end)]


def test_latency_graph_single_failure_remains_bar_marker(qt_app) -> None:
    now = datetime(2026, 1, 1, 12, 0, 0)
    failed_at = now + timedelta(seconds=1)
    points = [
        HopObservation(now, 0, "198.51.100.10", "Target", True, 10.0, STATUS_OK, True),
        HopObservation(failed_at, 0, "198.51.100.10", "Target", False, None, STATUS_TIMEOUT, True),
        HopObservation(now + timedelta(seconds=2), 0, "198.51.100.10", "Target", True, 11.0, STATUS_OK, True),
    ]

    spans = _failure_marker_spans(points, now.timestamp(), (now + timedelta(seconds=2)).timestamp())

    assert spans == [(failed_at.timestamp(), failed_at.timestamp())]


def test_latency_graph_continuous_total_loss_keeps_one_failure_region_when_window_moves(qt_app) -> None:
    now = datetime(2026, 1, 1, 12, 0, 0)
    points = [
        HopObservation(now + timedelta(seconds=index), 0, "198.51.100.10", "Target", False, None, STATUS_TIMEOUT, True)
        for index in range(120)
    ]
    first_window = (
        (now + timedelta(seconds=30)).timestamp(),
        (now + timedelta(seconds=119)).timestamp(),
    )
    next_window = (
        (now + timedelta(seconds=31)).timestamp(),
        (now + timedelta(seconds=120)).timestamp(),
    )

    first_spans = _failure_marker_spans(points, *first_window)
    next_spans = _failure_marker_spans(points, *next_window)

    assert first_spans == [first_window]
    assert next_spans == [next_window]


def test_latency_graph_renders_response_failures_without_crashing(qt_app) -> None:
    graph = LatencyGraphWidget()
    graph.resize(720, 260)
    now = datetime(2026, 1, 1, 12, 0, 0)
    points = [
        HopObservation(now, 0, "198.51.100.10", "Target", True, 10.0, STATUS_OK, True),
        HopObservation(now + timedelta(seconds=1), 0, "198.51.100.10", "Target", False, None, STATUS_TIMEOUT, True),
        HopObservation(now + timedelta(seconds=2), 0, "198.51.100.10", "Target", True, 12.0, STATUS_OK, True),
    ]

    graph.set_points(points)
    pixmap = QPixmap(graph.size())
    pixmap.fill(Qt.GlobalColor.white)
    graph.render(pixmap)

    assert pixmap.isNull() is False
    assert graph._last_plot_rect.height() > 100


def test_latency_graph_renders_consecutive_response_failures_without_crashing(qt_app) -> None:
    graph = LatencyGraphWidget()
    graph.resize(720, 260)
    now = datetime(2026, 1, 1, 12, 0, 0)
    points = [
        HopObservation(now, 0, "198.51.100.10", "Target", True, 10.0, STATUS_OK, True),
        HopObservation(now + timedelta(seconds=1), 0, "198.51.100.10", "Target", False, None, STATUS_TIMEOUT, True),
        HopObservation(now + timedelta(seconds=2), 0, "198.51.100.10", "Target", False, None, STATUS_TIMEOUT, True),
        HopObservation(now + timedelta(seconds=3), 0, "198.51.100.10", "Target", False, None, STATUS_TIMEOUT, True),
        HopObservation(now + timedelta(seconds=4), 0, "198.51.100.10", "Target", True, 12.0, STATUS_OK, True),
    ]

    graph.set_points(points)
    pixmap = QPixmap(graph.size())
    pixmap.fill(Qt.GlobalColor.white)
    graph.render(pixmap)

    assert pixmap.isNull() is False
    assert graph._last_plot_rect.height() > 100


def test_latency_graph_main_mode_ignores_wheel_without_time_pan(qt_app) -> None:
    graph = LatencyGraphWidget()
    now = datetime(2026, 1, 1, 12, 0, 0)
    points = [
        HopObservation(now + timedelta(seconds=index), 0, "198.51.100.10", "Target", True, 1.0, STATUS_OK, True)
        for index in range(120)
    ]
    requests: list[float] = []

    graph.set_series([TimelineSeries("target", "Target", points)])
    graph.set_main_graph_mode(True)
    graph.time_pan_requested.connect(requests.append)
    before_range = graph.visible_datetime_range()

    normal_wheel = _WheelEvent(120, Qt.KeyboardModifier.NoModifier)
    graph.wheelEvent(normal_wheel)

    assert normal_wheel.ignored is True
    assert graph.visible_datetime_range() == before_range
    assert requests == []

    shift_wheel = _WheelEvent(-120, Qt.KeyboardModifier.ShiftModifier)
    graph.wheelEvent(shift_wheel)

    assert shift_wheel.ignored is True
    assert requests == []


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
        assert "손실 50.0%" in detail.range_summary_label.text()
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
        assert "PNG 저장 완료:" in detail.timeline_status_label.text()
    finally:
        detail.close()


def test_graph_detail_png_preserves_existing_file_after_replace_failure(
    qt_app, tmp_path, monkeypatch
) -> None:
    detail = GraphDetailWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    history = [
        HopObservation(now + timedelta(seconds=index), 0, "198.51.100.10", "Target", True, 20.0 + index, STATUS_OK, True)
        for index in range(3)
    ]
    path = tmp_path / "selected_range.png"
    original_bytes = b"old png"
    path.write_bytes(original_bytes)
    original_replace = atomic_write_module._replace_path

    def locked_replace(source, target):
        if target == path:
            raise PermissionError("locked")
        return original_replace(source, target)

    monkeypatch.setattr(atomic_write_module, "EXPORT_IO_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(atomic_write_module, "_replace_path", locked_replace)

    try:
        detail.resize(900, 620)
        detail.set_data("198.51.100.10", _snapshot(0, "198.51.100.10", None, is_target=True), history)
        detail.show()
        qt_app.processEvents()

        try:
            detail.save_png(path)
        except PermissionError:
            pass
        else:
            raise AssertionError("expected PermissionError")

        assert path.read_bytes() == original_bytes
        assert not list(tmp_path.glob(f".{path.name}.*{path.suffix}"))
    finally:
        detail.close()


def test_graph_detail_png_button_reports_export_error_without_raising(
    qt_app, tmp_path, monkeypatch
) -> None:
    detail = GraphDetailWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    history = [
        HopObservation(now + timedelta(seconds=index), 0, "198.51.100.10", "Target", True, 20.0 + index, STATUS_OK, True)
        for index in range(3)
    ]
    path = tmp_path / "selected_range.png"
    original_bytes = b"old png"
    path.write_bytes(original_bytes)
    original_replace = atomic_write_module._replace_path

    def locked_replace(source, target):
        if target == path:
            raise PermissionError("locked")
        return original_replace(source, target)

    monkeypatch.setattr(atomic_write_module, "EXPORT_IO_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(atomic_write_module, "_replace_path", locked_replace)
    monkeypatch.setattr(detail, "_select_png_path", lambda: path)

    try:
        detail.resize(900, 620)
        detail.set_data("198.51.100.10", _snapshot(0, "198.51.100.10", None, is_target=True), history)
        detail.show()
        qt_app.processEvents()

        detail._save_png_from_button()

        assert path.read_bytes() == original_bytes
        assert "locked" in detail.timeline_status_label.text()
    finally:
        detail.close()


def test_graph_detail_saves_visible_csv_samples(qt_app, tmp_path) -> None:
    detail = GraphDetailWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    history = [
        HopObservation(
            now + timedelta(seconds=index),
            0,
            "198.51.100.10",
            "Target",
            True,
            20.0 + index,
            STATUS_OK,
            True,
        )
        for index in range(10)
    ]
    path = tmp_path / "visible.csv"

    try:
        detail.set_data("198.51.100.10", _snapshot(0, "198.51.100.10", None, is_target=True), history)
        detail.graph.zoom_in()

        saved = detail.save_visible_csv(path)

        assert saved == path
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
        assert rows[0] == [
            "series_key",
            "series_label",
            "timestamp",
            "address",
            "kind",
            "hop",
            "hostname",
            "success",
            "latency_ms",
            "status",
        ]
        assert [row[2] for row in rows[1:]] == [
            point.timestamp.isoformat(timespec="seconds") for point in history[3:]
        ]
        assert {row[0] for row in rows[1:]} == {"target"}
        assert "CSV 저장 완료:" in detail.timeline_status_label.text()
    finally:
        detail.close()


def test_graph_detail_visible_csv_preserves_existing_file_after_replace_failure(
    qt_app, tmp_path, monkeypatch
) -> None:
    detail = GraphDetailWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    history = [
        HopObservation(
            now + timedelta(seconds=index),
            0,
            "198.51.100.10",
            "Target",
            True,
            20.0 + index,
            STATUS_OK,
            True,
        )
        for index in range(3)
    ]
    path = tmp_path / "visible.csv"
    original_text = "old export\n"
    path.write_text(original_text, encoding="utf-8-sig")
    original_replace = atomic_write_module._replace_path

    def locked_replace(source, target):
        if target == path:
            raise PermissionError("locked")
        return original_replace(source, target)

    monkeypatch.setattr(atomic_write_module, "EXPORT_IO_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(atomic_write_module, "_replace_path", locked_replace)

    try:
        detail.set_data("198.51.100.10", _snapshot(0, "198.51.100.10", None, is_target=True), history)

        try:
            detail.save_visible_csv(path)
        except PermissionError:
            pass
        else:
            raise AssertionError("expected PermissionError")

        assert path.read_text(encoding="utf-8-sig") == original_text
        assert not list(tmp_path.glob(f".{path.name}.*{path.suffix}"))
    finally:
        detail.close()


def test_graph_detail_visible_csv_button_reports_export_error_without_raising(
    qt_app, tmp_path, monkeypatch
) -> None:
    detail = GraphDetailWindow()
    now = datetime(2026, 1, 1, 12, 0, 0)
    history = [
        HopObservation(
            now + timedelta(seconds=index),
            0,
            "198.51.100.10",
            "Target",
            True,
            20.0 + index,
            STATUS_OK,
            True,
        )
        for index in range(3)
    ]
    path = tmp_path / "visible.csv"
    original_text = "old export\n"
    path.write_text(original_text, encoding="utf-8-sig")
    original_replace = atomic_write_module._replace_path

    def locked_replace(source, target):
        if target == path:
            raise PermissionError("locked")
        return original_replace(source, target)

    monkeypatch.setattr(atomic_write_module, "EXPORT_IO_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(atomic_write_module, "_replace_path", locked_replace)
    monkeypatch.setattr(detail, "_select_csv_path", lambda: path)

    try:
        detail.set_data("198.51.100.10", _snapshot(0, "198.51.100.10", None, is_target=True), history)

        detail._save_visible_csv_from_button()

        assert path.read_text(encoding="utf-8-sig") == original_text
        assert "locked" in detail.timeline_status_label.text()
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
        buttons["48h"].click()

        assert requested == [600, 86400, 172800]
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
        buttons["이전"].click()
        previous_range = detail.graph.visible_datetime_range()

        assert previous_range is not None
        assert previous_range[1] < current_range[1]

        buttons["현재"].click()
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


class _WheelDelta:
    def __init__(self, value: int) -> None:
        self._value = value

    def y(self) -> int:
        return self._value


class _WheelEvent:
    def __init__(self, delta: int, modifiers: Qt.KeyboardModifier) -> None:
        self._delta = _WheelDelta(delta)
        self._modifiers = modifiers
        self.accepted = False
        self.ignored = False

    def angleDelta(self) -> _WheelDelta:
        return self._delta

    def modifiers(self) -> Qt.KeyboardModifier:
        return self._modifiers

    def position(self) -> QPointF:
        return QPointF(10, 10)

    def accept(self) -> None:
        self.accepted = True

    def ignore(self) -> None:
        self.ignored = True
