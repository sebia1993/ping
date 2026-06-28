from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
import threading
import time
import tracemalloc
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.models import STATUS_OK, STATUS_TIMEOUT, PingResult
from app.storage.atomic_write import atomic_write_path
from app.storage.session_log import iter_observations, session_log_segment_index
from app.ui.worker import TRACE_REFRESH_SECONDS, MeasurementWorker


# soak test는 "오래 돌려도 멈추지 않는지" 보는 안정성 테스트입니다.
# 실제 IP를 때리지 않고 가짜 ping 응답을 만들어, timeout이 많은 환경을 안전하게 재현합니다.
TOP_EVENT_SAMPLE_LIMIT = 10


@dataclass
class EventLoopStats:
    """UI 이벤트 tick을 전부 저장하지 않고 장시간 운영에 필요한 통계만 유지합니다."""

    tick_count: int = 0
    previous_tick: float | None = None
    total_gap_seconds: float = 0.0
    max_gap_seconds: float = 0.0
    total_process_seconds: float = 0.0
    max_process_seconds: float = 0.0
    top_gap_samples: list[dict[str, object]] = field(default_factory=list)
    top_process_samples: list[dict[str, object]] = field(default_factory=list)

    def record(
        self,
        tick: float,
        *,
        elapsed_seconds: float,
        event_process_seconds: float,
        updates: int,
        diagnostic_samples: int,
        last_diagnostics: dict[str, object] | None,
    ) -> None:
        self.tick_count += 1
        self.total_process_seconds += event_process_seconds
        self.max_process_seconds = max(self.max_process_seconds, event_process_seconds)

        if self.previous_tick is None:
            self.previous_tick = tick
            return

        event_gap_seconds = tick - self.previous_tick
        self.previous_tick = tick
        self.total_gap_seconds += event_gap_seconds
        self.max_gap_seconds = max(self.max_gap_seconds, event_gap_seconds)

        sample = self._sample(
            elapsed_seconds=elapsed_seconds,
            event_gap_seconds=event_gap_seconds,
            event_process_seconds=event_process_seconds,
            updates=updates,
            diagnostic_samples=diagnostic_samples,
            last_diagnostics=last_diagnostics or {},
        )
        _keep_top_samples(self.top_gap_samples, sample, key="event_gap_seconds")
        _keep_top_samples(self.top_process_samples, sample, key="event_process_seconds")

    @property
    def avg_gap_seconds(self) -> float:
        gap_count = max(self.tick_count - 1, 0)
        return self.total_gap_seconds / gap_count if gap_count else 0.0

    @property
    def avg_process_seconds(self) -> float:
        return self.total_process_seconds / self.tick_count if self.tick_count else 0.0

    def _sample(
        self,
        *,
        elapsed_seconds: float,
        event_gap_seconds: float,
        event_process_seconds: float,
        updates: int,
        diagnostic_samples: int,
        last_diagnostics: dict[str, object],
    ) -> dict[str, object]:
        return {
            "elapsed_seconds": round(elapsed_seconds, 3),
            "event_gap_seconds": round(event_gap_seconds, 6),
            "event_process_seconds": round(event_process_seconds, 6),
            "updates": updates,
            "diagnostic_samples": diagnostic_samples,
            "active_ping_count": int(last_diagnostics.get("active_ping_count", 0) or 0),
            "pending_ping_count": int(last_diagnostics.get("pending_ping_count", 0) or 0),
            "log_queue_depth": int(last_diagnostics.get("log_queue_depth", 0) or 0),
            "active_threads": threading.active_count(),
        }


def _keep_top_samples(samples: list[dict[str, object]], sample: dict[str, object], *, key: str) -> None:
    samples.append(sample)
    samples.sort(key=lambda row: float(row.get(key, 0.0) or 0.0), reverse=True)
    del samples[TOP_EVENT_SAMPLE_LIMIT:]


