from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.models import STATUS_OK, HopObservation, MetricSnapshot
from app.core.ping_runner import CommandPingRunner
from app.core.traceroute import run_traceroute
from app.storage.csv_exporter import export_csv
from app.storage.excel_exporter import export_xlsx
from app.storage.report_writer import write_text_report
from app.ui.worker import MeasurementWorker
from app.utils.validators import validate_target


# 릴리즈를 올리기 전에 "소스 테스트, GUI 기본 실행, 저장 파일 생성, 장시간 안정성, EXE 실행"을
# 한 번에 확인하는 검증 스크립트입니다. 하나라도 실패하면 배포하면 안 된다는 뜻입니다.
def main() -> int:
    parser = argparse.ArgumentParser(description="Run local release verification for Network Path Diagnostics.")
    parser.add_argument("--live", action="store_true", help="Run live public IPv4 checks.")
    parser.add_argument("--exe", action="store_true", help="Launch the packaged EXE if it exists.")
    parser.add_argument(
        "--target",
        action="append",
        default=[],
        help="Run a read-only ping/tracert smoke check against a field target. Can be passed multiple times.",
    )
    args = parser.parse_args()

    # 기본 검증은 외부 네트워크 없이도 반복 가능해야 합니다.
    # 실제 인터넷 ping은 환경에 따라 막힐 수 있으므로 --live를 줄 때만 실행합니다.
    checks = [
        ("unit tests", run_pytest),
        ("compileall", run_compileall),
        ("release policy", run_release_policy_check),
        ("qt smoke", run_qt_smoke),
        ("export smoke", run_export_smoke),
        ("deterministic 50-target soak", run_soak_smoke),
    ]
    if args.live:
        checks.append(("live network smoke", run_live_smoke))
    for index, target in enumerate(args.target, start=1):
        checks.append((f"custom target smoke #{index}", lambda target=target: run_custom_target_smoke(target)))
    if args.exe:
        checks.append(("packaged exe smoke", run_exe_smoke))

    for name, check in checks:
        print(f"[check] {name}")
        check()
        print(f"[ok] {name}")
    print("[ok] release verification complete")
    return 0


def run_command(command: list[str], *, env: dict[str, str] | None = None, timeout: int = 120) -> None:
    # 모든 하위 명령은 프로젝트 루트에서 실행합니다.
    # 이렇게 해야 어느 폴더에서 스크립트를 실행하든 import 경로와 상대 경로가 흔들리지 않습니다.
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=merged_env,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Command failed with exit code {completed.returncode}: {' '.join(command)}")


def run_pytest() -> None:
    run_command([sys.executable, "-m", "pytest"], timeout=180)


def run_compileall() -> None:
    run_command([sys.executable, "-m", "compileall", "app", "tests"], timeout=120)


def run_release_policy_check() -> None:
    # 배포 정책 점검입니다. 테스트가 통과해도 여기서 걸리면 사용자가 실행하기 불편한 EXE가 될 수 있습니다.
    # 예: 관리자 권한 요구, 콘솔 창 노출, 불필요하게 큰 패키지 포함 여부를 확인합니다.
    build_script = (ROOT / "build_windows_exe.ps1").read_text(encoding="utf-8")
    spec = (ROOT / "NetworkPathDiagnostics.spec").read_text(encoding="utf-8")
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")

    forbidden_admin_markers = ("--uac-admin", "requireAdministrator", "highestAvailable")
    combined_build_text = f"{build_script}\n{spec}"
    for marker in forbidden_admin_markers:
        if marker in combined_build_text:
            raise RuntimeError(f"Release packaging requests elevated privileges: {marker}")

    if "--windowed" not in build_script or "console=False" not in spec:
        raise RuntimeError("Release packaging is expected to use a windowed, console-free app.")

    expected_excludes = ("numpy", "PIL", "lxml", "PySide6.QtQuick", "PySide6.QtPdf")
    for marker in expected_excludes:
        if marker not in combined_build_text:
            raise RuntimeError(f"Release packaging is missing size optimization exclude: {marker}")

    runtime_forbidden_packages = ("requests", "httpx", "aiohttp")
    app_sources = list((ROOT / "app").rglob("*.py"))
    for path in app_sources:
        text = path.read_text(encoding="utf-8")
        for package in runtime_forbidden_packages:
            if f"import {package}" in text or f"from {package}" in text:
                raise RuntimeError(f"External API client dependency found in runtime code: {path}")

    forbidden_runtime_requirements = set(runtime_forbidden_packages)
    for line in requirements.splitlines():
        name = line.strip().split("==")[0].split(">=")[0].split("<")[0].lower()
        if name in forbidden_runtime_requirements:
            raise RuntimeError(f"External API client dependency found in requirements: {name}")


