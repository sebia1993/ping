from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QAbstractItemView, QSizePolicy, QTableWidget, QTableWidgetItem

from app.core.models import STATUS_PAUSED, MetricSnapshot


SESSION_ID_ROLE = Qt.UserRole + 1

TABLE_HEADERS = [
    "Hop",
    "주소",
    "호스트명",
    "상태",
    "현재",
    "평균",
    "최소",
    "최대",
    "손실률 %",
    "최근 손실률 %",
    "응답 없음",
    "지연 변동",
    "샘플",
]

TARGET_HEADERS = [
    "IP 주소",
    "상태",
    "현재 지연",
    "평균",
    "최소",
    "최대",
    "손실률 %",
    "샘플",
    "응답",
    "실패",
]

TARGET_HEADERS.extend(["Interval", "Interval Source"])
TARGET_HEADERS.append("Score")
TARGET_INTERVAL_COLUMN = TARGET_HEADERS.index("Interval")
TARGET_INTERVAL_SOURCE_COLUMN = TARGET_HEADERS.index("Interval Source")
TARGET_SCORE_COLUMN = len(TARGET_HEADERS) - 1

SESSION_HEADERS = [
    "상태",
    "대상",
    "시작",
    "종료",
    "샘플",
    "주기",
    "모드",
    "엔진",
    "포트",
    "대상 수",
    "세그먼트",
]

ALERT_HEADERS = [
    "시간",
    "심각도",
    "제목",
    "시작",
    "종료",
    "대상",
    "동작",
    "메시지",
]


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


def create_alert_table() -> QTableWidget:
    table = QTableWidget(0, len(ALERT_HEADERS))
    table.setHorizontalHeaderLabels(ALERT_HEADERS)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setSelectionMode(QAbstractItemView.SingleSelection)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.verticalHeader().setVisible(False)
    table.setAlternatingRowColors(True)
    table.setMaximumHeight(150)
    table.setSortingEnabled(True)
    table.sortItems(0, Qt.DescendingOrder)
    return table


def create_session_table() -> QTableWidget:
    table = QTableWidget(0, len(SESSION_HEADERS))
    table.setHorizontalHeaderLabels(SESSION_HEADERS)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setSelectionMode(QAbstractItemView.SingleSelection)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.verticalHeader().setVisible(False)
    table.setAlternatingRowColors(True)
    table.setMaximumHeight(150)
    table.setSortingEnabled(True)
    table.sortItems(2, Qt.DescendingOrder)
    return table