SOAK_PROFILES: dict[str, dict[str, object]] = {
    "default": {
        "duration_seconds": 1800.0,
        "targets": 20,
        "timeout_ratio": 0.75,
        "interval_seconds": 1,
        "timeout_delay_seconds": 1.5,
        "with_ui": False,
        "event_poll_seconds": 0.05,
        "event_process_max_milliseconds": 0,
        "sample_seconds": 1.0,
        "progress_seconds": 60.0,
        "max_ui_event_gap_seconds": 2.0,
        "max_ui_event_process_seconds": 2.0,
        "max_active_threads": 40,
        "max_memory_growth_mb": 96.0,
        "max_cpu_percent": 80.0,
    },
    "release": {
        "duration_seconds": 5.0,
        "targets": 50,
        "timeout_ratio": 0.8,
        "interval_seconds": 1,
        "timeout_delay_seconds": 0.05,
        "with_ui": False,
        "event_poll_seconds": 0.02,
        "event_process_max_milliseconds": 0,
        "sample_seconds": 0.5,
        "progress_seconds": 0.0,
        "max_ui_event_gap_seconds": 2.0,
        "max_ui_event_process_seconds": 2.0,
        "max_active_threads": 40,
        "max_memory_growth_mb": 96.0,
        "max_cpu_percent": 250.0,
    },
    "long": {
        "duration_seconds": 1800.0,
        "targets": 50,
        "timeout_ratio": 0.8,
        "interval_seconds": 1,
        "timeout_delay_seconds": 1.5,
        "with_ui": False,
        "event_poll_seconds": 0.05,
        "event_process_max_milliseconds": 0,
        "sample_seconds": 1.0,
        "progress_seconds": 60.0,
        "max_ui_event_gap_seconds": 2.0,
        "max_ui_event_process_seconds": 2.0,
        "max_active_threads": 40,
        "max_memory_growth_mb": 96.0,
        "max_cpu_percent": 80.0,
    },
    "long4h": {
        "duration_seconds": 14_400.0,
        "targets": 50,
        "timeout_ratio": 0.8,
        "interval_seconds": 1,
        "timeout_delay_seconds": 1.5,
        "with_ui": False,
        "event_poll_seconds": 0.05,
        "event_process_max_milliseconds": 0,
        "sample_seconds": 5.0,
        "progress_seconds": 300.0,
        "max_ui_event_gap_seconds": 2.0,
        "max_ui_event_process_seconds": 2.0,
        "max_active_threads": 40,
        "max_memory_growth_mb": 128.0,
        "max_cpu_percent": 70.0,
    },
    "long8h": {
        "duration_seconds": 28_800.0,
        "targets": 50,
        "timeout_ratio": 0.8,
        "interval_seconds": 1,
        "timeout_delay_seconds": 1.5,
        "with_ui": False,
        "event_poll_seconds": 0.05,
        "event_process_max_milliseconds": 0,
        "sample_seconds": 10.0,
        "progress_seconds": 600.0,
        "max_ui_event_gap_seconds": 2.0,
        "max_ui_event_process_seconds": 2.0,
        "max_active_threads": 40,
        "max_memory_growth_mb": 160.0,
        "max_cpu_percent": 70.0,
    },
    "long24h": {
        "duration_seconds": 86_400.0,
        "targets": 50,
        "timeout_ratio": 0.8,
        "interval_seconds": 1,
        "timeout_delay_seconds": 1.5,
        "with_ui": False,
        "event_poll_seconds": 0.05,
        "event_process_max_milliseconds": 0,
        "sample_seconds": 30.0,
        "progress_seconds": 1800.0,
        "max_ui_event_gap_seconds": 2.0,
        "max_ui_event_process_seconds": 2.0,
        "max_active_threads": 40,
        "max_memory_growth_mb": 256.0,
        "max_cpu_percent": 70.0,
    },
    "ui": {
        "duration_seconds": 60.0,
        "targets": 50,
        "timeout_ratio": 0.8,
        "interval_seconds": 1,
        "timeout_delay_seconds": 0.05,
        "with_ui": True,
        "event_poll_seconds": 0.01,
        "event_process_max_milliseconds": 10,
        "sample_seconds": 0.5,
        "progress_seconds": 10.0,
        "max_ui_event_gap_seconds": 2.0,
        "max_ui_event_process_seconds": 0.5,
        "max_active_threads": 40,
        "max_memory_growth_mb": 96.0,
        "max_cpu_percent": 250.0,
    },
    "ui10": {
        "duration_seconds": 600.0,
        "targets": 10,
        "timeout_ratio": 0.8,
        "interval_seconds": 1,
        "timeout_delay_seconds": 0.05,
        "with_ui": True,
        "event_poll_seconds": 0.01,
        "event_process_max_milliseconds": 10,
        "sample_seconds": 1.0,
        "progress_seconds": 60.0,
        "max_ui_event_gap_seconds": 0.2,
        "max_ui_event_process_seconds": 0.2,
        "max_active_threads": 32,
        "max_memory_growth_mb": 96.0,
        "max_cpu_percent": 200.0,
    },
    "ui20": {
        "duration_seconds": 600.0,
        "targets": 20,
        "timeout_ratio": 0.8,
        "interval_seconds": 1,
        "timeout_delay_seconds": 0.05,
        "with_ui": True,
        "event_poll_seconds": 0.01,
        "event_process_max_milliseconds": 10,
        "sample_seconds": 1.0,
        "progress_seconds": 60.0,
        "max_ui_event_gap_seconds": 0.2,
        "max_ui_event_process_seconds": 0.2,
        "max_active_threads": 36,
        "max_memory_growth_mb": 96.0,
        "max_cpu_percent": 200.0,
    },
    "ui50": {
        "duration_seconds": 600.0,
        "targets": 50,
        "timeout_ratio": 0.8,
        "interval_seconds": 1,
        "timeout_delay_seconds": 0.05,
        "with_ui": True,
        "event_poll_seconds": 0.01,
        "event_process_max_milliseconds": 10,
        "sample_seconds": 1.0,
        "progress_seconds": 60.0,
        "max_ui_event_gap_seconds": 0.2,
        "max_ui_event_process_seconds": 0.2,
        "max_active_threads": 40,
        "max_memory_growth_mb": 128.0,
        "max_cpu_percent": 250.0,
    },
}