def run_qt_smoke() -> None:
    # Qt smoke는 GUI를 화면에 띄우지 않고 생성만 해보는 빠른 확인입니다.
    # 위젯 생성 단계에서 깨지는 import, 폰트, 기본 컬럼 수 문제를 여기서 잡습니다.
    code = (
        "import sys; "
        "from PySide6.QtWidgets import QApplication; "
        "from app.ui.main_window import MainWindow; "
        "app=QApplication(sys.argv); "
        "w=MainWindow(); "
        "assert w.windowTitle() == '\\ub124\\ud2b8\\uc6cc\\ud06c \\uacbd\\ub85c \\uc9c4\\ub2e8'; "
        "assert w.table.columnCount() == 13; "
        "assert w.session_state_label.text() == '대기'"
    )
    run_command([sys.executable, "-c", code], env={"QT_QPA_PLATFORM": "offscreen"}, timeout=30)


def run_export_smoke() -> None:
    # 내보내기 smoke는 실제 측정 없이 가짜 샘플 하나로 CSV/XLSX/TXT가 만들어지는지만 봅니다.
    # 저장 기능은 현장 장애 분석에 중요하므로, 빈 파일이 만들어져도 실패로 처리합니다.
    snapshot = MetricSnapshot(
        hop_index=1,
        address="127.0.0.1",
        hostname="localhost",
        samples=1,
        sent=1,
        received=1,
        timeout_count=0,
        current_latency_ms=0.5,
        avg_latency_ms=0.5,
        min_latency_ms=0.5,
        max_latency_ms=0.5,
        loss_percent=0.0,
        recent_loss_percent=0.0,
        jitter_ms=None,
        status=STATUS_OK,
    )
    observation = HopObservation(
        timestamp=datetime.now(),
        hop_index=1,
        address="127.0.0.1",
        hostname="localhost",
        success=True,
        latency_ms=0.5,
        status=STATUS_OK,
    )
    with tempfile.TemporaryDirectory(prefix="npd_verify_") as tmp:
        base = Path(tmp)
        export_csv(base / "smoke.csv", [observation], [snapshot], ["정상"])
        export_xlsx(base / "smoke.xlsx", "127.0.0.1", [observation], [snapshot], ["정상"])
        write_text_report(base / "smoke.txt", "127.0.0.1", [snapshot], ["정상"])
        for name in ("smoke.csv", "smoke.xlsx", "smoke.txt"):
            path = base / name
            if not path.exists() or path.stat().st_size == 0:
                raise RuntimeError(f"Export smoke failed: {name}")


def run_soak_smoke() -> None:
    # soak smoke는 50개 대상 중 대부분이 timeout인 상황을 짧게 시뮬레이션합니다.
    # 다중 IP 측정이 느린 대상 때문에 멈추지 않는지 확인하는 용도입니다.
    with tempfile.TemporaryDirectory(prefix="npd_soak_") as tmp:
        base = Path(tmp)
        run_command(
            [
                sys.executable,
                "scripts\\soak_test.py",
                "--profile",
                "release",
                "--output-dir",
                str(base / "output"),
                "--session-log-root",
                str(base / "session_logs"),
            ],
            timeout=90,
        )


def run_live_smoke() -> None:
    # --live를 줄 때만 실행되는 실제 네트워크 확인입니다.
    # 회사망, VPN, 방화벽 상태에 따라 실패할 수 있으므로 기본 릴리즈 검증에는 포함하지 않습니다.
    public_ping = CommandPingRunner(1000).ping("8.8.8.8")
    if not public_ping.success:
        raise RuntimeError(f"8.8.8.8 ping did not succeed: {public_ping.status}")

    hops = run_traceroute("8.8.8.8", max_hops=5, timeout_ms=700)
    if not hops:
        raise RuntimeError("8.8.8.8 tracert returned no hops")

    timeout_result = CommandPingRunner(500).ping("203.0.113.1")
    if timeout_result.success:
        raise RuntimeError("Reserved non-routable test address unexpectedly replied")

    resolved_ok, _message = validate_target("definitely-not-real.invalid")
    if resolved_ok:
        raise RuntimeError("Domain input unexpectedly passed IPv4-only validation")

    run_live_worker_smoke("8.8.8.8")


