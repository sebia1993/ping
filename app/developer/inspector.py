from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QComboBox,
    QGroupBox,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QRubberBand,
    QTextEdit,
    QWidget,
)

from app.developer.models import UNKNOWN_VALUE, display_value
from app.developer.registry import DeveloperRegistry, UiMetadata


@dataclass(frozen=True)
class UiSelection:
    widget: QWidget
    values: dict[str, str]


class UiInspector(QObject):
    selection_changed = Signal(object)

    def __init__(self, main_window: QWidget, registry: DeveloperRegistry, *, panel_provider) -> None:
        super().__init__(main_window)
        self.main_window = main_window
        self.registry = registry
        self.panel_provider = panel_provider
        self.active = False
        self.inspection_enabled = False
        self.selected_widget: QWidget | None = None
        self.hovered_widget: QWidget | None = None
        self._event_filter_installed = False
        self._suppressing_click = False
        self._hover_timer = QTimer(self)
        self._hover_timer.setInterval(75)
        self._hover_timer.timeout.connect(self._update_hovered_widget)
        self._rubber_band = QRubberBand(QRubberBand.Rectangle, main_window)
        self._rubber_band.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._rubber_band.setStyleSheet("border: 2px solid #dc2626; background: rgba(220, 38, 38, 20);")
        self._id_label = QLabel(main_window)
        self._id_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._id_label.setStyleSheet(
            "background: #111827; color: white; border: 1px solid #dc2626; "
            "padding: 3px 6px; font-size: 11px;"
        )
        self._id_label.hide()

    def start(self) -> None:
        if self.active:
            return
        self.active = True
        app = QApplication.instance()
        if app is not None and not self._event_filter_installed:
            app.installEventFilter(self)
            self._event_filter_installed = True
        self._sync_timer()

    def stop(self) -> None:
        self.active = False
        self.inspection_enabled = False
        self._hover_timer.stop()
        app = QApplication.instance()
        if app is not None and self._event_filter_installed:
            app.removeEventFilter(self)
        self._event_filter_installed = False
        self._suppressing_click = False
        self.clear_selection()
        self._hide_highlight()

    def set_inspection_enabled(self, enabled: bool) -> None:
        self.inspection_enabled = bool(enabled and self.active)
        self._suppressing_click = False
        self._sync_timer()
        if not self.inspection_enabled:
            self.hovered_widget = None
            self._hide_highlight()

    def eventFilter(self, watched, event) -> bool:
        if not self.active or not self.inspection_enabled:
            return False
        event_type = event.type()
        if event_type == QEvent.KeyPress and event.key() == Qt.Key_Escape:
            self.clear_selection()
            return True
        if event_type not in {
            QEvent.MouseButtonPress,
            QEvent.MouseButtonRelease,
            QEvent.MouseButtonDblClick,
        }:
            return False
        if not isinstance(watched, QWidget) or self._is_panel_widget(watched):
            return False
        if event.button() != Qt.LeftButton:
            return False
        if event_type == QEvent.MouseButtonPress:
            candidate = self._selection_candidate(watched)
            if candidate is not None:
                self.select_widget(candidate)
                self._suppressing_click = True
                return True
        if self._suppressing_click:
            if event_type == QEvent.MouseButtonRelease:
                self._suppressing_click = False
            return True
        return False

    def select_widget(self, widget: QWidget | None) -> None:
        if widget is None or self._is_panel_widget(widget):
            return
        self.selected_widget = widget
        self.hovered_widget = widget
        values = inspect_widget(widget, self.main_window, self.registry)
        self._show_highlight(widget, values)
        self.selection_changed.emit(UiSelection(widget=widget, values=values))

    def clear_selection(self) -> None:
        self.selected_widget = None
        self.hovered_widget = None
        self._hide_highlight()
        self.selection_changed.emit(None)

    def select_parent(self) -> None:
        widget = self.selected_widget
        if widget is None:
            return
        parent = widget.parentWidget()
        while parent is not None and self._is_panel_widget(parent):
            parent = parent.parentWidget()
        if parent is not None:
            self.select_widget(parent)

    def selectable_children(self) -> list[QWidget]:
        if self.selected_widget is None:
            return []
        return [
            child
            for child in self.selected_widget.findChildren(QWidget, options=Qt.FindDirectChildrenOnly)
            if not self._is_panel_widget(child)
        ]

    def _sync_timer(self) -> None:
        if self.active and self.inspection_enabled:
            if not self._hover_timer.isActive():
                self._hover_timer.start()
        else:
            self._hover_timer.stop()

    def _update_hovered_widget(self) -> None:
        if not self.active or not self.inspection_enabled:
            return
        widget = QApplication.widgetAt(QCursor.pos())
        if not isinstance(widget, QWidget) or self._is_panel_widget(widget):
            if self.selected_widget is None:
                self._hide_highlight()
            return
        candidate = self._selection_candidate(widget)
        if candidate is None:
            return
        self.hovered_widget = candidate
        values = inspect_widget(candidate, self.main_window, self.registry)
        self._show_highlight(candidate, values)

    def _selection_candidate(self, widget: QWidget) -> QWidget | None:
        current: QWidget | None = widget
        nearest_registered: QWidget | None = None
        while current is not None and current is not self.main_window:
            if self._is_panel_widget(current):
                return None
            if isinstance(current, QAbstractButton):
                return current
            if nearest_registered is None and self.registry.metadata_for(current) is not None:
                nearest_registered = current
            current = current.parentWidget()
        return nearest_registered or widget

    def _is_panel_widget(self, widget: QWidget) -> bool:
        panel = self.panel_provider()
        return bool(panel is not None and (widget is panel or panel.isAncestorOf(widget)))

    def _show_highlight(self, widget: QWidget, values: dict[str, str]) -> None:
        if not widget.isVisible():
            self._hide_highlight()
            return
        top_left = widget.mapTo(self.main_window, QPoint(0, 0))
        rectangle = QRect(top_left, widget.size()).intersected(self.main_window.rect())
        if rectangle.isEmpty():
            self._hide_highlight()
            return
        self._rubber_band.setGeometry(rectangle)
        self._rubber_band.show()
        self._rubber_band.raise_()
        developer_id = values.get("developer_id") or ""
        if developer_id == "미등록":
            developer_id = values.get("temporary_path") or developer_id
        developer_id = developer_id or UNKNOWN_VALUE
        self._id_label.setText(developer_id)
        self._id_label.adjustSize()
        label_x = max(0, min(rectangle.left(), self.main_window.width() - self._id_label.width()))
        panel = self.panel_provider()
        if panel is not None and panel.isVisible():
            panel_top_left = panel.mapTo(self.main_window, QPoint(0, 0))
            panel_rectangle = QRect(panel_top_left, panel.size())
            proposed_label = QRect(label_x, 0, self._id_label.width(), self._id_label.height())
            if proposed_label.right() >= panel_rectangle.left() and rectangle.left() < panel_rectangle.left():
                label_x = max(0, panel_rectangle.left() - self._id_label.width() - 4)
        label_y = rectangle.top() - self._id_label.height() - 2
        if label_y < 0:
            label_y = min(self.main_window.height() - self._id_label.height(), rectangle.bottom() + 2)
        self._id_label.move(label_x, max(0, label_y))
        self._id_label.show()
        self._id_label.raise_()

    def _hide_highlight(self) -> None:
        self._rubber_band.hide()
        self._id_label.hide()


