from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QAbstractItemView, QSizePolicy, QTableWidget, QTableWidgetItem

from app.core.models import STATUS_PAUSED, MetricSnapshot


TABLE_HEADERS = [
    "Hop",
    "Address",
    "Hostname",
    "Status",
    "Current",
    "Avg",
    "Min",
    "Max",
    "Loss %",
    "Recent Loss %",
    "Timeout",
    "Jitter",
    "Samples",
]

TARGET_HEADERS = [
    "대상IP",
    "상태",
    "현재",
    "평균",
    "최소",
    "최대",
    "손실률",
    "송신",
    "수신",
    "실패",
]


TARGET_HEADERS.append("Score")
TARGET_SCORE_COLUMN = len(TARGET_HEADERS) - 1


def create_hop_table() -> QTableWidget:
    table = QTableWidget(0, len(TABLE_HEADERS))
    table.setHorizontalHeaderLabels(TABLE_HEADERS)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.verticalHeader().setVisible(False)
    table.setAlternatingRowColors(True)
    table.setObjectName("hopTable")
    table.setMinimumHeight(340)
    table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
    return table


def create_target_table() -> QTableWidget:
    table = QTableWidget(0, len(TARGET_HEADERS))
    table.setHorizontalHeaderLabels(TARGET_HEADERS)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setSelectionMode(QAbstractItemView.ExtendedSelection)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.verticalHeader().setVisible(False)
    table.setAlternatingRowColors(True)
    table.setMinimumHeight(120)
    table.setSortingEnabled(False)
    table.hideColumn(TARGET_SCORE_COLUMN)
    return table


def populate_trace_table(table: QTableWidget, hops: object) -> None:
    hop_list = list(hops)
    table.setRowCount(len(hop_list))
    for row, hop in enumerate(hop_list):
        values = [
            hop.index,
            hop.address or "Timeout",
            hop.hostname or "",
            "TIMEOUT" if hop.timed_out else "WAITING",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "0",
        ]
        for column, value in enumerate(values):
            table.setItem(row, column, QTableWidgetItem(str(value)))
    table.resizeColumnsToContents()


def update_hop_table(table: QTableWidget, snapshots: list[MetricSnapshot]) -> None:
    row_by_hop = {
        int(table.item(row, 0).text()): row
        for row in range(table.rowCount())
        if table.item(row, 0)
    }
    for snapshot in snapshots:
        row = row_by_hop.get(snapshot.hop_index)
        if row is None:
            continue
        values = [
            snapshot.hop_index,
            snapshot.address or "Timeout",
            snapshot.hostname or "",
            display_status(snapshot),
            fmt_ms(snapshot.current_latency_ms),
            fmt_ms(snapshot.avg_latency_ms),
            fmt_ms(snapshot.min_latency_ms),
            fmt_ms(snapshot.max_latency_ms),
            f"{snapshot.loss_percent:.1f}",
            f"{snapshot.recent_loss_percent:.1f}",
            snapshot.timeout_count,
            fmt_ms(snapshot.jitter_ms),
            snapshot.samples,
        ]
        for column, value in enumerate(values):
            item = table.item(row, column)
            if item is None:
                item = QTableWidgetItem()
                table.setItem(row, column, item)
            item.setText(str(value))
            item.setBackground(row_color(snapshot))


def update_target_table(table: QTableWidget, snapshots: list[MetricSnapshot]) -> None:
    sorting_enabled = table.isSortingEnabled()
    table.setSortingEnabled(False)
    rows_changed = table.rowCount() != len(snapshots)
    if rows_changed:
        table.setRowCount(len(snapshots))
    for row, snapshot in enumerate(snapshots):
        failed = snapshot.sent - snapshot.received
        score = target_problem_score(snapshot)
        values = [
            (snapshot.address or "", snapshot.address or ""),
            (display_status(snapshot), score),
            (fmt_ms(snapshot.current_latency_ms), snapshot.current_latency_ms if snapshot.current_latency_ms is not None else -1),
            (fmt_ms(snapshot.avg_latency_ms), snapshot.avg_latency_ms if snapshot.avg_latency_ms is not None else -1),
            (fmt_ms(snapshot.min_latency_ms), snapshot.min_latency_ms if snapshot.min_latency_ms is not None else -1),
            (fmt_ms(snapshot.max_latency_ms), snapshot.max_latency_ms if snapshot.max_latency_ms is not None else -1),
            (f"{snapshot.loss_percent:.1f}", snapshot.loss_percent),
            (snapshot.sent, snapshot.sent),
            (snapshot.received, snapshot.received),
            (failed, failed),
            (f"{score:.3f}", score),
        ]
        for column, (value, sort_value) in enumerate(values):
            item = table.item(row, column)
            if item is None:
                item = SortableTableWidgetItem()
                table.setItem(row, column, item)
            item.setText(str(value))
            item.setData(Qt.UserRole, sort_value)
            item.setBackground(row_color(snapshot))
    if rows_changed:
        table.resizeColumnsToContents()
        table.hideColumn(TARGET_SCORE_COLUMN)
    table.setSortingEnabled(sorting_enabled)


def fmt_ms(value: float | None) -> str:
    return "" if value is None else f"{value:.1f}"


def display_status(snapshot: MetricSnapshot) -> str:
    if snapshot.status == STATUS_PAUSED:
        return STATUS_PAUSED
    if snapshot.loss_percent >= 20:
        return "CRITICAL"
    if snapshot.loss_percent >= 5 or (snapshot.jitter_ms is not None and snapshot.jitter_ms >= 30):
        return "WARNING"
    return snapshot.status


def target_problem_score(snapshot: MetricSnapshot) -> float:
    if snapshot.status == STATUS_PAUSED:
        return -1.0
    failed = snapshot.sent - snapshot.received
    jitter = snapshot.jitter_ms or 0.0
    current = snapshot.current_latency_ms or 0.0
    return (
        max(snapshot.loss_percent, snapshot.recent_loss_percent) * 1000
        + failed * 100
        + jitter
        + current / 1000
    )


def row_color(snapshot: MetricSnapshot) -> QColor:
    if snapshot.status == STATUS_PAUSED:
        return QColor("#e5e7eb")
    if snapshot.loss_percent >= 20:
        return QColor("#fee2e2")
    if snapshot.loss_percent >= 5 or (snapshot.jitter_ms is not None and snapshot.jitter_ms >= 30):
        return QColor("#fef3c7")
    return QColor("#ffffff")


class SortableTableWidgetItem(QTableWidgetItem):
    def __lt__(self, other: QTableWidgetItem) -> bool:
        left = self.data(Qt.UserRole)
        right = other.data(Qt.UserRole)
        try:
            return float(left) < float(right)
        except (TypeError, ValueError):
            return str(left) < str(right)
