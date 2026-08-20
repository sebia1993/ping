from __future__ import annotations

from app.developer.models import BuildInfo, ErrorSnapshot, RuntimeInfo
from app.developer.registry import FeatureMetadata, RecentFeatureRun
from app.developer.request_builder import (
    RequestContext,
    build_error_request,
    build_feature_request,
    build_technical_information,
    build_ui_request,
)


def _context(**overrides) -> RequestContext:
    values = {
        "request_id": "REQ-20260813-132501",
        "build_info": BuildInfo(
            program_version="1.4.2",
            build_id="20260813-01",
            build_time="2026-08-13T10:25:41+09:00",
            git_commit="a81c4f2",
            git_branch="main",
            distribution="Windows Portable EXE",
            config_schema_version="2",
            source_state="커밋과 일치",
        ),
        "runtime_info": RuntimeInfo(
            operating_system="Windows 11",
            screen_resolution="1366 x 768",
            display_scale="125%",
            window_size="1200 x 720",
            generated_at="2026-08-13T13:25:01+09:00",
            environment_kind="사내 테스트 환경",
        ),
        "feature": FeatureMetadata(
            "measurement.start",
            "Ping 측정 시작",
            "측정 시작",
            ui_id="main_dashboard.measurement.start_button",
            event_handler="MainWindow.start_measurement",
            service_function="MeasurementWorker.start",
            source_file="app/ui/main_window.py",
        ),
        "fields": {"request_type": "기존 기능 변경", "desired_change": "시작 버튼 동작을 수정"},
    }
    values.update(overrides)
    return RequestContext(**values)


def test_feature_request_contains_build_identity_and_commit_mismatch_guard() -> None:
    text = build_feature_request(_context())

    assert "프로그램 버전: 1.4.2" in text
    assert "빌드 ID: 20260813-01" in text
    assert "Git 커밋: a81c4f2" in text
    assert "기능 ID: measurement.start" in text
    assert "테스트한 실행 파일과 현재 소스가 동일하다고 임의로 가정하지 말 것" in text
    assert "현재 동작:\n확인 불가" in text


def test_error_request_contains_reproduction_and_recent_feature_status() -> None:
    context = _context(
        request_id="BUG-20260813-132501",
        fields={
            "steps": "시작 클릭\n10분 대기",
            "actual_result": "화면이 멈춤",
            "expected_result": "그래프가 계속 갱신",
            "frequency": "자주 발생",
            "reproducible": "재현 가능",
        },
        error=ErrorSnapshot("2026-08-13T13:20:00+09:00", "TimeoutError", "worker timeout"),
        recent_runs=(RecentFeatureRun("measurement.start", "2026-08-13T13:19:00+09:00", "성공"),),
    )

    text = build_error_request(context)

    assert "1. 시작 클릭" in text
    assert "2. 10분 대기" in text
    assert "TimeoutError" in text
    assert "measurement.start" in text


def test_ui_request_uses_temporary_path_for_unregistered_widget() -> None:
    context = _context(
        request_id="UI-20260813-132501",
        fields={"desired_change": "폭을 넓힘"},
        ui_info={
            "screen_name": "main_dashboard",
            "developer_id": "미등록",
            "temporary_path": "main_window > content > QPushButton[3]",
            "ui_type": "QPushButton",
            "display_text": "새로고침",
            "hierarchy": "MainWindow > QPushButton",
            "x": "10",
            "y": "20",
            "width": "80",
            "height": "30",
            "current_value": "선택 안 됨",
        },
    )

    text = build_ui_request(context)

    assert "개발자 ID: 미등록" in text
    assert "임시 경로: main_window > content > QPushButton[3]" in text
    assert "현재 입력값 (사용자 선택 포함)" in text


def test_technical_information_excludes_machine_identity_fields() -> None:
    text = build_technical_information(_context())

    assert "운영체제: Windows 11" in text
    assert "화면 배율: 125%" in text
    assert "사용자 이름" not in text
    assert "PC 이름" not in text
    assert "로컬 사용자 경로" not in text