def main() -> int:
    args = parse_args()
    # 결과 파일은 나중에 원인을 볼 수 있도록 JSON 요약과 CSV 두 종류로 남깁니다.
    # diagnostics는 측정 엔진 상태, health는 프로세스 메모리와 이벤트 루프 상태입니다.
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.session_log_root is None:
        args.session_log_root = args.output_dir / "session_logs"
    args.session_log_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    diagnostics_csv_path = args.output_dir / f"soak_{args.targets}_targets_{timestamp}.diagnostics.csv"
    health_csv_path = args.output_dir / f"soak_{args.targets}_targets_{timestamp}.health.csv"
    json_path = args.output_dir / f"soak_{args.targets}_targets_{timestamp}.json"

    # with_ui=False면 실제 창 없이 QCoreApplication만 돌립니다.
    # with_ui=True면 offscreen 창까지 연결해 UI 갱신 경로도 같이 확인합니다.
    app, window = create_application(with_ui=args.with_ui)
    targets = [f"198.51.100.{index}" for index in range(1, args.targets + 1)]
    updates: list[float] = []
    diagnostics_rows: list[dict[str, object]] = []
    health_rows: list[dict[str, object]] = []
    event_loop_stats = EventLoopStats()
    errors: list[str] = []
    session_log_paths: list[str] = []
    ping_calls: dict[str, int] = {}
    ping_results: dict[str, int] = {}
    ping_counter_lock = threading.Lock()
    # 앞쪽 일부 IP는 정상 응답, 뒤쪽 IP는 timeout으로 만듭니다.
    # 예를 들어 targets=50, timeout_ratio=0.8이면 대략 40개가 timeout입니다.
    timeout_start_index = max(1, int(args.targets * (1 - args.timeout_ratio)) + 1)
    traceroute_probe = StableTracerouteProbe()

    def ping_factory(timeout_ms: int) -> SimulatedPingRunner:
        return SimulatedPingRunner(
            timeout_ms=timeout_ms,
            timeout_start_index=timeout_start_index,
            timeout_delay_seconds=args.timeout_delay_seconds,
            calls=ping_calls,
            results=ping_results,
            counter_lock=ping_counter_lock,
        )

    # 실제 앱과 같은 MeasurementWorker를 사용하되, ping/tracert 실행기만 가짜로 바꿉니다.
    # 그래서 네트워크 환경에 영향받지 않고 다중 타깃 병목과 저장 큐 상태를 확인할 수 있습니다.
    worker = MeasurementWorker(
        targets[0],
        interval_seconds=args.interval_seconds,
        max_cycles=None,
        targets=targets,
        ping_probe_factory=ping_factory,
        traceroute_probe=traceroute_probe,
        session_log_root=args.session_log_root,
    )
    worker.measurement_updated.connect(lambda *_args: updates.append(time.monotonic()))
    worker.diagnostics_updated.connect(
        lambda diagnostics: diagnostics_rows.append(
            diagnostics_to_row(diagnostics, time.monotonic() - started_at)
        )
    )
    worker.error_message.connect(errors.append)
    worker.session_log_ready.connect(session_log_paths.append)
    connect_window(window, worker)

    # tracemalloc은 Python 메모리 증가량을 보기 위한 표준 도구입니다.
    # 테스트 중 메모리가 계속 늘면 장시간 사용 시 문제가 될 수 있습니다.
    tracemalloc.start()
    process_started_at = time.process_time()
    started_at = time.monotonic()
    last_sample_at = started_at
    last_progress_at = started_at
    worker.start()
    stopped_cleanly = False
    try:
        # 이 루프가 실제 장시간 실행 구간입니다.
        # Qt 이벤트 처리, Worker 결과 수집, 메모리/스레드 샘플링을 일정 간격으로 반복합니다.
        while time.monotonic() - started_at < args.duration_seconds and not errors:
            before_events = time.monotonic()
            process_application_events(app, args.event_process_max_milliseconds)
            after_events = time.monotonic()
            event_loop_stats.record(
                after_events,
                elapsed_seconds=after_events - started_at,
                event_process_seconds=after_events - before_events,
                updates=len(updates),
                diagnostic_samples=len(diagnostics_rows),
                last_diagnostics=diagnostics_rows[-1] if diagnostics_rows else None,
            )
            now = time.monotonic()
            if now - last_sample_at >= args.sample_seconds:
                current_memory, peak_memory = tracemalloc.get_traced_memory()
                health_rows.append(
                    {
                        "elapsed_seconds": round(now - started_at, 3),
                        "current_memory_bytes": current_memory,
                        "peak_memory_bytes": peak_memory,
                        "active_threads": threading.active_count(),
                        "event_process_seconds": round(time.monotonic() - before_events, 6),
                    }
                )
                last_sample_at = now
            if args.progress_seconds and now - last_progress_at >= args.progress_seconds:
                print_progress(now - started_at, updates, diagnostics_rows, health_rows)
                last_progress_at = now
            time.sleep(args.event_poll_seconds)
    finally:
        worker.request_stop()
        stopped_cleanly = worker.wait(max(30000, int((args.timeout_delay_seconds + 5) * 1000)))
        process_application_events(app, args.event_process_max_milliseconds)
        if window is not None:
            window.close()

    elapsed = time.monotonic() - started_at
    current_memory, peak_memory = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    health_rows.append(
        {
            "elapsed_seconds": round(elapsed, 3),
            "current_memory_bytes": current_memory,
            "peak_memory_bytes": peak_memory,
            "active_threads": threading.active_count(),
            "event_process_seconds": 0.0,
        }
    )

    # 수집한 값을 한 번에 요약한 뒤 기준치를 넘는 항목이 있는지 평가합니다.
    # failures가 비어 있으면 soak test는 성공입니다.
    summary = build_summary(
        args=args,
        elapsed=elapsed,
        updates=updates,
        diagnostics_rows=diagnostics_rows,
        health_rows=health_rows,
        event_loop_stats=event_loop_stats,
        errors=errors,
        session_log_paths=session_log_paths,
        ping_calls=ping_calls,
        ping_results=ping_results,
        traceroute_calls=traceroute_probe.calls,
        cpu_seconds=time.process_time() - process_started_at,
        current_memory_bytes=current_memory,
        peak_memory_bytes=peak_memory,
        diagnostics_csv_path=diagnostics_csv_path,
        health_csv_path=health_csv_path,
        stopped_cleanly=stopped_cleanly,
    )
    failures = evaluate_summary(summary, args)
    summary["failures"] = failures

    write_diagnostics_csv(diagnostics_csv_path, diagnostics_rows)
    write_health_csv(health_csv_path, health_rows)
    write_summary_json(json_path, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    return 1 if failures else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    # profile은 사람이 자주 쓰는 기준값 묶음입니다.
    # release는 빠른 릴리즈 검증, long 계열은 장시간 안정성 확인에 맞춰져 있습니다.
    # ui10/ui20/ui50은 대상 수별 UI 멈춤 여부를 200ms 기준으로 수치화합니다.
    argv = list(sys.argv[1:] if argv is None else argv)
    profile_parser = argparse.ArgumentParser(add_help=False)
    profile_parser.add_argument("--profile", choices=sorted(SOAK_PROFILES), default="default")
    profile_args, _unused = profile_parser.parse_known_args(argv)
    profile_defaults = SOAK_PROFILES[profile_args.profile]

    parser = argparse.ArgumentParser(description="Run a long-duration stability check.")
    parser.add_argument(
        "--profile",
        choices=sorted(SOAK_PROFILES),
        default=profile_args.profile,
        help="Preset thresholds for default, release smoke, long stability, or offscreen UI soak.",
    )
    parser.add_argument("--duration-seconds", type=float, default=profile_defaults["duration_seconds"])
    parser.add_argument("--targets", type=int, default=profile_defaults["targets"])
    parser.add_argument("--timeout-ratio", type=float, default=profile_defaults["timeout_ratio"])
    parser.add_argument("--interval-seconds", type=int, default=profile_defaults["interval_seconds"])
    parser.add_argument("--timeout-delay-seconds", type=float, default=profile_defaults["timeout_delay_seconds"])
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts" / "soak")
    parser.add_argument("--session-log-root", type=Path)
    ui_group = parser.add_mutually_exclusive_group()
    ui_group.add_argument(
        "--with-ui",
        dest="with_ui",
        action="store_true",
        default=bool(profile_defaults["with_ui"]),
        help="Drive the real MainWindow slots offscreen.",
    )
    ui_group.add_argument("--no-ui", dest="with_ui", action="store_false")
    parser.add_argument("--event-poll-seconds", type=float, default=profile_defaults["event_poll_seconds"])
    parser.add_argument(
        "--event-process-max-milliseconds",
        type=int,
        default=profile_defaults["event_process_max_milliseconds"],
    )
    parser.add_argument("--sample-seconds", type=float, default=profile_defaults["sample_seconds"])
    parser.add_argument("--progress-seconds", type=float, default=profile_defaults["progress_seconds"])
    parser.add_argument("--max-update-gap-seconds", type=float)
    parser.add_argument("--max-average-update-gap-seconds", type=float)
    parser.add_argument("--max-ui-event-gap-seconds", type=float, default=profile_defaults["max_ui_event_gap_seconds"])
    parser.add_argument(
        "--max-ui-event-process-seconds",
        type=float,
        default=profile_defaults["max_ui_event_process_seconds"],
    )
    parser.add_argument("--max-log-queue-depth", type=int)
    parser.add_argument("--max-pending-pings", type=int)
    parser.add_argument("--max-active-threads", type=int, default=profile_defaults["max_active_threads"])
    parser.add_argument("--max-memory-growth-mb", type=float, default=profile_defaults["max_memory_growth_mb"])
    parser.add_argument("--max-cpu-percent", type=float, default=profile_defaults["max_cpu_percent"])
    parser.add_argument("--no-require-backoff", action="store_true")
    args = parser.parse_args(argv)
    if args.targets < 1:
        parser.error("--targets must be at least 1")
    if not 0 <= args.timeout_ratio <= 1:
        parser.error("--timeout-ratio must be between 0 and 1")
    if args.interval_seconds < 0:
        parser.error("--interval-seconds must be non-negative")
    return args


def create_application(*, with_ui: bool):
    # GUI까지 시험할 때도 실제 모니터에 창을 띄우지 않도록 offscreen 플랫폼을 사용합니다.
    # 자동 검증 환경에서 창이 떠서 작업을 방해하지 않게 하기 위함입니다.
    if with_ui:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        from app.ui.main_window import MainWindow

        app = QApplication.instance() or QApplication([])
        window = MainWindow()
        window.resize(1366, 820)
        window.show()
        return app, window

    from PySide6.QtCore import QCoreApplication

    return QCoreApplication.instance() or QCoreApplication([]), None


def process_application_events(app: object, max_milliseconds: int) -> None:
    if max_milliseconds > 0:
        from PySide6.QtCore import QEventLoop

        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, max_milliseconds)
        return
    app.processEvents()


