from __future__ import annotations

import weakref
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Callable


@dataclass(frozen=True)
class UiMetadata:
    developer_id: str
    screen_name: str = "main_dashboard"
    source_file: str = ""
    component_class: str = ""
    creation_method: str = ""
    event_handler: str = ""
    service_function: str = ""
    style_file: str = ""
    settings_key: str = ""
    feature_id: str = ""


@dataclass(frozen=True)
class FeatureMetadata:
    feature_id: str
    display_name: str
    description: str
    screen_name: str = "main_dashboard"
    ui_id: str = ""
    event_handler: str = ""
    service_function: str = ""
    source_file: str = ""
    settings_key: str = ""
    log_category: str = ""
    may_contain_sensitive_data: bool = False


@dataclass(frozen=True)
class RecentFeatureRun:
    feature_id: str
    executed_at: str
    status: str
    repeat_count: int = 1


class DeveloperRegistry:
    """Registry for stable UI and operator-facing feature identifiers."""

    def __init__(self, *, recent_limit: int = 20) -> None:
        self.enabled = False
        self._ui_metadata: dict[int, UiMetadata] = {}
        self._widget_references: dict[int, weakref.ReferenceType[object]] = {}
        self._object_ids_by_developer_id: dict[str, int] = {}
        self._features: dict[str, FeatureMetadata] = {}
        self._recent: deque[RecentFeatureRun] = deque(maxlen=max(1, recent_limit))
        self._recent_callbacks: list[Callable[[], None]] = []

    def register_ui(self, widget: object, metadata: UiMetadata) -> None:
        object_id = id(widget)
        previous_metadata = self._ui_metadata.get(object_id)
        if (
            previous_metadata is not None
            and previous_metadata.developer_id != metadata.developer_id
            and self._object_ids_by_developer_id.get(previous_metadata.developer_id) == object_id
        ):
            self._object_ids_by_developer_id.pop(previous_metadata.developer_id, None)
        existing_object_id = self._object_ids_by_developer_id.get(metadata.developer_id)
        if existing_object_id is not None and existing_object_id != object_id:
            existing_reference = self._widget_references.get(existing_object_id)
            if existing_reference is not None and existing_reference() is not None:
                raise ValueError(f"Duplicate developer ID: {metadata.developer_id}")
            self._drop_widget(existing_object_id)

        self._ui_metadata[object_id] = metadata
        self._object_ids_by_developer_id[metadata.developer_id] = object_id
        self._widget_references[object_id] = weakref.ref(
            widget,
            lambda _reference, registered_id=object_id: self._drop_widget(registered_id),
        )
        set_property = getattr(widget, "setProperty", None)
        if callable(set_property):
            set_property("developerId", metadata.developer_id)
            if metadata.feature_id:
                set_property("featureId", metadata.feature_id)

    def unregister_ui(self, widget: object) -> None:
        self._drop_widget(id(widget))

    def metadata_for(self, widget: object | None) -> UiMetadata | None:
        if widget is None:
            return None
        return self._ui_metadata.get(id(widget))

    def developer_id_for(self, widget: object | None) -> str:
        metadata = self.metadata_for(widget)
        return metadata.developer_id if metadata is not None else ""

    def feature_for_widget(self, widget: object | None) -> FeatureMetadata | None:
        current = widget
        while current is not None:
            metadata = self.metadata_for(current)
            if metadata is not None and metadata.feature_id:
                return self.feature(metadata.feature_id)
            parent_method = getattr(current, "parentWidget", None)
            current = parent_method() if callable(parent_method) else None
        return None

    def registered_ui_ids(self) -> tuple[str, ...]:
        self._purge_dead_widgets()
        return tuple(sorted(self._object_ids_by_developer_id))

    def register_feature(self, metadata: FeatureMetadata) -> None:
        existing = self._features.get(metadata.feature_id)
        if existing is not None and existing != metadata:
            raise ValueError(f"Duplicate feature ID: {metadata.feature_id}")
        self._features[metadata.feature_id] = metadata

    def feature(self, feature_id: str) -> FeatureMetadata | None:
        return self._features.get(feature_id)

    def features(self) -> tuple[FeatureMetadata, ...]:
        return tuple(sorted(self._features.values(), key=lambda item: item.feature_id))

    def record_feature(self, feature_id: str, status: str) -> None:
        if not self.enabled or feature_id not in self._features:
            return
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        if self._recent and self._recent[-1].feature_id == feature_id and self._recent[-1].status == status:
            previous = self._recent.pop()
            self._recent.append(
                RecentFeatureRun(
                    feature_id=feature_id,
                    executed_at=now,
                    status=status,
                    repeat_count=previous.repeat_count + 1,
                )
            )
        else:
            self._recent.append(RecentFeatureRun(feature_id, now, status))
        self._notify_recent_changed()

    def recent_runs(self) -> tuple[RecentFeatureRun, ...]:
        return tuple(reversed(self._recent))

    def clear_recent(self) -> None:
        self._recent.clear()
        self._notify_recent_changed()

    def on_recent_changed(self, callback: Callable[[], None]) -> None:
        if callback not in self._recent_callbacks:
            self._recent_callbacks.append(callback)

    def _drop_widget(self, object_id: int) -> None:
        metadata = self._ui_metadata.pop(object_id, None)
        self._widget_references.pop(object_id, None)
        if metadata is not None and self._object_ids_by_developer_id.get(metadata.developer_id) == object_id:
            self._object_ids_by_developer_id.pop(metadata.developer_id, None)

    def _purge_dead_widgets(self) -> None:
        for object_id, reference in list(self._widget_references.items()):
            if reference() is None:
                self._drop_widget(object_id)

    def _notify_recent_changed(self) -> None:
        for callback in tuple(self._recent_callbacks):
            callback()