def inspect_widget(
    widget: QWidget,
    main_window: QWidget,
    registry: DeveloperRegistry,
    *,
    include_current_value: bool = False,
) -> dict[str, str]:
    metadata = registry.metadata_for(widget)
    temporary_path = temporary_widget_path(widget, main_window)
    developer_id = metadata.developer_id if metadata is not None else ""
    parent = widget.parentWidget()
    parent_id = registry.developer_id_for(parent) or (temporary_widget_path(parent, main_window) if parent else "")
    position = widget.mapTo(main_window, QPoint(0, 0))
    layout = widget.layout()
    parent_layout = parent.layout() if parent is not None else None
    alignment = ""
    if parent_layout is not None:
        index = parent_layout.indexOf(widget)
        if index >= 0:
            item = parent_layout.itemAt(index)
            alignment = _alignment_text(item.alignment()) if item is not None else ""
    margins = layout.contentsMargins() if layout is not None else None
    values = {
        "screen_name": metadata.screen_name if metadata is not None else "main_dashboard",
        "developer_id": developer_id or "미등록",
        "temporary_path": temporary_path,
        "ui_type": type(widget).__name__,
        "display_text": display_widget_text(widget),
        "parent_id": display_value(parent_id),
        "child_count": str(len(widget.findChildren(QWidget, options=Qt.FindDirectChildrenOnly))),
        "hierarchy": widget_hierarchy(widget, main_window, registry),
        "x": str(position.x()),
        "y": str(position.y()),
        "width": str(widget.width()),
        "height": str(widget.height()),
        "minimum_size": f"{widget.minimumWidth()} x {widget.minimumHeight()}",
        "maximum_size": _maximum_size_text(widget),
        "alignment": display_value(alignment),
        "layout_type": type(layout).__name__ if layout is not None else "없음",
        "margins": (
            f"왼쪽 {margins.left()}, 위 {margins.top()}, 오른쪽 {margins.right()}, 아래 {margins.bottom()}"
            if margins is not None
            else "없음"
        ),
        "spacing": str(layout.spacing()) if layout is not None else "없음",
        "visible": "예" if widget.isVisible() else "아니요",
        "enabled": "예" if widget.isEnabled() else "아니요",
        "selected": _selected_state(widget),
        "focused": "예" if widget.hasFocus() else "아니요",
        "read_only": _read_only_state(widget),
        "component_class": metadata.component_class if metadata is not None else "",
        "source_file": metadata.source_file if metadata is not None else "",
        "creation_method": metadata.creation_method if metadata is not None else "",
        "event_handler": metadata.event_handler if metadata is not None else "",
        "service_function": metadata.service_function if metadata is not None else "",
        "style_file": metadata.style_file if metadata is not None else "",
        "settings_key": metadata.settings_key if metadata is not None else "",
        "feature_id": metadata.feature_id if metadata is not None else "",
    }
    if include_current_value:
        current_value = current_widget_value(widget)
        if current_value:
            values["current_value"] = current_value
    return values