def connect_window(window: object | None, worker: MeasurementWorker) -> None:
    # UI soak에서는 Worker의 신호를 MainWindow 슬롯에 직접 연결합니다.
    # 실제 사용자가 앱을 켰을 때와 같은 화면 갱신 경로를 통과시키기 위한 연결입니다.
    if window is None:
        return
    worker.trace_completed.connect(window.on_trace_completed)
    worker.measurement_updated.connect(window.on_measurement_updated)
    worker.diagnostics_updated.connect(window.on_diagnostics_updated)
    worker.session_log_ready.connect(window.on_session_log_ready)
    worker.status_message.connect(window.on_status_message)


class StableTracerouteProbe:
    # tracert는 느리고 네트워크 환경마다 결과가 달라집니다.
    # soak test에서는 다중 ping 안정성이 핵심이라, tracert는 빠르게 끝나는 가짜 객체로 대체합니다.
    def __init__(self) -> None:
        self.calls = 0

    def trace(self, target: str, timeout_ms: int, stop_event) -> object:
        self.calls += 1
        time.sleep(0.01)
        return []


class SimulatedPingRunner:
    # 실제 ping 명령 대신 성공/timeout 결과를 예측 가능하게 돌려주는 테스트용 실행기입니다.
    # timeout이 많은 현장을 흉내 내면서도 외부 네트워크나 관리자 권한이 필요 없습니다.
    def __init__(
        self,
        *,
        timeout_ms: int,
        timeout_start_index: int,
        timeout_delay_seconds: float,
        calls: dict[str, int],
        results: dict[str, int],
        counter_lock: threading.Lock,
    ) -> None:
        self.timeout_ms = timeout_ms
        self.timeout_start_index = timeout_start_index
        self.timeout_delay_seconds = timeout_delay_seconds
        self.calls = calls
        self.results = results
        self.counter_lock = counter_lock

    def ping(self, target: str) -> PingResult:
        self._increment(self.calls, target)
        target_index = int(target.rsplit(".", 1)[1])
        if target_index >= self.timeout_start_index:
            time.sleep(self.timeout_delay_seconds)
            self._increment(self.results, target)
            return PingResult(target, False, None, STATUS_TIMEOUT, datetime.now())
        time.sleep(0.01)
        self._increment(self.results, target)
        return PingResult(target, True, 10.0, STATUS_OK, datetime.now())

    def _increment(self, counters: dict[str, int], target: str) -> None:
        with self.counter_lock:
            counters[target] = counters.get(target, 0) + 1


