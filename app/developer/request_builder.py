from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Sequence

from app.developer.models import BuildInfo, ErrorSnapshot, RuntimeInfo, UNKNOWN_VALUE, display_value
from app.developer.registry import FeatureMetadata, RecentFeatureRun


@dataclass(frozen=True)
class RequestContext:
    request_id: str
    build_info: BuildInfo
    runtime_info: RuntimeInfo
    fields: Mapping[str, str] = field(default_factory=dict)
    ui_info: Mapping[str, str] = field(default_factory=dict)
    feature: FeatureMetadata | None = None
    manual_feature_id: str = ""
    recent_runs: Sequence[RecentFeatureRun] = field(default_factory=tuple)
    error: ErrorSnapshot | None = None
    log_excerpt: str = ""
    screenshot_name: str = ""


class RequestIdFactory:
    def __init__(self) -> None:
        self._last_base = ""
        self._sequence = 0

    def create(self, prefix: str, now: datetime | None = None) -> str:
        current = now or datetime.now()
        base = f"{prefix}-{current.strftime('%Y%m%d-%H%M%S')}"
        if base == self._last_base:
            self._sequence += 1
            return f"{base}-{self._sequence:02d}"
        self._last_base = base
        self._sequence = 1
        return base


def build_feature_request(context: RequestContext) -> str:
    fields = context.fields
    feature = _feature_values(context)
    sections = [
        "[Codex 기능 변경 요청]",
        "",
        "요청 ID:",
        context.request_id,
        "",
        "요청 유형:",
        _field(fields, "request_type"),
        "",
        "테스트 환경:",
        context.runtime_info.environment_kind,
        "",
        "테스트한 프로그램:",
        *_program_lines(context, include_window=False),
        "",
        "대상 기능:",
        f"- 기능 ID: {feature['feature_id']}",
        f"- 기능 이름: {feature['display_name']}",
        f"- 관련 화면: {feature['screen_name']}",
        f"- 관련 UI ID: {feature['ui_id']}",
        f"- 관련 이벤트 핸들러: {feature['event_handler']}",
        f"- 관련 서비스 함수: {feature['service_function']}",
        f"- 관련 소스 파일: {feature['source_file']}",
        "",
        "현재 동작:",
        _field(fields, "current_behavior"),
        "",
        "원하는 변경:",
        _field(fields, "desired_change"),
        "",
        "사용 또는 재현 순서:",
        _field(fields, "steps"),
        "",
        "변경 이유:",
        _field(fields, "reason"),
        "",
        "그대로 유지할 부분:",
        _field(fields, "keep"),
        "",
        "변경하면 안 되는 범위:",
        _field(fields, "boundaries"),
        "",
        "완료 기준:",
        _field(fields, "acceptance"),
        "",
        "추가 정보:",
        _field(fields, "additional"),
    ]
    _append_optional_error_and_logs(sections, context)
    sections.extend(
        [
            "",
            "작업 제한:",
            "- 대상 기능과 직접 관련된 코드만 수정할 것",
            "- 기존 정상 기능을 유지할 것",
            "- 관련 없는 리팩터링을 하지 말 것",
            "- 현재 저장소와 테스트 빌드 커밋이 다르면 먼저 차이를 확인할 것",
            "- 테스트한 실행 파일과 현재 소스가 동일하다고 임의로 가정하지 말 것",
            "- 존재하지 않는 파일이나 함수를 추측하지 말 것",
            "- 수정 후 기존 기능 회귀 테스트를 수행할 것",
            "- 실행하지 못한 테스트를 완료했다고 표현하지 말 것",
            "",
            "작업 완료 후 보고할 내용:",
            "- 변경한 파일",
            "- 변경 전 동작",
            "- 변경 후 동작",
            "- 유지된 기존 기능",
            "- 수행한 테스트",
            "- 확인하지 못한 항목",
        ]
    )
    return "\n".join(sections).strip() + "\n"