def run_live_worker_smoke(target: str) -> None:
    # Worker를 직접 한 바퀴 실행해 신호가 나오는지 확인합니다.
    # GUI 버튼을 누르지 않아도 측정 엔진 자체가 살아 있는지 볼 수 있습니다.
    from PySide6.QtCore import QCoreApplication

    app = QCoreApplication.instance() or QCoreApplication([])
    _ = app
    traces: list[object] = []
    updates: list[tuple[object, object, object, object, object]] = []
    errors: list[str] = []

    class LimitedTracerouteProbe:
        def trace(self, target: str, timeout_ms: int, stop_event) -> object:
            return run_traceroute(target, max_hops=5, timeout_ms=timeout_ms, stop_event=stop_event)

    worker = MeasurementWorker(
        target,
        interval_seconds=0,
        max_cycles=1,
        timeout_ms=700,
        traceroute_probe=LimitedTracerouteProbe(),
    )
    worker.trace_completed.connect(traces.append)
    worker.measurement_updated.connect(lambda *args: updates.append(args))
    worker.error_message.connect(errors.append)
    worker.run()

    if errors:
        raise RuntimeError(f"Live worker smoke failed: {errors[0]}")
    if not traces:
        raise RuntimeError("Live worker smoke emitted no trace")
    if not updates:
        raise RuntimeError("Live worker smoke emitted no measurement update")

    snapshots = list(updates[-1][0])
    target_snapshot = updates[-1][1]
    target_snapshots = list(updates[-1][2])
    observations = list(updates[-1][4])
    if not snapshots:
        raise RuntimeError("Live worker smoke returned no hop snapshots")
    if not target_snapshots:
        raise RuntimeError("Live worker smoke returned no IPv4 target snapshots")
    if target_snapshot.sent < 1:
        raise RuntimeError("Live worker smoke did not measure the target")
    if not observations:
        raise RuntimeError("Live worker smoke returned no observations")


def run_custom_target_smoke(target: str) -> None:
    target = target.strip()
    if not target:
        raise RuntimeError("Custom target is empty")

    resolved_ok, _message = validate_target(target)
    if not resolved_ok:
        raise RuntimeError("Custom target IPv4 validation failed")

    ping_result = CommandPingRunner(1000).ping(target)
    if not ping_result.success:
        raise RuntimeError(f"Custom target ping did not succeed: {ping_result.status}")

    hops = run_traceroute(target, max_hops=10, timeout_ms=700)
    if not hops:
        raise RuntimeError("Custom target tracert returned no hops")


def run_exe_smoke() -> None:
    # 빌드된 EXE를 실제 프로세스로 실행해봅니다.
    # 3초 안에 바로 종료되면 누락된 DLL이나 런타임 오류일 가능성이 높습니다.
    exe = ROOT / "dist" / "NetworkPathDiagnostics" / "NetworkPathDiagnostics.exe"
    if not exe.exists():
        raise RuntimeError(f"Packaged EXE not found: {exe}")
    run_packaged_size_check(exe.parent)
    process = subprocess.Popen([str(exe)], cwd=ROOT)
    try:
        time.sleep(3)
        if process.poll() is not None:
            raise RuntimeError("Packaged app exited during launch smoke test")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()


def run_packaged_size_check(package_dir: Path) -> None:
    # 빌드 스크립트에서 제외하기로 한 큰 파일이 다시 들어오지 않았는지 확인합니다.
    # 새 의존성을 추가할 때 ZIP 크기가 갑자기 커지는 문제를 막기 위한 검사입니다.
    internal = package_dir / "_internal"
    forbidden_paths = [
        internal / "numpy",
        internal / "numpy.libs",
        internal / "PIL",
        internal / "lxml",
        internal / "PySide6" / "Qt6Quick.dll",
        internal / "PySide6" / "Qt6Qml.dll",
        internal / "PySide6" / "Qt6Pdf.dll",
        internal / "PySide6" / "translations",
    ]
    for path in forbidden_paths:
        if path.exists():
            raise RuntimeError(f"Packaged app still contains excluded size-heavy artifact: {path}")


if __name__ == "__main__":
    raise SystemExit(main())