def diagnostics_to_row(diagnostics: object, elapsed_seconds: float) -> dict[str, object]:
    return {
        "elapsed_seconds": round(elapsed_seconds, 3),
        "timestamp": getattr(diagnostics, "last_update_iso", datetime.now().isoformat(timespec="seconds")),
        "active_ping_count": getattr(diagnostics, "active_ping_count", 0),
        "pending_ping_count": getattr(diagnostics, "pending_ping_count", 0),
        "timeout_target_count": getattr(diagnostics, "timeout_target_count", 0),
        "backoff_target_count": getattr(diagnostics, "backoff_target_count", 0),
        "log_queue_depth": getattr(diagnostics, "log_queue_depth", 0),
        "average_loop_delay_ms": getattr(diagnostics, "average_loop_delay_ms", 0.0),
        "tracert_status": getattr(diagnostics, "tracert_status", ""),
    }


def build_summary(
    *,
    args: argparse.Namespace,
    elapsed: float,
    updates: list[float],
    diagnostics_rows: list[dict[str, object]],
    health_rows: list[dict[str, object]],
    event_loop_stats: EventLoopStats,
    errors: list[str],
    session_log_paths: list[str],
    ping_calls: dict[str, int],
    ping_results: dict[str, int],
    traceroute_calls: int,
    cpu_seconds: float,
    current_memory_bytes: int,
    peak_memory_bytes: int,
    diagnostics_csv_path: Path,
    health_csv_path: Path,
    stopped_cleanly: bool,
) -> dict[str, Any]:
    # raw 샘플을 그대로 읽기 어렵기 때문에, 실패 판단에 필요한 최댓값과 평균값만 요약합니다.
    # 이 summary는 콘솔 출력과 JSON 파일에 같이 기록됩니다.
    session_stats = collect_session_log_stats(session_log_paths)
    update_gaps = [later - earlier for earlier, later in zip(updates, updates[1:])]
    current_memory_values = [int(row["current_memory_bytes"]) for row in health_rows]
    memory_growth = (
        max(current_memory_values) - current_memory_values[0]
        if current_memory_values
        else current_memory_bytes
    )
    max_pending = max_int(diagnostics_rows, "pending_ping_count")
    max_active = max_int(diagnostics_rows, "active_ping_count")
    max_log_queue = max_int(diagnostics_rows, "log_queue_depth")
    max_timeout_targets = max_int(diagnostics_rows, "timeout_target_count")
    max_backoff_targets = max_int(diagnostics_rows, "backoff_target_count")
    max_loop_delay_ms = max_float(diagnostics_rows, "average_loop_delay_ms")
    max_threads = max_int(health_rows, "active_threads")
    cpu_percent = (cpu_seconds / elapsed * 100.0) if elapsed > 0 else 0.0
    completed_ping_results = sum(ping_results.values())
    session_log_min_expected_rows = completed_ping_results
    session_log_row_delta = session_stats["rows"] - session_log_min_expected_rows
    return {
        "profile": args.profile,
        "targets": args.targets,
        "timeout_ratio": args.timeout_ratio,
        "duration_seconds": elapsed,
        "with_ui": args.with_ui,
        "updates": len(updates),
        "errors": errors,
        "stopped_cleanly": stopped_cleanly,
        "session_log_paths": session_log_paths,
        "session_log_rows": session_stats["rows"],
        "session_log_segments": session_stats["segments"],
        "ping_calls": sum(ping_calls.values()),
        "ping_results": completed_ping_results,
        "session_log_min_expected_rows": session_log_min_expected_rows,
        "session_log_row_delta": session_log_row_delta,
        "traceroute_calls": traceroute_calls,
        "active_threads_final": threading.active_count(),
        "max_active_threads": max_threads,
        "cpu_seconds": cpu_seconds,
        "cpu_percent": cpu_percent,
        "current_memory_bytes": current_memory_bytes,
        "peak_memory_bytes": peak_memory_bytes,
        "memory_growth_bytes": memory_growth,
        "max_update_gap_seconds": max(update_gaps, default=0.0),
        "avg_update_gap_seconds": statistics.fmean(update_gaps) if update_gaps else 0.0,
        "event_loop_samples": event_loop_stats.tick_count,
        "max_ui_event_gap_seconds": event_loop_stats.max_gap_seconds,
        "avg_ui_event_gap_seconds": event_loop_stats.avg_gap_seconds,
        "max_ui_event_process_seconds": event_loop_stats.max_process_seconds,
        "avg_ui_event_process_seconds": event_loop_stats.avg_process_seconds,
        "top_ui_event_gaps": event_loop_stats.top_gap_samples,
        "top_ui_event_processes": event_loop_stats.top_process_samples,
        "diagnostic_samples": len(diagnostics_rows),
        "health_samples": len(health_rows),
        "max_active_ping_count": max_active,
        "max_pending_ping_count": max_pending,
        "max_timeout_target_count": max_timeout_targets,
        "max_backoff_target_count": max_backoff_targets,
        "max_log_queue_depth": max_log_queue,
        "max_average_loop_delay_ms": max_loop_delay_ms,
        "diagnostics_csv": str(diagnostics_csv_path),
        "health_csv": str(health_csv_path),
    }


