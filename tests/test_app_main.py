from __future__ import annotations

import sys
import subprocess
import time
from pathlib import Path

import pytest

import app.main as app_main
from app.utils.instance_lock import (
    APP_ALREADY_RUNNING_CODE,
    APP_INSTANCE_LOCK_FAILED_CODE,
    InstanceLockError,
    acquire_instance_lock,
)


class _FakeApplication:
    def __init__(self, _argv) -> None:
        self.application_name = ""

    def setApplicationName(self, value: str) -> None:
        self.application_name = value

    def exec(self) -> int:
        return 0


class _FakeLock:
    def __init__(self) -> None:
        self.unlocked = False

    def unlock(self) -> None:
        self.unlocked = True


def test_main_reports_localized_startup_error_without_raising(monkeypatch) -> None:
    messages: list[tuple[str, str]] = []
    instance_lock = _FakeLock()

    class FailingMainWindow:
        def __init__(self) -> None:
            raise PermissionError("denied")

    monkeypatch.setattr(app_main, "QApplication", _FakeApplication)
    monkeypatch.setattr(app_main, "MainWindow", FailingMainWindow)
    monkeypatch.setattr(app_main, "acquire_instance_lock", lambda: instance_lock)
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
    assert instance_lock.unlocked is True


def test_main_reports_existing_instance_without_starting_window(monkeypatch) -> None:
    messages: list[tuple[str, str]] = []

    def already_running():
        raise InstanceLockError(APP_ALREADY_RUNNING_CODE, "멀티핑체크가 이미 실행 중입니다.")

    monkeypatch.setattr(app_main, "QApplication", _FakeApplication)
    monkeypatch.setattr(app_main, "acquire_instance_lock", already_running)
    monkeypatch.setattr(app_main, "configure_logging", lambda: pytest.fail("logging must not start"))
    monkeypatch.setattr(
        app_main.QMessageBox,
        "critical",
        lambda _parent, title, message: messages.append((title, message)),
    )

    assert app_main.main() == 2
    assert messages == [
        (
            "멀티핑체크 실행 중",
            "멀티핑체크가 이미 실행 중입니다.\n\n오류 코드: APP_ALREADY_RUNNING",
        )
    ]


def test_instance_lock_rejects_second_owner_and_recovers_after_unlock(tmp_path) -> None:
    first = acquire_instance_lock(tmp_path)
    try:
        with pytest.raises(InstanceLockError) as exc_info:
            acquire_instance_lock(tmp_path)
        assert exc_info.value.code == APP_ALREADY_RUNNING_CODE
    finally:
        first.unlock()

    recovered = acquire_instance_lock(tmp_path)
    recovered.unlock()


def test_instance_lock_rejects_a_second_process(tmp_path) -> None:
    ready_path = tmp_path / "ready"
    release_path = tmp_path / "release"
    project_root = Path(__file__).resolve().parents[1]
    script = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from app.utils.instance_lock import acquire_instance_lock\n"
        "root, ready, release = map(Path, sys.argv[1:4])\n"
        "lock = acquire_instance_lock(root)\n"
        "ready.write_text('ready', encoding='utf-8')\n"
        "deadline = time.monotonic() + 10\n"
        "while not release.exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.01)\n"
        "lock.unlock()\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path), str(ready_path), str(release_path)],
        cwd=project_root,
    )
    try:
        deadline = time.monotonic() + 5
        while not ready_path.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready_path.exists(), f"lock owner process exited with {process.poll()}"

        with pytest.raises(InstanceLockError) as exc_info:
            acquire_instance_lock(tmp_path)
        assert exc_info.value.code == APP_ALREADY_RUNNING_CODE
    finally:
        release_path.write_text("release", encoding="utf-8")
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    assert process.returncode == 0


def test_instance_lock_reports_unwritable_root(tmp_path) -> None:
    root_file = tmp_path / "not-a-directory"
    root_file.write_text("occupied", encoding="utf-8")

    with pytest.raises(InstanceLockError) as exc_info:
        acquire_instance_lock(root_file)

    assert exc_info.value.code == APP_INSTANCE_LOCK_FAILED_CODE


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