def display_widget_text(widget: QWidget) -> str:
    if isinstance(widget, (QAbstractButton, QLabel)):
        return display_value(widget.text())
    if isinstance(widget, QGroupBox):
        return display_value(widget.title())
    if isinstance(widget, QComboBox):
        return display_value(widget.currentText())
    if isinstance(widget, QLineEdit):
        return display_value(widget.placeholderText() or widget.accessibleName())
    if isinstance(widget, (QPlainTextEdit, QTextEdit)):
        return display_value(widget.placeholderText() or widget.accessibleName())
    return display_value(widget.accessibleName() or widget.windowTitle())


def current_widget_value(widget: QWidget) -> str:
    if isinstance(widget, QLineEdit):
        if widget.echoMode() != QLineEdit.Normal:
            return "[MASKED_SECRET]"
        return widget.text()
    if isinstance(widget, QComboBox):
        return widget.currentText()
    if isinstance(widget, (QPlainTextEdit, QTextEdit)):
        return widget.toPlainText()
    if isinstance(widget, QAbstractButton) and widget.isCheckable():
        return "선택됨" if widget.isChecked() else "선택 안 됨"
    return ""


def temporary_widget_path(widget: QWidget | None, main_window: QWidget) -> str:
    if widget is None:
        return ""
    parts: list[str] = []
    current: QWidget | None = widget
    while current is not None:
        object_name = current.objectName().strip()
        if object_name:
            token = object_name
        else:
            token = _class_token(current)
        parts.append(token)
        if current is main_window:
            break
        current = current.parentWidget()
    return " > ".join(reversed(parts))


def widget_hierarchy(widget: QWidget, main_window: QWidget, registry: DeveloperRegistry) -> str:
    parts: list[str] = []
    current: QWidget | None = widget
    while current is not None:
        metadata = registry.metadata_for(current)
        parts.append(metadata.developer_id if metadata is not None else type(current).__name__)
        if current is main_window:
            break
        current = current.parentWidget()
    return " > ".join(reversed(parts))


