from __future__ import annotations

import sys
from pathlib import Path

import app.main as app_main


class _FakeApplication:
    def __init__(self, _argv) -> None:
        self.application_name = ""

    def setApplicationName(self, value: str) -> None:
        self.application_name = value

    def exec(self) -> int:
        return 0


def test_main_reports_localized_startup_error_without_raising(monkeypatch) -> None:
    messages: list[tuple[str, str]] = []

    class FailingMainWindow:
        def __init__(self) -> None:
            raise PermissionError("denied")

    monkeypatch.setattr(app_main, "QApplication", _FakeApplication)
    monkeypatch.setattr(app_main, "MainWindow", FailingMainWindow)
    monkeypatch.setattr(app_main, "configure_logging", lambda: None)
    monkeypatch.setattr(app_main, "_install_exception_hook", lambda _path: None)
    monkeypatch.setattr(
        app_main.QMessageBox,
        "critical",
        lambda _parent, title, message: messages.append((title, message)),
    )

    assert app_main.main() == 1
    assert messages == [
        (
            "멀티핑체크 시작 오류",
            "프로그램을 시작할 수 없습니다.\n"
            "프로그램을 다시 실행해도 문제가 계속되면 진단 로그를 확인하세요.\n\n"
            "오류 코드: APP_STARTUP_FAILED",
        )
    ]


def test_unhandled_exception_hook_logs_error_code_and_log_path(monkeypatch, caplog) -> None:
    messages: list[tuple[str, str]] = []
    log_path = Path("C:/Users/test/AppData/Local/MultiPingCheck/logs/multipingcheck.log")
    original_hook = sys.excepthook
    monkeypatch.setattr(
        app_main.QMessageBox,
        "critical",
        lambda _parent, title, message: messages.append((title, message)),
    )

    try:
        app_main._install_exception_hook(log_path)
        try:
            raise ValueError("test failure")
        except ValueError:
            sys.excepthook(*sys.exc_info())
    finally:
        sys.excepthook = original_hook

    assert len(messages) == 1
    assert messages[0][0] == "멀티핑체크 오류"
    assert "APP_UNEXPECTED_ERROR" in messages[0][1]
    assert "ValueError" in messages[0][1]
    assert str(log_path) in messages[0][1]
    assert "Unhandled application error" in caplog.text