def build_error_request(context: RequestContext) -> str:
    fields = context.fields
    feature = _feature_values(context)
    ui = context.ui_info
    sections = [
        "[Codex 오류 수정 요청]",
        "",
        "요청 ID:",
        context.request_id,
        "",
        "테스트 환경:",
        context.runtime_info.environment_kind,
        "",
        "테스트한 프로그램:",
        *_program_lines(context, include_window=False),
        "",
        "오류 발생 위치:",
        f"- 화면: {display_value(fields.get('screen_name') or ui.get('screen_name') or feature['screen_name'])}",
        f"- UI ID: {display_value(ui.get('developer_id'))}",
        f"- 기능 ID: {feature['feature_id']}",
        f"- 관련 함수: {display_value(feature['event_handler'] or feature['service_function'])}",
        f"- 관련 소스 파일: {feature['source_file']}",
        "",
        "재현 순서:",
        _numbered_steps(_field(fields, "steps")),
        "",
        "오류 발생 전 수행한 작업:",
        _field(fields, "before_error"),
        "",
        "실제 결과:",
        _field(fields, "actual_result"),
        "",
        "기대 결과:",
        _field(fields, "expected_result"),
        "",
        "발생 빈도:",
        _field(fields, "frequency"),
        "",
        "재현 가능 여부:",
        _field(fields, "reproducible"),
    ]
    if context.error is not None:
        sections.extend(
            [
                "",
                "오류 메시지:",
                f"- 발생 시각: {display_value(context.error.occurred_at)}",
                f"- 오류 종류: {display_value(context.error.error_type)}",
                f"- 메시지: {display_value(context.error.message)}",
                f"- 프로그램 종료 여부: {'예' if context.error.application_terminated else '아니요'}",
            ]
        )
    sections.extend(
        [
            "",
            "최근 실행 기능:",
            _recent_runs_text(context.recent_runs),
        ]
    )
    if context.log_excerpt:
        sections.extend(["", "관련 로그:", context.log_excerpt])
    sections.extend(
        [
            "",
            "추가 설명:",
            _field(fields, "additional"),
            "",
            "그대로 유지해야 하는 기능:",
            _field(fields, "keep"),
            "",
            "완료 기준:",
            "- 위 재현 순서에서 오류가 다시 발생하지 않을 것",
            "- 기존 정상 기능에 영향이 없을 것",
            "- 수정에 대한 테스트를 추가하거나 실행할 것",
            "",
            "작업 제한:",
            "- 테스트 빌드와 현재 저장소의 커밋 차이를 먼저 확인할 것",
            "- 테스트한 실행 파일과 현재 소스가 동일하다고 임의로 가정하지 말 것",
            "- 오류와 무관한 코드를 임의로 수정하지 말 것",
            "- 오류를 단순히 숨기거나 예외를 무시하는 방식으로 처리하지 말 것",
            "- 원인을 확인하고 최소 범위로 수정할 것",
        ]
    )
    return "\n".join(sections).strip() + "\n"


def build_ui_request(context: RequestContext) -> str:
    fields = context.fields
    ui = context.ui_info
    feature = _feature_values(context)
    sections = [
        "[Codex UI 수정 요청]",
        "",
        "요청 ID:",
        context.request_id,
        "",
        "테스트한 프로그램:",
        *_program_lines(context, include_window=True),
        "",
        "대상 화면:",
        display_value(ui.get("screen_name")),
        "",
        "대상 UI:",
        _ui_identifier(ui),
        "",
        "UI 종류:",
        display_value(ui.get("ui_type")),
        "",
        "표시 문구:",
        display_value(ui.get("display_text")),
        "",
        "UI 계층:",
        display_value(ui.get("hierarchy")),
        "",
        "관련 코드:",
        f"- 소스 파일: {display_value(ui.get('source_file'))}",
        f"- 클래스: {display_value(ui.get('component_class'))}",
        f"- 생성 함수: {display_value(ui.get('creation_method'))}",
        f"- 연결된 기능 ID: {feature['feature_id']}",
        f"- 이벤트 함수: {display_value(ui.get('event_handler'))}",
        "",
        "현재 위치 및 크기:",
        f"- X: {display_value(ui.get('x'))}",
        f"- Y: {display_value(ui.get('y'))}",
        f"- 너비: {display_value(ui.get('width'))}",
        f"- 높이: {display_value(ui.get('height'))}",
        "",
        "원하는 변경:",
        _field(fields, "desired_change"),
        "",
        "그대로 유지할 부분:",
        _field(fields, "keep"),
        "",
        "완료 기준:",
        _field(fields, "acceptance"),
    ]
    if ui.get("current_value"):
        sections.extend(["", "현재 입력값 (사용자 선택 포함):", display_value(ui.get("current_value"))])
    if context.screenshot_name:
        sections.extend(["", "첨부 참고 이미지:", context.screenshot_name])
    sections.extend(
        [
            "",
            "작업 제한:",
            "- 지정한 UI와 직접 관련된 부분만 수정할 것",
            "- 기존 클릭 기능을 유지할 것",
            "- 주변 UI를 임의로 변경하지 말 것",
            "- 공통 스타일 변경 시 다른 화면 영향을 검증할 것",
            "- 관련 없는 리팩터링을 하지 말 것",
            "- 현재 저장소와 테스트 빌드 커밋이 다르면 먼저 차이를 확인할 것",
            "- 테스트한 실행 파일과 현재 소스가 동일하다고 임의로 가정하지 말 것",
            "- 수정 후 실제 실행 화면을 확인할 것",
        ]
    )
    return "\n".join(sections).strip() + "\n"