def format_inspection(values: dict[str, str]) -> str:
    developer_id = values.get("developer_id") or "미등록"
    if developer_id == "미등록":
        developer_id = f"미등록\n임시 경로: {values.get('temporary_path', UNKNOWN_VALUE)}"
    lines = [
        "[기본 정보]",
        f"화면 이름: {values.get('screen_name', UNKNOWN_VALUE)}",
        f"개발자 ID: {developer_id}",
        f"UI 종류: {values.get('ui_type', UNKNOWN_VALUE)}",
        f"표시 문구: {values.get('display_text', UNKNOWN_VALUE)}",
        f"상위 요소 ID: {values.get('parent_id', UNKNOWN_VALUE)}",
        f"하위 요소 수: {values.get('child_count', '0')}",
        f"UI 계층 경로: {values.get('hierarchy', UNKNOWN_VALUE)}",
        "",
        "[위치 및 크기]",
        f"X: {values.get('x', UNKNOWN_VALUE)}",
        f"Y: {values.get('y', UNKNOWN_VALUE)}",
        f"너비: {values.get('width', UNKNOWN_VALUE)}",
        f"높이: {values.get('height', UNKNOWN_VALUE)}",
        f"최소 크기: {values.get('minimum_size', UNKNOWN_VALUE)}",
        f"최대 크기: {values.get('maximum_size', UNKNOWN_VALUE)}",
        f"정렬 방식: {values.get('alignment', UNKNOWN_VALUE)}",
        f"레이아웃 종류: {values.get('layout_type', UNKNOWN_VALUE)}",
        f"여백: {values.get('margins', UNKNOWN_VALUE)}",
        f"간격: {values.get('spacing', UNKNOWN_VALUE)}",
        "",
        "[상태]",
        f"표시 여부: {values.get('visible', UNKNOWN_VALUE)}",
        f"활성화 여부: {values.get('enabled', UNKNOWN_VALUE)}",
        f"선택 여부: {values.get('selected', UNKNOWN_VALUE)}",
        f"포커스 여부: {values.get('focused', UNKNOWN_VALUE)}",
        f"읽기 전용 여부: {values.get('read_only', UNKNOWN_VALUE)}",
        "",
        "[코드 연결 정보]",
        f"관련 클래스: {display_value(values.get('component_class'))}",
        f"소스 파일: {display_value(values.get('source_file'))}",
        f"생성 함수: {display_value(values.get('creation_method'))}",
        f"클릭 이벤트 함수: {display_value(values.get('event_handler'))}",
        f"관련 서비스 함수: {display_value(values.get('service_function'))}",
        f"관련 스타일 파일: {display_value(values.get('style_file'))}",
        f"관련 설정 키: {display_value(values.get('settings_key'))}",
        f"연결된 기능 ID: {display_value(values.get('feature_id'))}",
    ]
    if "current_value" in values:
        lines.extend(["", "[사용자 선택 포함]", f"현재 입력값: {display_value(values.get('current_value'))}"])
    return "\n".join(lines)


def _class_token(widget: QWidget) -> str:
    parent = widget.parentWidget()
    class_name = type(widget).__name__
    if parent is None:
        return class_name
    siblings = [child for child in parent.findChildren(QWidget, options=Qt.FindDirectChildrenOnly) if type(child) is type(widget)]
    try:
        index = siblings.index(widget) + 1
    except ValueError:
        index = 1
    return f"{class_name}[{index}]"


def _maximum_size_text(widget: QWidget) -> str:
    width = "제한 없음" if widget.maximumWidth() >= 16_777_215 else str(widget.maximumWidth())
    height = "제한 없음" if widget.maximumHeight() >= 16_777_215 else str(widget.maximumHeight())
    return f"{width} x {height}"


def _alignment_text(alignment) -> str:
    names = []
    checks = (
        (Qt.AlignLeft, "왼쪽"),
        (Qt.AlignRight, "오른쪽"),
        (Qt.AlignHCenter, "가운데"),
        (Qt.AlignTop, "위"),
        (Qt.AlignBottom, "아래"),
        (Qt.AlignVCenter, "세로 가운데"),
    )
    for flag, label in checks:
        if alignment & flag:
            names.append(label)
    return ", ".join(names)


def _selected_state(widget: QWidget) -> str:
    checked = getattr(widget, "isChecked", None)
    if callable(checked):
        return "예" if checked() else "아니요"
    return "해당 없음"


def _read_only_state(widget: QWidget) -> str:
    read_only = getattr(widget, "isReadOnly", None)
    if callable(read_only):
        return "예" if read_only() else "아니요"
    return "해당 없음"