def populate_trace_table(table: QTableWidget, hops: object) -> None:
    hop_list = list(hops)
    table.setRowCount(len(hop_list))
    for row, hop in enumerate(hop_list):
        values = [
            hop.index,
            hop.address or "응답 없음",
            hop.hostname or "",
            "응답 없음" if hop.timed_out else "대기",
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
            snapshot.address or "응답 없음",
            snapshot.hostname or "",
            _status_display_text(display_status(snapshot)),
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


def update_target_table(
    table: QTableWidget,
    snapshots: list[MetricSnapshot],
    *,
    interval_seconds_by_target: dict[str, int | None] | None = None,
    interval_source_by_target: dict[str, str] | None = None,
) -> None:
    interval_seconds_by_target = interval_seconds_by_target or {}
    interval_source_by_target = interval_source_by_target or {}
    sorting_enabled = table.isSortingEnabled()
    table.setSortingEnabled(False)
    rows_changed = table.rowCount() != len(snapshots)
    if rows_changed:
        table.setRowCount(len(snapshots))
    for row, snapshot in enumerate(snapshots):
        failed = snapshot.sent - snapshot.received
        score = target_problem_score(snapshot)
        address = snapshot.address or ""
        interval_seconds = interval_seconds_by_target.get(address)
        interval_source = interval_source_by_target.get(address, "")
        values = [
            (address, address),
            (_status_display_text(display_status(snapshot)), score),
            (fmt_ms(snapshot.current_latency_ms), snapshot.current_latency_ms if snapshot.current_latency_ms is not None else -1),
            (fmt_ms(snapshot.avg_latency_ms), snapshot.avg_latency_ms if snapshot.avg_latency_ms is not None else -1),
            (fmt_ms(snapshot.min_latency_ms), snapshot.min_latency_ms if snapshot.min_latency_ms is not None else -1),
            (fmt_ms(snapshot.max_latency_ms), snapshot.max_latency_ms if snapshot.max_latency_ms is not None else -1),
            (f"{snapshot.loss_percent:.1f}", snapshot.loss_percent),
            (snapshot.sent, snapshot.sent),
            (snapshot.received, snapshot.received),
            (failed, failed),
            ("" if interval_seconds is None else f"{interval_seconds}s", interval_seconds or 0),
            (interval_source, interval_source),
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


def update_session_table(table: QTableWidget, sessions: object) -> None:
    session_list = list(sessions)
    sorting_enabled = table.isSortingEnabled()
    table.setSortingEnabled(False)
    rows_changed = table.rowCount() != len(session_list)
    table.setRowCount(len(session_list))
    for row, session in enumerate(session_list):
        end = session.end.strftime("%Y-%m-%d %H:%M:%S") if session.end is not None else "실행 중"
        values = [
            (_session_state_label(session.state), _session_state_sort_key(session.state)),
            (session.target, session.target),
            (session.start.strftime("%Y-%m-%d %H:%M:%S"), session.start.timestamp()),
            (end, session.end.timestamp() if session.end is not None else float("inf")),
            (session.samples, session.samples),
            (session.interval_seconds or "", session.interval_seconds or 0),
            (_session_mode_label(session), _session_mode_sort_key(session)),
            (_session_probe_label(session), _session_probe_sort_key(session)),
            (_session_tcp_port_label(session), _session_tcp_port_sort_key(session)),
            (session.target_count, session.target_count),
            (len(session.segments), len(session.segments)),
        ]
        for column, (value, sort_value) in enumerate(values):
            item = table.item(row, column)
            if item is None:
                item = SortableTableWidgetItem()
                table.setItem(row, column, item)
            item.setText(str(value))
            item.setData(Qt.UserRole, sort_value)
            item.setData(SESSION_ID_ROLE, session.session_id)
            item.setToolTip(_session_tooltip(session))
    if rows_changed:
        table.resizeColumnsToContents()
    table.setSortingEnabled(sorting_enabled)


def update_alert_table(
    table: QTableWidget,
    events: object,
    actions_by_key: dict[str, list[str]] | None = None,
) -> None:
    event_list = list(events)
    actions_by_key = actions_by_key or {}
    sorting_enabled = table.isSortingEnabled()
    table.setSortingEnabled(False)
    rows_changed = table.rowCount() != len(event_list)
    table.setRowCount(len(event_list))
    for row, event in enumerate(event_list):
        actions = actions_by_key.get(event.key, [])
        actions_text = ", ".join(_alert_action_text(action) for action in actions) if actions else "-"
        values = [
            (event.timestamp.strftime("%Y-%m-%d %H:%M:%S"), event.timestamp.timestamp()),
            (_alert_severity_text(event.severity), _alert_severity_sort_key(event.severity)),
            (event.title, event.title),
            (event.start.strftime("%Y-%m-%d %H:%M:%S"), event.start.timestamp()),
            (event.end.strftime("%Y-%m-%d %H:%M:%S"), event.end.timestamp()),
            (event.series_key or "route", event.series_key or ""),
            (actions_text, actions_text),
            (event.message, event.message),
        ]
        color = alert_row_color(event.severity)
        tooltip = f"{event.title}: {event.message}"
        for column, (value, sort_value) in enumerate(values):
            item = table.item(row, column)
            if item is None:
                item = SortableTableWidgetItem()
                table.setItem(row, column, item)
            item.setText(str(value))
            item.setData(Qt.UserRole, sort_value)
            item.setBackground(color)
            item.setToolTip(tooltip)
    if rows_changed:
        table.resizeColumnsToContents()
    table.setSortingEnabled(sorting_enabled)


def _session_state_sort_key(state: str) -> int:
    return {
        "Active": 0,
        "Archived": 1,
        "Pause": 2,
        "Will Delete": 3,
    }.get(state, 99)


def _session_state_label(state: str) -> str:
    return {
        "Active": "실행 중",
        "Archived": "보관됨",
        "Pause": "일시중지",
        "Will Delete": "삭제 예정",
    }.get(state, state or "-")


def _session_mode_label(session: object) -> str:
    mode = _session_mode_value(session)
    return {
        "full_route": "전체 경로",
        "final_hop_only": "최종 IP만",
    }.get(mode, mode or "-")


def _session_probe_label(session: object) -> str:
    probe_engine = _session_probe_value(session)
    return {
        "icmp": "ICMP",
        "tcp_connect": "TCP 연결",
    }.get(probe_engine, probe_engine or "-")


def _session_tcp_port_label(session: object) -> str:
    tcp_port = _session_tcp_port_value(session)
    return str(tcp_port) if tcp_port is not None else "-"


def _session_mode_sort_key(session: object) -> str:
    return _session_mode_value(session)


def _session_probe_sort_key(session: object) -> str:
    return _session_probe_value(session)


def _session_tcp_port_sort_key(session: object) -> int:
    return _session_tcp_port_value(session) or 0


def _session_mode_value(session: object) -> str:
    measurement_mode = str(getattr(session, "measurement_mode", "") or "")
    parts = [part for part in measurement_mode.split(":") if part]
    return parts[0] if parts else measurement_mode


def _session_probe_value(session: object) -> str:
    probe_engine = str(getattr(session, "probe_engine", "") or "")
    if probe_engine:
        return probe_engine
    measurement_mode = str(getattr(session, "measurement_mode", "") or "")
    return next((part for part in measurement_mode.split(":") if part in {"icmp", "tcp_connect"}), "")


def _session_tcp_port_value(session: object) -> int | None:
    tcp_port = getattr(session, "tcp_port", None)
    if tcp_port not in (None, ""):
        return int(tcp_port)
    measurement_mode = str(getattr(session, "measurement_mode", "") or "")
    for part in measurement_mode.split(":"):
        if not part.startswith("port"):
            continue
        try:
            return int(part.removeprefix("port"))
        except ValueError:
            return None
    return None


def _session_tooltip(session: object) -> str:
    lines: list[str] = []
    last_error = str(getattr(session, "last_error", "") or "")
    if last_error:
        lines.append(last_error)
    resumed_from = str(getattr(session, "resumed_from_session_id", "") or "")
    if resumed_from:
        lines.append(f"원본 세션: {resumed_from}")
    return "\n".join(lines)


def _alert_severity_sort_key(severity: str) -> int:
    return {
        "critical": 0,
        "warning": 1,
        "info": 2,
    }.get(severity.casefold(), 99)


def _alert_severity_text(severity: str) -> str:
    return {
        "critical": "심각",
        "warning": "주의",
        "info": "정보",
    }.get(severity.casefold(), severity.upper())


def _alert_action_text(action: str) -> str:
    return {
        "timeline_annotation": "타임라인",
        "comment": "코멘트",
        "log": "로그",
        "beep": "소리",
        "image": "이미지",
        "email": "이메일",
        "email_failed": "이메일 실패",
        "rest": "REST",
        "rest_failed": "REST 실패",
        "executable": "실행파일",
        "executable_failed": "실행파일 실패",
        "route_adjustment": "경로 조정",
    }.get(action, action)


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


def _status_display_text(status: object) -> str:
    text = str(status or "")
    return {
        "OK": "정상",
        "WARNING": "주의",
        "CRITICAL": "장애",
        "PAUSED": "일시중지",
        "TIMEOUT": "응답 없음",
        "UNREACHABLE": "도달 불가",
        "ERROR": "오류",
        "NO_PING_TARGET": "대상 없음",
        "WAITING": "대기",
    }.get(text, text or "-")


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


def alert_row_color(severity: str) -> QColor:
    normalized = severity.casefold()
    if normalized == "critical":
        return QColor("#fee2e2")
    if normalized == "warning":
        return QColor("#fef3c7")
    if normalized == "info":
        return QColor("#e0f2fe")
    return QColor("#ffffff")


class SortableTableWidgetItem(QTableWidgetItem):
    def __lt__(self, other: QTableWidgetItem) -> bool:
        left = self.data(Qt.UserRole)
        right = other.data(Qt.UserRole)
        try:
            return float(left) < float(right)
        except (TypeError, ValueError):
            return str(left) < str(right)
