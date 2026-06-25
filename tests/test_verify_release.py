from __future__ import annotations

from datetime import datetime
from pathlib import Path

import scripts.verify_release as verify_release
from app.core.models import STATUS_ERROR, STATUS_OK, HopInfo, MetricSnapshot, PingResult


def test_custom_target_smoke_runs_read_only_ping_and_trace(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(verify_release, "validate_target", lambda target: (True, ""))

    class FakePingRunner:
        def __init__(self, timeout_ms: int) -> None:
            self.timeout_ms = timeout_ms

        def ping(self, target: str) -> PingResult:
            calls.append(("ping", target))
            return PingResult(target, True, 1.0, STATUS_OK, datetime.now())

    monkeypatch.setattr(verify_release, "CommandPingRunner", FakePingRunner)
    monkeypatch.setattr(
        verify_release,
        "run_traceroute",
        lambda target, max_hops, timeout_ms: calls.append(("tracert", target))
        or [HopInfo(index=1, address="192.0.2.1")],
    )

    verify_release.run_custom_target_smoke("192.0.2.1")

    assert calls == [("ping", "192.0.2.1"), ("tracert", "192.0.2.1")]


def test_custom_target_smoke_fails_without_echo_reply(monkeypatch) -> None:
    monkeypatch.setattr(verify_release, "validate_target", lambda target: (True, ""))

    class FakePingRunner:
        def __init__(self, timeout_ms: int) -> None:
            self.timeout_ms = timeout_ms

        def ping(self, target: str) -> PingResult:
            return PingResult(target, False, None, STATUS_ERROR, datetime.now())

    monkeypatch.setattr(verify_release, "CommandPingRunner", FakePingRunner)

    try:
        verify_release.run_custom_target_smoke("203.0.113.1")
    except RuntimeError as exc:
        assert "Custom target ping did not succeed" in str(exc)
    else:
        raise AssertionError("Expected custom target smoke to fail")


def test_live_worker_smoke_requires_trace_and_update(monkeypatch) -> None:
    monkeypatch.setattr(verify_release, "MeasurementWorker", _SuccessfulWorker)

    verify_release.run_live_worker_smoke("8.8.8.8")


def test_live_worker_smoke_fails_on_worker_error(monkeypatch) -> None:
    monkeypatch.setattr(verify_release, "MeasurementWorker", _ErrorWorker)

    try:
        verify_release.run_live_worker_smoke("8.8.8.8")
    except RuntimeError as exc:
        assert "Live worker smoke failed" in str(exc)
    else:
        raise AssertionError("Expected live worker smoke to fail")


def test_release_policy_accepts_windowed_non_admin_package(monkeypatch, tmp_path) -> None:
    _write_policy_tree(tmp_path)
    monkeypatch.setattr(verify_release, "ROOT", tmp_path)

    verify_release.run_release_policy_check()


def test_release_policy_rejects_admin_manifest(monkeypatch, tmp_path) -> None:
    _write_policy_tree(tmp_path, build_script="python -m PyInstaller --windowed --uac-admin app\\main.py")
    monkeypatch.setattr(verify_release, "ROOT", tmp_path)

    try:
        verify_release.run_release_policy_check()
    except RuntimeError as exc:
        assert "elevated privileges" in str(exc)
    else:
        raise AssertionError("Expected release policy check to fail")


def test_release_policy_rejects_external_api_clients(monkeypatch, tmp_path) -> None:
    _write_policy_tree(tmp_path, app_source="import requests\n")
    monkeypatch.setattr(verify_release, "ROOT", tmp_path)

    try:
        verify_release.run_release_policy_check()
    except RuntimeError as exc:
        assert "External API client dependency" in str(exc)
    else:
        raise AssertionError("Expected release policy check to fail")


def test_field_verification_docs_match_current_graph_controls() -> None:
    text = (Path(__file__).resolve().parents[1] / "docs" / "field_verification.md").read_text(encoding="utf-8")

    assert "`그래프 확대` 버튼" not in text
    assert "`전체 보기`" not in text
    assert "`최근 보기`" not in text
    assert "시간 범위 선택" in text
    assert "이름 버튼" in text
    assert "일시중지/삭제 버튼" in text
    assert "python scripts\\run_stability_soak_suite.py --dry-run" in text
    assert "python scripts\\run_stability_soak_suite.py" in text
    assert "python scripts\\soak_test.py --profile long --duration-seconds 1800 --no-ui" in text
    assert "python scripts\\soak_test.py --profile long4h" in text
    assert "python scripts\\soak_test.py --profile long8h" in text
    assert "python scripts\\soak_test.py --profile long24h" in text
    assert "python scripts\\soak_test.py --profile ui10" in text
    assert "python scripts\\soak_test.py --profile ui20" in text
    assert "python scripts\\soak_test.py --profile ui50" in text
    assert "`max_active_threads`" in text
    assert "`max_pending_ping_count`" in text


class _Signal:
    def __init__(self) -> None:
        self._callbacks = []

    def connect(self, callback) -> None:
        self._callbacks.append(callback)

    def emit(self, *args) -> None:
        for callback in self._callbacks:
            callback(*args)


class _SuccessfulWorker:
    def __init__(self, *args, **kwargs) -> None:
        self.trace_completed = _Signal()
        self.measurement_updated = _Signal()
        self.error_message = _Signal()

    def run(self) -> None:
        snapshot = MetricSnapshot(
            hop_index=1,
            address="8.8.8.8",
            hostname=None,
            samples=1,
            sent=1,
            received=1,
            timeout_count=0,
            current_latency_ms=1.0,
            avg_latency_ms=1.0,
            min_latency_ms=1.0,
            max_latency_ms=1.0,
            loss_percent=0.0,
            recent_loss_percent=0.0,
            jitter_ms=None,
            status=STATUS_OK,
            is_target=True,
        )
        self.trace_completed.emit([HopInfo(index=1, address="8.8.8.8", is_target=True)])
        self.measurement_updated.emit([snapshot], snapshot, [snapshot], ["정상"], [object()], [object()])


class _ErrorWorker:
    def __init__(self, *args, **kwargs) -> None:
        self.trace_completed = _Signal()
        self.measurement_updated = _Signal()
        self.error_message = _Signal()

    def run(self) -> None:
        self.error_message.emit("simulated failure")


def _write_policy_tree(
    root,
    *,
    build_script: str = (
        "python -m PyInstaller --windowed --exclude-module numpy --exclude-module PIL "
        "--exclude-module lxml --exclude-module PySide6.QtQuick --exclude-module PySide6.QtPdf app\\main.py"
    ),
    spec: str = "a = Analysis(excludes=['numpy', 'PIL', 'lxml', 'PySide6.QtQuick', 'PySide6.QtPdf'])\nexe = EXE(console=False)",
    requirements: str = "PySide6>=6.7\nopenpyxl>=3.1\n",
    app_source: str = "import subprocess\n",
) -> None:
    (root / "app").mkdir()
    (root / "app" / "main.py").write_text(app_source, encoding="utf-8")
    (root / "build_windows_exe.ps1").write_text(build_script, encoding="utf-8")
    (root / "MultiPingCheck.spec").write_text(spec, encoding="utf-8")
    (root / "requirements.txt").write_text(requirements, encoding="utf-8")