def evaluate_summary(summary: dict[str, Any], args: argparse.Namespace) -> list[str]:
    # 여기서 성공/실패 기준을 한곳에 모아 판정합니다.
    # 기준을 조정해야 할 때는 Worker 코드가 아니라 이 함수의 threshold를 먼저 확인하면 됩니다.
    failures: list[str] = []
    max_update_gap_seconds = args.max_update_gap_seconds or max(
        args.interval_seconds * 4,
        args.timeout_delay_seconds + 3,
        4.0,
    )
    max_average_update_gap_seconds = args.max_average_update_gap_seconds or max(
        args.interval_seconds * 1.25,
        args.interval_seconds + 0.25,
        1.25,
    )
    max_pending_pings = args.max_pending_pings or min(args.targets, 20) + 8
    max_log_queue_depth = args.max_log_queue_depth or max(args.targets * 5, 100)
    max_memory_growth_bytes = int(args.max_memory_growth_mb * 1024 * 1024)
    expected_updates = int(args.duration_seconds // max(args.interval_seconds, 1))
    min_updates = max(int(expected_updates * 0.95), 1)
    min_diagnostics = max(int(expected_updates * 0.95), 1)
    require_backoff = (
        not args.no_require_backoff
        and args.timeout_ratio > 0
        and args.duration_seconds >= max(5.0, args.interval_seconds * 4)
    )

    if summary["errors"]:
        failures.append(f"worker errors: {summary['errors']}")
    if not summary["stopped_cleanly"]:
        failures.append("worker did not stop cleanly")
    if summary["updates"] < min_updates:
        failures.append(f"too few UI-delivered updates: {summary['updates']} < {min_updates}")
    if summary["diagnostic_samples"] < min_diagnostics:
        failures.append(f"too few diagnostic samples: {summary['diagnostic_samples']} < {min_diagnostics}")
    if summary["max_update_gap_seconds"] > max_update_gap_seconds:
        failures.append(
            f"update gap too high: {summary['max_update_gap_seconds']:.3f}s > {max_update_gap_seconds:.3f}s"
        )
    if summary["avg_update_gap_seconds"] > max_average_update_gap_seconds:
        failures.append(
            f"average update gap too high: {summary['avg_update_gap_seconds']:.3f}s > "
            f"{max_average_update_gap_seconds:.3f}s"
        )
    if summary["max_ui_event_gap_seconds"] > args.max_ui_event_gap_seconds:
        failures.append(
            f"UI event gap too high: {summary['max_ui_event_gap_seconds']:.3f}s > "
            f"{args.max_ui_event_gap_seconds:.3f}s"
        )
    if summary["max_ui_event_process_seconds"] > args.max_ui_event_process_seconds:
        failures.append(
            f"UI event processing too slow: {summary['max_ui_event_process_seconds']:.3f}s > "
            f"{args.max_ui_event_process_seconds:.3f}s"
        )
    if summary["max_pending_ping_count"] > max_pending_pings:
        failures.append(f"pending ping count too high: {summary['max_pending_ping_count']} > {max_pending_pings}")
    if summary["max_log_queue_depth"] > max_log_queue_depth:
        failures.append(f"log queue depth too high: {summary['max_log_queue_depth']} > {max_log_queue_depth}")
    if summary["max_active_threads"] > args.max_active_threads:
        failures.append(f"active thread count too high: {summary['max_active_threads']} > {args.max_active_threads}")
    if summary["memory_growth_bytes"] > max_memory_growth_bytes:
        failures.append(
            f"memory growth too high: {summary['memory_growth_bytes']} > {max_memory_growth_bytes}"
        )
    if summary["cpu_percent"] > args.max_cpu_percent:
        failures.append(f"CPU usage too high: {summary['cpu_percent']:.1f}% > {args.max_cpu_percent:.1f}%")
    if int(summary.get("session_log_segments", 0) or 0) < 1:
        failures.append("session log was not created")
    completed_pings = int(summary.get("ping_results", summary.get("ping_calls", 0)) or 0)
    if int(summary.get("session_log_rows", 0) or 0) < completed_pings:
        failures.append(
            f"session log rows too low: {summary.get('session_log_rows', 0)} < completed ping results {completed_pings}"
        )
    if require_backoff and summary["max_backoff_target_count"] < 1:
        failures.append("timeout backoff was not observed")
    if args.duration_seconds >= TRACE_REFRESH_SECONDS * 1.5:
        expected_trace_calls = max(int(args.duration_seconds // TRACE_REFRESH_SECONDS), 1)
        if summary["traceroute_calls"] < expected_trace_calls:
            failures.append(
                f"too few tracert refreshes: {summary['traceroute_calls']} < {expected_trace_calls}"
            )
    return failures


def max_int(rows: list[dict[str, object]], key: str) -> int:
    return max((int(row.get(key, 0) or 0) for row in rows), default=0)


def max_float(rows: list[dict[str, object]], key: str) -> float:
    return max((float(row.get(key, 0.0) or 0.0) for row in rows), default=0.0)


def collect_session_log_stats(paths: list[str]) -> dict[str, int]:
    rows = 0
    segments = 0
    for value in paths:
        path = Path(value)
        if not path.exists():
            continue
        indexed_segments = session_log_segment_index(path)
        segments += len(indexed_segments)
        rows += sum(1 for _observation in iter_observations(path))
    return {"rows": rows, "segments": segments}


def write_diagnostics_csv(path: Path, rows: list[dict[str, object]]) -> None:
    fieldnames = [
        "elapsed_seconds",
        "timestamp",
        "active_ping_count",
        "pending_ping_count",
        "timeout_target_count",
        "backoff_target_count",
        "log_queue_depth",
        "average_loop_delay_ms",
        "tracert_status",
    ]
    atomic_write_path(path, lambda temp_path: _write_csv(temp_path, fieldnames, rows))


def write_health_csv(path: Path, rows: list[dict[str, object]]) -> None:
    fieldnames = [
        "elapsed_seconds",
        "current_memory_bytes",
        "peak_memory_bytes",
        "active_threads",
        "event_process_seconds",
    ]
    atomic_write_path(path, lambda temp_path: _write_csv(temp_path, fieldnames, rows))


def write_summary_json(path: Path, summary: dict[str, Any]) -> None:
    atomic_write_path(
        path,
        lambda temp_path: temp_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        ),
    )


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def print_progress(
    elapsed_seconds: float,
    updates: list[float],
    diagnostics_rows: list[dict[str, object]],
    health_rows: list[dict[str, object]],
) -> None:
    last_diag = diagnostics_rows[-1] if diagnostics_rows else {}
    last_health = health_rows[-1] if health_rows else {}
    print(
        json.dumps(
            {
                "elapsed_seconds": round(elapsed_seconds, 1),
                "updates": len(updates),
                "pending_ping_count": last_diag.get("pending_ping_count", 0),
                "log_queue_depth": last_diag.get("log_queue_depth", 0),
                "active_threads": last_health.get("active_threads", threading.active_count()),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
