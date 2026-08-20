from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


UNKNOWN_VALUE = "확인 불가"


def display_value(value: object) -> str:
    text = str(value or "").strip()
    return text or UNKNOWN_VALUE


@dataclass(frozen=True)
class BuildInfo:
    program_name: str = "MultiPingCheck"
    program_version: str = UNKNOWN_VALUE
    build_id: str = UNKNOWN_VALUE
    build_time: str = UNKNOWN_VALUE
    git_commit: str = UNKNOWN_VALUE
    git_branch: str = UNKNOWN_VALUE
    distribution: str = UNKNOWN_VALUE
    config_schema_version: str = UNKNOWN_VALUE
    source_state: str = UNKNOWN_VALUE

    @classmethod
    def from_mapping(cls, values: dict[str, Any]) -> "BuildInfo":
        allowed = {field_info.name for field_info in cls.__dataclass_fields__.values()}
        normalized = {
            key: display_value(value)
            for key, value in values.items()
            if key in allowed
        }
        return cls(**normalized)

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class RuntimeInfo:
    operating_system: str = UNKNOWN_VALUE
    screen_resolution: str = UNKNOWN_VALUE
    display_scale: str = UNKNOWN_VALUE
    window_size: str = UNKNOWN_VALUE
    current_screen: str = "main_dashboard"
    selected_ui_id: str = UNKNOWN_VALUE
    selected_feature_id: str = UNKNOWN_VALUE
    generated_at: str = UNKNOWN_VALUE
    environment_kind: str = "사내 테스트 환경"

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class ErrorSnapshot:
    occurred_at: str
    error_type: str
    message: str
    feature_id: str = UNKNOWN_VALUE
    screen_name: str = "main_dashboard"
    ui_id: str = UNKNOWN_VALUE
    application_terminated: bool = False


@dataclass(frozen=True)
class RequestRecord:
    request_id: str
    request_type: str
    created_at: str
    screen_name: str
    ui_id: str
    feature_id: str
    summary: str
    prompt: str
    draft: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, values: dict[str, Any]) -> "RequestRecord":
        draft = values.get("draft")
        if not isinstance(draft, dict):
            draft = {}
        return cls(
            request_id=display_value(values.get("request_id")),
            request_type=display_value(values.get("request_type")),
            created_at=display_value(values.get("created_at")),
            screen_name=display_value(values.get("screen_name")),
            ui_id=display_value(values.get("ui_id")),
            feature_id=display_value(values.get("feature_id")),
            summary=display_value(values.get("summary")),
            prompt=str(values.get("prompt") or ""),
            draft={str(key): str(value) for key, value in draft.items()},
        )

    def as_dict(self) -> dict[str, object]:
        return asdict(self)