def build_technical_information(context: RequestContext) -> str:
    ui_id = _ui_identifier(context.ui_info)
    feature_id = _feature_values(context)["feature_id"]
    lines = [
        "[MultiPingCheck 기술 정보]",
        f"- 프로그램 버전: {context.build_info.program_version}",
        f"- 빌드 ID: {context.build_info.build_id}",
        f"- 빌드 시각: {context.build_info.build_time}",
        f"- Git 커밋: {context.build_info.git_commit}",
        f"- Git 브랜치: {context.build_info.git_branch}",
        f"- 소스 상태: {context.build_info.source_state}",
        f"- 배포 형태: {context.build_info.distribution}",
        f"- 설정 스키마 버전: {context.build_info.config_schema_version}",
        f"- 운영체제: {context.runtime_info.operating_system}",
        f"- 화면 해상도: {context.runtime_info.screen_resolution}",
        f"- 화면 배율: {context.runtime_info.display_scale}",
        f"- 창 크기: {context.runtime_info.window_size}",
        f"- 현재 화면: {context.runtime_info.current_screen}",
        f"- 선택 UI ID: {ui_id}",
        f"- 선택 기능 ID: {feature_id}",
        f"- 실행 환경: {context.runtime_info.environment_kind}",
        f"- 생성 시각: {context.runtime_info.generated_at}",
    ]
    return "\n".join(lines) + "\n"


def _program_lines(context: RequestContext, *, include_window: bool) -> list[str]:
    build = context.build_info
    runtime = context.runtime_info
    lines = [
        f"- 프로그램 버전: {build.program_version}",
        f"- 빌드 ID: {build.build_id}",
        f"- 빌드 시각: {build.build_time}",
        f"- Git 커밋: {build.git_commit}",
        f"- Git 브랜치: {build.git_branch}",
        f"- 소스 상태: {build.source_state}",
        f"- 배포 형태: {build.distribution}",
        f"- 설정 스키마 버전: {build.config_schema_version}",
        f"- 운영체제: {runtime.operating_system}",
        f"- 화면 배율: {runtime.display_scale}",
    ]
    if include_window:
        lines.extend(
            [
                f"- 화면 해상도: {runtime.screen_resolution}",
                f"- 창 크기: {runtime.window_size}",
            ]
        )
    return lines


def _feature_values(context: RequestContext) -> dict[str, str]:
    feature = context.feature
    manual_feature_id = context.manual_feature_id.strip()
    return {
        "feature_id": display_value(feature.feature_id if feature is not None else manual_feature_id),
        "display_name": display_value(feature.display_name if feature is not None else context.fields.get("manual_feature")),
        "screen_name": display_value(feature.screen_name if feature is not None else context.runtime_info.current_screen),
        "ui_id": display_value(feature.ui_id if feature is not None else _ui_identifier(context.ui_info)),
        "event_handler": display_value(feature.event_handler if feature is not None else ""),
        "service_function": display_value(feature.service_function if feature is not None else ""),
        "source_file": display_value(feature.source_file if feature is not None else ""),
    }


def _field(fields: Mapping[str, str], key: str) -> str:
    return display_value(fields.get(key))


def _ui_identifier(ui: Mapping[str, str]) -> str:
    developer_id = str(ui.get("developer_id") or "").strip()
    if developer_id and developer_id != "미등록":
        return developer_id
    temporary_path = str(ui.get("temporary_path") or "").strip()
    if temporary_path:
        return f"개발자 ID: 미등록\n임시 경로: {temporary_path}"
    return UNKNOWN_VALUE


def _numbered_steps(value: str) -> str:
    if value == UNKNOWN_VALUE:
        return "1. 확인 불가"
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if not lines:
        return "1. 확인 불가"
    return "\n".join(f"{index}. {_strip_existing_number(line)}" for index, line in enumerate(lines, start=1))


def _strip_existing_number(value: str) -> str:
    head, separator, tail = value.partition(".")
    return tail.strip() if separator and head.strip().isdigit() else value


def _recent_runs_text(runs: Sequence[RecentFeatureRun]) -> str:
    if not runs:
        return UNKNOWN_VALUE
    lines = []
    for index, run in enumerate(runs[:10], start=1):
        repeat = f" x{run.repeat_count}" if run.repeat_count > 1 else ""
        lines.append(f"{index}. {run.feature_id} | {run.executed_at} | {run.status}{repeat}")
    return "\n".join(lines)


def _append_optional_error_and_logs(sections: list[str], context: RequestContext) -> None:
    if context.error is not None:
        sections.extend(
            [
                "",
                "관련 오류:",
                f"- 발생 시각: {display_value(context.error.occurred_at)}",
                f"- 오류 종류: {display_value(context.error.error_type)}",
                f"- 오류 메시지: {display_value(context.error.message)}",
            ]
        )
    if context.log_excerpt:
        sections.extend(["", "관련 로그:", context.log_excerpt])
