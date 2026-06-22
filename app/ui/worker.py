from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import (
    FIRST_COMPLETED,
    Future,
    ThreadPoolExecutor,
    TimeoutError as FutureTimeoutError,
    as_completed,
    wait,
)
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue

from PySide6.QtCore import QThread, Signal

from app.core.alerts import (
    JITTER_ALERT_KEY,
    LATENCY_ALERT_KEY,
    LOSS_ALERT_KEY,
    MOS_ALERT_KEY,
    SAMPLE_ALERT_KEY,
    TIMER_ALERT_KEY,
    AlertRuleConfig,
    evaluate_target_alerts,
)
from app.core.analyzer import analyze_path
from app.core.metrics import MetricsSession, TargetMetricTracker
from app.core.models import STATUS_ERROR, STATUS_PAUSED, STATUS_TIMEOUT, HopInfo, HopObservation, PingResult
from app.core.ping_runner import CommandPingRunner, TcpConnectRunner
from app.core.probes import PingProbeFactory, TracerouteProbe
from app.core.route_history import RouteHistory
from app.core.traceroute import ensure_target_hop, run_traceroute
from app.storage.route_log import RouteLogWriter
from app.storage.session_index import (
    SESSION_STATE_ARCHIVED,
    SESSION_STATE_PAUSED,
    SessionIndexStore,
    session_index_root_for_sample_path,
)
from app.storage.session_log import SessionLogWriter
from app.utils.validators import parse_ipv4_targets, validate_target


# 여러 IP를 동시에 측정할 때 프로그램이 과도하게 많은 ping/tracert를 만들지 않도록
# 제한값을 한곳에 모아 둡니다. 숫자를 바꾸면 성능과 안정성에 직접 영향이 있습니다.
MAX_IPV4_TARGETS = 50
RECENT_OBSERVATION_LIMIT = 300
MAX_TARGET_PING_WORKERS = 20
MAX_HOP_PING_WORKERS = 4
WORKER_POLL_SECONDS = 0.05
TRACE_REFRESH_SECONDS = 60.0
BACKOFF_AFTER_FAILURES = 3
SLOW_BACKOFF_AFTER_FAILURES = 10
FIRST_BACKOFF_SECONDS = 2.0
SLOW_BACKOFF_SECONDS = 5.0
MEASUREMENT_MODE_FULL_ROUTE = "full_route"
MEASUREMENT_MODE_FINAL_HOP_ONLY = "final_hop_only"
MEASUREMENT_MODES = {MEASUREMENT_MODE_FULL_ROUTE, MEASUREMENT_MODE_FINAL_HOP_ONLY}
AUTO_FULL_ROUTE_ALERT_KEYS = {
    LOSS_ALERT_KEY,
    LATENCY_ALERT_KEY,
    JITTER_ALERT_KEY,
    SAMPLE_ALERT_KEY,
    TIMER_ALERT_KEY,
    MOS_ALERT_KEY,
}
PROBE_ENGINE_ICMP = "icmp"
PROBE_ENGINE_TCP_CONNECT = "tcp_connect"
PROBE_ENGINES = {PROBE_ENGINE_ICMP, PROBE_ENGINE_TCP_CONNECT}
WORKER_UNEXPECTED_ERROR_CODE = "WORKER_UNEXPECTED_ERROR"
SESSION_LOG_WRITE_FAILED_CODE = "SESSION_LOG_WRITE_FAILED"
ROUTE_LOG_CLOSE_FAILED_CODE = "ROUTE_LOG_CLOSE_FAILED"
SESSION_INDEX_FINISH_FAILED_CODE = "SESSION_INDEX_FINISH_FAILED"


@dataclass
class TargetProbeState:
    """대상 IP 하나의 다음 측정 시각과 실패 누적 상태를 보관합니다."""

    target: str
    next_due: float = 0.0
    consecutive_failures: int = 0
    current_interval_seconds: float = 0.0
    last_status: str = "WAITING"
    last_started_at: float = 0.0
    last_completed_at: float = 0.0

    def record_result(self, result: PingResult, base_interval_seconds: float, now: float) -> None:
        self.last_status = result.status
        self.last_completed_at = now
        if result.success:
            self.consecutive_failures = 0
        else:
            self.consecutive_failures += 1

        self.current_interval_seconds = self._next_interval(base_interval_seconds)
        base_time = self.last_started_at or now
        self.next_due = base_time + self.current_interval_seconds

    def _next_interval(self, base_interval_seconds: float) -> float:
        # 연속 실패가 많은 대상은 잠시 느리게 측정합니다. 응답이 없는 IP가 많아도
        # 전체 측정 루프가 막히지 않게 하는 안정성 장치입니다.
        if base_interval_seconds <= 0:
            return 0.0
        if self.consecutive_failures >= SLOW_BACKOFF_AFTER_FAILURES:
            return max(base_interval_seconds, SLOW_BACKOFF_SECONDS)
        if self.consecutive_failures >= BACKOFF_AFTER_FAILURES:
            return max(base_interval_seconds, FIRST_BACKOFF_SECONDS)
        return base_interval_seconds


@dataclass(frozen=True)
class WorkerDiagnostics:
    """측정 스레드 내부 상태를 UI에 보여주기 위한 요약 정보입니다."""

    active_ping_count: int
    pending_ping_count: int
    timeout_target_count: int
    backoff_target_count: int
    log_queue_depth: int
    average_loop_delay_ms: float
    last_update_iso: str
    tracert_status: str
    paused_target_count: int = 0
    target_probe_engine: str = "ICMP"
    route_probe_engine: str = "tracert/ICMP"
    tcp_port: int | None = None


class _ThreadLocalPingProbePool:
    """각 ping 작업 스레드가 자기 전용 probe를 재사용하게 해 주는 작은 풀입니다."""

    def __init__(self, factory: Callable[[], object]) -> None:
        self._factory = factory
        self._local = threading.local()
        self._lock = threading.Lock()
        self._probes: list[object] = []

    def ping(self, target: str) -> PingResult:
        probe = getattr(self._local, "probe", None)
        if probe is None:
            probe = self._factory()
            self._local.probe = probe
            with self._lock:
                self._probes.append(probe)
        return probe.ping(target)

    def close(self) -> None:
        with self._lock:
            probes = list(self._probes)
            self._probes.clear()
        for probe in probes:
            close = getattr(probe, "close", None)
            if close:
                close()


class _AsyncSessionLogWriter:
    """측정 결과를 별도 스레드에서 저장해 UI와 ping 루프가 디스크 I/O에 묶이지 않게 합니다."""

    def __init__(
        self,
        writer: SessionLogWriter,
        on_samples_written: Callable[[int, datetime, list[object]], None] | None = None,
    ) -> None:
        self.path = writer.path
        self._writer = writer
        self._on_samples_written = on_samples_written
        self._queue: Queue[list[HopObservation] | None] = Queue()
        self._error: Exception | None = None
        self._closed = False
        self._close_lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="session-log-writer", daemon=True)
        self._thread.start()

    def write_many(self, observations: list[HopObservation]) -> None:
        if observations:
            with self._close_lock:
                if not self._closed:
                    self._queue.put(list(observations))

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._queue.put(None)
        self._thread.join()
        if self._error:
            raise self._error

    def _run(self) -> None:
        pending: list[HopObservation] = []
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    if pending:
                        written = list(pending)
                        self._writer.write_many(pending)
                        self._notify_samples_written(written)
                    return
                pending.extend(item)

                while True:
                    try:
                        extra = self._queue.get_nowait()
                    except Empty:
                        break
                    if extra is None:
                        if pending:
                            written = list(pending)
                            self._writer.write_many(pending)
                            self._notify_samples_written(written)
                        return
                    pending.extend(extra)

                if pending:
                    written = list(pending)
                    self._writer.write_many(pending)
                    self._notify_samples_written(written)
                    pending = []
        except Exception as exc:
            self._error = exc
        finally:
            try:
                self._writer.close()
            except Exception as exc:
                if self._error is None:
                    self._error = exc

    @property
    def segment_paths(self) -> list[object]:
        return list(getattr(self._writer, "paths", [self.path]))

    def _notify_samples_written(self, observations: list[HopObservation]) -> None:
        if self._on_samples_written is None or not observations:
            return
        last_timestamp = max(observation.timestamp for observation in observations)
        self._on_samples_written(len(observations), last_timestamp, self.segment_paths)


class MeasurementWorker(QThread):
    """대상 목록을 실제로 측정하고, 그래프/표/세션 저장소로 보낼 데이터를 만드는 작업자입니다.

    MainWindow는 화면과 버튼을 담당하고, 이 클래스는 네트워크 측정의 실제 흐름을 담당합니다.
    흐름은 대략 `입력 IP 정리 -> tracert 갱신 -> 각 대상 ping -> 통계 계산 -> CSV 저장 -> UI 신호 전송`입니다.
    """

    trace_completed = Signal(object)
    route_changed = Signal(object)
    measurement_updated = Signal(object, object, object, object, object, object)
    diagnostics_updated = Signal(object)
    session_log_ready = Signal(str)
    status_message = Signal(str)
    error_message = Signal(str)

    def __init__(
        self,
        target: str,
        interval_seconds: int,
        max_cycles: int | None,
        timeout_ms: int = 1000,
        targets: list[str] | None = None,
        ping_probe_factory: PingProbeFactory | None = None,
        traceroute_probe: TracerouteProbe | None = None,
        measurement_mode: str = MEASUREMENT_MODE_FULL_ROUTE,
        probe_engine: str = PROBE_ENGINE_ICMP,
        tcp_port: int = 443,
        alert_rule_config: AlertRuleConfig | None = None,
        auto_full_route_on_alert: bool = True,
        auto_restore_final_hop_on_recovery: bool = False,
        parent=None,
        session_log_root: str | Path | None = None,
    ) -> None:
        super().__init__(parent)
        self.target = target.strip()
        self.interval_seconds = interval_seconds
        self.max_cycles = max_cycles
        self.timeout_ms = timeout_ms
        self.targets = self._normalize_targets(targets)
        self.ping_probe_factory = ping_probe_factory
        self.traceroute_probe = traceroute_probe
        self.measurement_mode = (
            measurement_mode if measurement_mode in MEASUREMENT_MODES else MEASUREMENT_MODE_FULL_ROUTE
        )
        self.probe_engine = probe_engine if probe_engine in PROBE_ENGINES else PROBE_ENGINE_ICMP
        self.tcp_port = max(min(int(tcp_port), 65535), 1)
        self.alert_rule_config = alert_rule_config or AlertRuleConfig()
        self.auto_full_route_on_alert = bool(auto_full_route_on_alert)
        self.auto_restore_final_hop_on_recovery = bool(auto_restore_final_hop_on_recovery)
        self.session_log_root = Path(session_log_root) if session_log_root is not None else None
        self.resumed_from_session_id = ""
        # 아래 값들은 UI 버튼에서 들어오는 중지/일시정지/간격 변경 요청을 안전하게 반영하기 위한 상태입니다.
        self._stop_event = threading.Event()
        self._control_lock = threading.Lock()
        self._paused_targets: set[str] = set()
        self._target_interval_overrides: dict[str, int] = {}
        self._ping_probe_pool: _ThreadLocalPingProbePool | None = None
        self._hop_ping_probe_pool: _ThreadLocalPingProbePool | None = None
        self._route_history = RouteHistory()
        self._auto_full_route_active = False
        self._tracert_status = "대기"

    def request_stop(self) -> None:
        self._stop_event.set()

    def pause_targets(self, targets: list[str]) -> None:
        with self._control_lock:
            self._paused_targets.update(target for target in targets if target in self.targets)

    def resume_targets(self, targets: list[str]) -> None:
        with self._control_lock:
            for target in targets:
                self._paused_targets.discard(target)

    def set_interval_seconds(self, interval_seconds: int) -> None:
        with self._control_lock:
            self.interval_seconds = max(int(interval_seconds), 0)
            self._target_interval_overrides.clear()

    def set_target_interval_seconds(self, targets: list[str], interval_seconds: int) -> None:
        interval = max(int(interval_seconds), 0)
        with self._control_lock:
            for target in targets:
                if target in self.targets:
                    self._target_interval_overrides[target] = interval

    def target_interval_overrides(self) -> dict[str, int]:
        with self._control_lock:
            return dict(self._target_interval_overrides)

    def paused_targets(self) -> set[str]:
        with self._control_lock:
            return set(self._paused_targets)

    def run(self) -> None:
        """Qt가 별도 스레드에서 호출하는 메인 측정 루프입니다."""

        # 사용자가 입력한 값은 이 지점에서 다시 IPv4 목록으로 검증합니다.
        # GUI 검증을 통과했더라도 작업자 스레드에서 한 번 더 막아야 저장/측정 로직이 안전합니다.
        targets, invalid = parse_ipv4_targets("\n".join(self.targets or [self.target]))
        if invalid:
            self.error_message.emit(f"IPv4 주소만 입력 가능합니다: {', '.join(invalid[:5])}")
            return
        if not targets:
            self.error_message.emit("대상 IPv4 주소를 입력하세요.")
            return
        if len(targets) > MAX_IPV4_TARGETS:
            targets = targets[:MAX_IPV4_TARGETS]
        self.targets = targets
        self._route_history = RouteHistory()

        # tracert 기준 대상은 전체 대상 목록 중 하나여야 합니다. 그래프의 hop 경로를
        # 어느 대상 기준으로 그릴지 정하는 값이라서 여기서 엄격하게 확인합니다.
        valid, message = validate_target(self.target)
        if not valid or self.target not in self.targets:
            self.error_message.emit(message or "Tracert 대상은 등록된 IPv4 주소 중 하나여야 합니다.")
            return
        if self._stop_event.is_set():
            self.status_message.emit("측정이 중지되었습니다.")
            return

        target_trackers = {
            target: TargetMetricTracker(target, recent_observation_limit=RECENT_OBSERVATION_LIMIT)
            for target in self.targets
        }
        recent_observations: deque[HopObservation] = deque(maxlen=RECENT_OBSERVATION_LIMIT)

        # tracert, 최종 대상 ping, hop ping을 분리된 실행기로 돌립니다.
        # 이렇게 나누면 느린 tracert나 중간 hop 응답 지연이 전체 대상 ping을 막지 않습니다.
        trace_executor = ThreadPoolExecutor(max_workers=1)
        target_executor = ThreadPoolExecutor(max_workers=min(max(len(self.targets), 1), MAX_TARGET_PING_WORKERS))
        hop_executor = ThreadPoolExecutor(max_workers=MAX_HOP_PING_WORKERS)
        trace_future: Future[list[HopInfo]] | None = (
            self._start_trace_refresh(trace_executor) if self._uses_full_route() else None
        )
        target_futures: dict[Future[PingResult], str] = {}
        hop_futures: dict[Future[PingResult], str] = {}
        active_target_pings: set[str] = set()
        active_hop_pings: set[str] = set()
        target_states = {target: TargetProbeState(target) for target in self.targets}
        session_log: _AsyncSessionLogWriter | None = None
        route_log: RouteLogWriter | None = None
        session_index: SessionIndexStore | None = None
        session_id: str | None = None
        metrics: MetricsSession | None = None
        hops: list[HopInfo] = []
        full_cycles = 0
        next_trace_refresh_due = time.monotonic() + TRACE_REFRESH_SECONDS
        loop_delays: deque[float] = deque(maxlen=120)
        completed_normally = False
        last_error = ""

        try:
            # 세션 CSV와 세션 인덱스는 측정 시작 직후 만들어 둡니다.
            # 프로그램이 중간에 꺼져도 Session Manager가 남은 CSV를 찾아 복구할 수 있습니다.
            self._ping_probe_pool = _ThreadLocalPingProbePool(self._new_ping_probe)
            self._hop_ping_probe_pool = _ThreadLocalPingProbePool(self._new_hop_ping_probe)
            session_writer = SessionLogWriter.create(self.target, root=self.session_log_root)
            route_log = RouteLogWriter.create_for_session(session_writer.path)
            session_index = SessionIndexStore.create(session_index_root_for_sample_path(session_writer.path))
            session_record = session_index.register_session(
                target=self.target,
                sample_path=session_writer.path,
                route_path=route_log.path,
                started_at=datetime.now(),
                interval_seconds=self.interval_seconds,
                measurement_mode=self._session_measurement_mode(),
                target_count=len(self.targets),
                probe_engine=self.probe_engine,
                tcp_port=self.tcp_port if self.probe_engine == PROBE_ENGINE_TCP_CONNECT else None,
                route_probe_engine=self._route_probe_label(),
                resumed_from_session_id=self.resumed_from_session_id,
            )
            session_id = session_record.session_id
            session_log = _AsyncSessionLogWriter(
                session_writer,
                lambda count, last_timestamp, segments: session_index.add_samples(
                    session_record.session_id,
                    count,
                    last_timestamp,
                    segments=segments,
                ),
            )
            self.session_log_ready.emit(str(session_log.path))
            if self._uses_full_route():
                self.status_message.emit("측정 중... 경로 탐색 병행")
            else:
                self._tracert_status = "final hop only"
                self.status_message.emit("측정 중... Final Hop Only")

            # 아래 while 문이 실제 장시간 측정 루프입니다. 한 바퀴마다 새 ping을 예약하고,
            # 완료된 결과를 수집한 뒤 UI와 저장소에 반영합니다.
            while not self._stop_event.is_set():
                round_started_at = time.monotonic()
                is_final_round = self.max_cycles is not None and full_cycles + 1 >= self.max_cycles
                scheduled_hop_targets: set[str] = set()
                metrics, hops = self._refresh_trace_result(
                    metrics,
                    hops,
                    trace_future,
                    route_log,
                    first_check=(full_cycles == 0),
                )
                if trace_future is not None and trace_future.done():
                    trace_future = None
                    next_trace_refresh_due = time.monotonic() + TRACE_REFRESH_SECONDS
                scheduled_targets = self._schedule_target_pings(
                    target_executor,
                    target_futures,
                    active_target_pings,
                    target_states,
                    now=round_started_at,
                )
                self._schedule_hop_pings(
                    hop_executor,
                    hop_futures,
                    active_hop_pings,
                    metrics,
                    hops,
                    scheduled_hop_targets,
                )

                round_deadline = round_started_at + self._target_round_wait_seconds()
                while not self._stop_event.is_set() and not self._target_round_ready(
                    scheduled_targets,
                    scheduled_hop_targets,
                    active_target_pings,
                    active_hop_pings,
                    round_deadline,
                ):
                    self._collect_completed_ping_results(
                        target_futures,
                        active_target_pings,
                        hop_futures,
                        active_hop_pings,
                        metrics,
                        hops,
                        target_trackers,
                        target_states,
                        session_log,
                        recent_observations,
                        timeout=self._next_poll_timeout(round_deadline),
                    )
                    metrics, hops = self._refresh_trace_result(metrics, hops, trace_future, route_log)
                    if trace_future is not None and trace_future.done():
                        trace_future = None
                        next_trace_refresh_due = time.monotonic() + TRACE_REFRESH_SECONDS
                    self._schedule_hop_pings(
                        hop_executor,
                        hop_futures,
                        active_hop_pings,
                        metrics,
                        hops,
                        scheduled_hop_targets,
                    )

                if is_final_round:
                    self._wait_for_active_target_pings(
                        target_futures,
                        active_target_pings,
                        hop_futures,
                        active_hop_pings,
                        metrics,
                        hops,
                        target_trackers,
                        target_states,
                        session_log,
                        recent_observations,
                    )
                self._collect_completed_ping_results(
                    target_futures,
                    active_target_pings,
                    hop_futures,
                    active_hop_pings,
                    metrics,
                    hops,
                    target_trackers,
                    target_states,
                    session_log,
                    recent_observations,
                    timeout=0,
                )
                trace_future = self._maybe_adjust_route_for_alerts(
                    trace_executor,
                    trace_future,
                    target_trackers,
                )
                self._emit_measurement_update(metrics, target_trackers, recent_observations)
                loop_delays.append(max(time.monotonic() - round_started_at - max(self.interval_seconds, 0), 0.0))
                self._emit_diagnostics(
                    active_target_pings,
                    active_hop_pings,
                    target_futures,
                    hop_futures,
                    target_states,
                    session_log,
                    loop_delays,
                )

                full_cycles += 1
                self.status_message.emit(
                    self._format_ping_progress(
                        len(self.targets) - len(active_target_pings),
                        len(self.targets),
                        waiting_for_trace=(self._uses_full_route() and metrics is None),
                    )
                )
                if self.max_cycles is not None and full_cycles >= self.max_cycles:
                    break

                if (
                    self._uses_full_route()
                    and trace_future is None
                    and time.monotonic() >= next_trace_refresh_due
                ):
                    trace_future = self._start_trace_refresh(trace_executor)

                self._sleep_until_next_round(
                    round_started_at,
                    target_executor,
                    target_futures,
                    active_target_pings,
                    target_states,
                    hop_futures,
                    active_hop_pings,
                    metrics,
                    hops,
                    target_trackers,
                    session_log,
                    recent_observations,
                )

            completed_normally = not self._stop_event.is_set()
            self.status_message.emit(
                "측정이 완료되었습니다." if not self._stop_event.is_set() else "측정이 중지되었습니다."
            )
        except Exception as exc:
            last_error = _error_code_summary(WORKER_UNEXPECTED_ERROR_CODE, exc)
            self.error_message.emit(f"측정 중 오류가 발생했습니다. 세션은 Pause 상태로 저장됩니다. ({last_error})")
        finally:
            self._stop_event.set()
            if session_log is not None:
                try:
                    session_log.close()
                except Exception as exc:
                    session_error = _error_code_summary(SESSION_LOG_WRITE_FAILED_CODE, exc)
                    last_error = _merge_error_summaries(last_error, session_error)
                    self.error_message.emit(
                        f"세션 로그 저장 중 오류가 발생했습니다. 세션은 Pause 상태로 저장됩니다. ({session_error})"
                    )
            if route_log is not None:
                try:
                    route_log.close()
                except Exception as exc:
                    route_error = _error_code_summary(ROUTE_LOG_CLOSE_FAILED_CODE, exc)
                    last_error = _merge_error_summaries(last_error, route_error)
                    self.error_message.emit(f"경로 로그 종료 중 오류가 발생했습니다. ({route_error})")
            if session_index is not None and session_id is not None:
                state = SESSION_STATE_ARCHIVED if completed_normally and not last_error else SESSION_STATE_PAUSED
                try:
                    session_index.finish_session(
                        session_id,
                        state=state,
                        ended_at=datetime.now(),
                        segments=session_log.segment_paths if session_log is not None else None,
                        last_error=last_error,
                    )
                except Exception as exc:
                    index_error = _error_code_summary(SESSION_INDEX_FINISH_FAILED_CODE, exc)
                    self.error_message.emit(f"세션 인덱스 마감 중 오류가 발생했습니다. ({index_error})")
            if self._ping_probe_pool is not None:
                self._ping_probe_pool.close()
                self._ping_probe_pool = None
            if self._hop_ping_probe_pool is not None:
                self._hop_ping_probe_pool.close()
                self._hop_ping_probe_pool = None
            target_executor.shutdown(wait=True, cancel_futures=True)
            hop_executor.shutdown(wait=True, cancel_futures=True)
            trace_executor.shutdown(wait=True, cancel_futures=True)

    def _schedule_target_pings(
        self,
        executor: ThreadPoolExecutor,
        futures: dict[Future[PingResult], str],
        active_targets: set[str],
        target_states: dict[str, TargetProbeState],
        *,
        now: float | None = None,
    ) -> set[str]:
        scheduled: set[str] = set()
        now = time.monotonic() if now is None else now
        capacity = max(min(len(self.targets), MAX_TARGET_PING_WORKERS) - len(active_targets), 0)
        if capacity == 0:
            return scheduled
        for target in self.targets:
            if self._is_target_paused(target):
                continue
            state = target_states[target]
            if target in active_targets:
                continue
            interval_seconds = self._target_base_interval_seconds(target)
            if interval_seconds > 0 and now < state.next_due:
                continue
            state.last_started_at = now
            future = executor.submit(self._ping_target, target)
            futures[future] = target
            active_targets.add(target)
            scheduled.add(target)
            capacity -= 1
            if capacity == 0:
                break
        return scheduled

    def _schedule_hop_pings(
        self,
        executor: ThreadPoolExecutor,
        futures: dict[Future[PingResult], str],
        active_hops: set[str],
        metrics: MetricsSession | None,
        hops: list[HopInfo],
        scheduled_hops: set[str],
    ) -> None:
        if metrics is None or not self._uses_full_route():
            return
        capacity = max(MAX_HOP_PING_WORKERS - len(active_hops), 0)
        if capacity == 0:
            return

        registered_targets = set(self.targets)
        for hop in hops:
            ping_target = hop.ping_target
            if (
                not ping_target
                or ping_target in registered_targets
                or ping_target in active_hops
                or ping_target in scheduled_hops
            ):
                continue
            future = executor.submit(self._ping_hop, ping_target)
            futures[future] = ping_target
            active_hops.add(ping_target)
            scheduled_hops.add(ping_target)
            capacity -= 1
            if capacity == 0:
                return

    def _collect_completed_ping_results(
        self,
        target_futures: dict[Future[PingResult], str],
        active_target_pings: set[str],
        hop_futures: dict[Future[PingResult], str],
        active_hop_pings: set[str],
        metrics: MetricsSession | None,
        hops: list[HopInfo],
        target_trackers: dict[str, TargetMetricTracker],
        target_states: dict[str, TargetProbeState],
        session_log: _AsyncSessionLogWriter,
        recent_observations: deque[HopObservation],
        *,
        timeout: float,
    ) -> None:
        all_futures = list(target_futures) + list(hop_futures)
        if not all_futures:
            self._sleep_for(timeout)
            return

        ready = {future for future in all_futures if future.done()}
        if not ready and timeout > 0:
            done, _pending = wait(all_futures, timeout=timeout, return_when=FIRST_COMPLETED)
            ready = set(done)
        ready.update(future for future in all_futures if future.done())

        for future in ready:
            if future in target_futures:
                target = target_futures.pop(future)
                active_target_pings.discard(target)
                result = self._future_result(future, target)
                target_states[target].record_result(
                    result,
                    self._target_base_interval_seconds(target),
                    time.monotonic(),
                )
                observations = self._record_target_ping_result(result, metrics, hops, target_trackers)
                self._store_observations(session_log, recent_observations, observations)
            elif future in hop_futures:
                target = hop_futures.pop(future)
                active_hop_pings.discard(target)
                result = self._future_result(future, target)
                observations = self._record_hop_ping_result(result, metrics, hops)
                self._store_observations(session_log, recent_observations, observations)

    def _record_target_ping_result(
        self,
        result: PingResult,
        metrics: MetricsSession | None,
        hops: list[HopInfo],
        target_trackers: dict[str, TargetMetricTracker],
    ) -> list[HopObservation]:
        observations: list[HopObservation] = []
        if metrics is not None:
            for hop in hops:
                if hop.ping_target == result.target:
                    observations.append(metrics.add_result(hop.index, result))

        tracker = target_trackers.get(result.target)
        if tracker:
            observations.append(tracker.add_result(result))
        return observations

    def _record_hop_ping_result(
        self,
        result: PingResult,
        metrics: MetricsSession | None,
        hops: list[HopInfo],
    ) -> list[HopObservation]:
        if metrics is None:
            return []
        return [metrics.add_result(hop.index, result) for hop in hops if hop.ping_target == result.target]

    def _store_observations(
        self,
        session_log: _AsyncSessionLogWriter,
        recent_observations: deque[HopObservation],
        observations: list[HopObservation],
    ) -> None:
        if not observations:
            return
        session_log.write_many(observations)
        recent_observations.extend(observations)

    def _maybe_adjust_route_for_alerts(
        self,
        executor: ThreadPoolExecutor,
        trace_future: Future[list[HopInfo]] | None,
        target_trackers: dict[str, TargetMetricTracker],
    ) -> Future[list[HopInfo]] | None:
        if not self.auto_full_route_on_alert:
            return trace_future
        tracker = target_trackers.get(self.target)
        if tracker is None:
            return trace_future
        active_keys, events = evaluate_target_alerts(
            list(tracker.observations),
            current_target=self.target,
            config=self.alert_rule_config,
        )
        route_adjustment_keys = active_keys.intersection(AUTO_FULL_ROUTE_ALERT_KEYS)
        if (
            self.measurement_mode == MEASUREMENT_MODE_FINAL_HOP_ONLY
            and trace_future is None
            and route_adjustment_keys
        ):
            reason = "target alert"
            for event in events:
                if event.key in route_adjustment_keys:
                    reason = event.title
                    break
            self.measurement_mode = MEASUREMENT_MODE_FULL_ROUTE
            self._auto_full_route_active = True
            self.status_message.emit(f"Auto Full Route enabled: {reason}")
            return self._start_trace_refresh(executor)
        if self._auto_full_route_active and not route_adjustment_keys:
            self._auto_full_route_active = False
            if self.auto_restore_final_hop_on_recovery:
                self.measurement_mode = MEASUREMENT_MODE_FINAL_HOP_ONLY
                self.status_message.emit("Auto Final Hop Only restored: alert recovered")
            else:
                self.status_message.emit("Auto Full Route alert recovered; Full Route remains enabled")
            return trace_future
        return trace_future

    def _emit_measurement_update(
        self,
        metrics: MetricsSession | None,
        target_trackers: dict[str, TargetMetricTracker],
        recent_observations: deque[HopObservation],
    ) -> None:
        """현재까지 쌓인 측정값을 UI가 바로 그릴 수 있는 형태로 묶어서 보냅니다."""

        snapshots = metrics.snapshots() if metrics is not None else []
        paused_targets = self.paused_targets()
        target_snapshots = [
            self._paused_snapshot(target_trackers[target].snapshot())
            if target in paused_targets
            else target_trackers[target].snapshot()
            for target in self.targets
        ]
        target_snapshot = target_trackers[self.target].snapshot()
        if self.target in paused_targets:
            target_snapshot = self._paused_snapshot(target_snapshot)
        analysis = analyze_path(snapshots, target_snapshot)
        self.measurement_updated.emit(
            snapshots,
            target_snapshot,
            target_snapshots,
            analysis,
            list(recent_observations),
            list(target_trackers[self.target].observations),
        )

    def _sleep_until_next_round(
        self,
        round_started_at: float,
        target_executor: ThreadPoolExecutor,
        target_futures: dict[Future[PingResult], str],
        active_target_pings: set[str],
        target_states: dict[str, TargetProbeState],
        hop_futures: dict[Future[PingResult], str],
        active_hop_pings: set[str],
        metrics: MetricsSession | None,
        hops: list[HopInfo],
        target_trackers: dict[str, TargetMetricTracker],
        session_log: _AsyncSessionLogWriter,
        recent_observations: deque[HopObservation],
    ) -> None:
        if self.interval_seconds <= 0:
            return
        deadline = round_started_at + self.interval_seconds
        while not self._stop_event.is_set() and time.monotonic() < deadline:
            self._schedule_target_pings(
                target_executor,
                target_futures,
                active_target_pings,
                target_states,
            )
            self._collect_completed_ping_results(
                target_futures,
                active_target_pings,
                hop_futures,
                active_hop_pings,
                metrics,
                hops,
                target_trackers,
                target_states,
                session_log,
                recent_observations,
                timeout=self._next_poll_timeout(deadline),
            )

    def _emit_diagnostics(
        self,
        active_target_pings: set[str],
        active_hop_pings: set[str],
        target_futures: dict[Future[PingResult], str],
        hop_futures: dict[Future[PingResult], str],
        target_states: dict[str, TargetProbeState],
        session_log: _AsyncSessionLogWriter,
        loop_delays: deque[float],
    ) -> None:
        """운영자가 장시간 측정 안정성을 확인할 수 있게 내부 부하 상태를 요약합니다."""

        timeout_target_count = sum(1 for state in target_states.values() if state.last_status == STATUS_TIMEOUT)
        backoff_target_count = sum(
            1
            for target, state in target_states.items()
            if self._target_base_interval_seconds(target) > 0
            and state.current_interval_seconds > self._target_base_interval_seconds(target)
        )
        pending_ping_count = len(target_futures) + len(hop_futures)
        average_loop_delay_ms = (sum(loop_delays) / len(loop_delays) * 1000) if loop_delays else 0.0
        self.diagnostics_updated.emit(
            WorkerDiagnostics(
                active_ping_count=len(active_target_pings) + len(active_hop_pings),
                pending_ping_count=pending_ping_count,
                timeout_target_count=timeout_target_count,
                backoff_target_count=backoff_target_count,
                log_queue_depth=session_log.queue_depth,
                average_loop_delay_ms=average_loop_delay_ms,
                last_update_iso=datetime.now().isoformat(timespec="seconds"),
                tracert_status=self._tracert_status,
                paused_target_count=len(self.paused_targets()),
                target_probe_engine=self._target_probe_label(),
                route_probe_engine=self._route_probe_label(),
                tcp_port=self.tcp_port if self.probe_engine == PROBE_ENGINE_TCP_CONNECT else None,
            )
        )

    def _wait_for_active_target_pings(
        self,
        target_futures: dict[Future[PingResult], str],
        active_target_pings: set[str],
        hop_futures: dict[Future[PingResult], str],
        active_hop_pings: set[str],
        metrics: MetricsSession | None,
        hops: list[HopInfo],
        target_trackers: dict[str, TargetMetricTracker],
        target_states: dict[str, TargetProbeState],
        session_log: _AsyncSessionLogWriter,
        recent_observations: deque[HopObservation],
    ) -> None:
        wait_seconds = self._target_round_wait_seconds()
        if wait_seconds <= 0:
            wait_seconds = WORKER_POLL_SECONDS
        deadline = time.monotonic() + wait_seconds
        while (
            (active_target_pings or (self.interval_seconds <= 0 and active_hop_pings))
            and not self._stop_event.is_set()
            and time.monotonic() < deadline
        ):
            self._collect_completed_ping_results(
                target_futures,
                active_target_pings,
                hop_futures,
                active_hop_pings,
                metrics,
                hops,
                target_trackers,
                target_states,
                session_log,
                recent_observations,
                timeout=self._next_poll_timeout(deadline),
            )

    def _refresh_trace_result(
        self,
        metrics: MetricsSession | None,
        hops: list[HopInfo],
        trace_future: Future[list[HopInfo]] | None,
        route_log: RouteLogWriter | None = None,
        *,
        first_check: bool = False,
    ) -> tuple[MetricsSession | None, list[HopInfo]]:
        """백그라운드 tracert 결과가 준비되었으면 hop 목록과 경로 변경 기록을 갱신합니다."""

        if trace_future is None:
            return metrics, hops
        if metrics is not None and not trace_future.done():
            return metrics, hops
        try:
            refreshed_hops = trace_future.result(timeout=0.05 if first_check else 0)
        except FutureTimeoutError:
            self._tracert_status = "탐색 중"
            return metrics, hops
        except Exception:
            self._tracert_status = "오류"
            self.status_message.emit("tracert 실행 중 오류가 발생했습니다. 대상 Ping은 계속 측정합니다.")
            refreshed_hops = [
                HopInfo(
                    index=1,
                    address=self.target,
                    hostname="Target",
                    timed_out=False,
                    is_target=True,
                )
            ]
        if self._stop_event.is_set():
            return metrics, hops
        if not refreshed_hops:
            refreshed_hops = ensure_target_hop([], self.target, self.target)
        self._tracert_status = "완료"
        route_change = self._route_history.record(refreshed_hops, datetime.now())
        if route_log is not None and self._route_history.snapshots:
            route_log.write_snapshot(self._route_history.snapshots[-1], route_change)
        self.trace_completed.emit(refreshed_hops)
        if route_change is not None:
            self.route_changed.emit(route_change)
        self.status_message.emit("측정 중...")
        return MetricsSession(refreshed_hops, recent_observation_limit=RECENT_OBSERVATION_LIMIT), refreshed_hops

    def _target_round_wait_seconds(self) -> float:
        if self.interval_seconds <= 0:
            return 0
        ping_timeout_seconds = max(self.timeout_ms / 1000, WORKER_POLL_SECONDS)
        return min(float(self.interval_seconds), ping_timeout_seconds)

    def _target_base_interval_seconds(self, target: str) -> int:
        with self._control_lock:
            return max(int(self._target_interval_overrides.get(target, self.interval_seconds)), 0)

    def _target_round_ready(
        self,
        scheduled_targets: set[str],
        scheduled_hops: set[str],
        active_target_pings: set[str],
        active_hop_pings: set[str],
        deadline: float,
    ) -> bool:
        if self.interval_seconds <= 0:
            target_ready = not any(target in active_target_pings for target in scheduled_targets)
            hop_ready = not any(hop in active_hop_pings for hop in scheduled_hops)
            return target_ready and hop_ready
        if scheduled_targets and not any(target in active_target_pings for target in scheduled_targets):
            return True
        return time.monotonic() >= deadline

    def _next_poll_timeout(self, deadline: float) -> float:
        remaining = max(deadline - time.monotonic(), 0)
        return min(WORKER_POLL_SECONDS, remaining)

    def _sleep_for(self, seconds: float) -> None:
        if seconds <= 0:
            return
        deadline = time.monotonic() + seconds
        while not self._stop_event.is_set() and time.monotonic() < deadline:
            time.sleep(min(WORKER_POLL_SECONDS, max(deadline - time.monotonic(), 0)))

    def _future_result(self, future: Future[PingResult], target: str) -> PingResult:
        try:
            return future.result()
        except Exception:
            return PingResult(target, False, None, STATUS_ERROR, datetime.now())

    def _ping_target(self, target: str) -> PingResult:
        if self._ping_probe_pool is not None:
            return self._ping_probe_pool.ping(target)
        return self._new_ping_probe().ping(target)

    def _ping_hop(self, target: str) -> PingResult:
        if self._hop_ping_probe_pool is not None:
            return self._hop_ping_probe_pool.ping(target)
        return self._new_hop_ping_probe().ping(target)

    def _start_trace_refresh(self, executor: ThreadPoolExecutor) -> Future[list[HopInfo]]:
        """tracert는 느릴 수 있으므로 별도 Future로 시작하고 ping 루프는 계속 돌립니다."""

        self._tracert_status = "탐색 중"
        return executor.submit(self._trace_hops)

    def _uses_full_route(self) -> bool:
        return self.measurement_mode == MEASUREMENT_MODE_FULL_ROUTE

    def _is_target_paused(self, target: str) -> bool:
        with self._control_lock:
            return target in self._paused_targets

    def _paused_snapshot(self, snapshot):
        return replace(snapshot, current_latency_ms=None, status=STATUS_PAUSED)

    def _trace_hops(self) -> list[HopInfo]:
        if self.traceroute_probe:
            hops = self.traceroute_probe.trace(
                self.target,
                timeout_ms=self.timeout_ms,
                stop_event=self._stop_event,
            )
        else:
            hops = run_traceroute(self.target, timeout_ms=self.timeout_ms, stop_event=self._stop_event)
        return ensure_target_hop(hops, self.target, self.target)

    def _collect_trace_result(
        self,
        trace_future: Future[list[HopInfo]],
        *,
        first_check: bool = False,
    ) -> tuple[MetricsSession | None, list[HopInfo]]:
        try:
            hops = trace_future.result(timeout=0.05 if first_check else 0)
        except FutureTimeoutError:
            return None, []
        except Exception:
            self.status_message.emit("tracert 실행 중 오류가 발생했습니다. 대상 Ping은 계속 측정합니다.")
            hops = [
                HopInfo(
                    index=1,
                    address=self.target,
                    hostname="Target",
                    timed_out=False,
                    is_target=True,
                )
            ]
        if self._stop_event.is_set():
            return None, []
        if not hops:
            hops = ensure_target_hop([], self.target, self.target)
        self.trace_completed.emit(hops)
        self.status_message.emit("측정 중...")
        return MetricsSession(hops, recent_observation_limit=RECENT_OBSERVATION_LIMIT), hops

    def _ping_unique_targets(
        self,
        hops: list[HopInfo],
        on_result: Callable[[PingResult, int, int, int], None] | None = None,
    ) -> dict[str, PingResult]:
        registered_targets = set(self.targets or [self.target])
        targets = {hop.ping_target for hop in hops if hop.ping_target}
        targets.update(registered_targets)
        clean_targets = sorted(target for target in targets if target)

        results: dict[str, PingResult] = {}
        max_workers = min(max(len(clean_targets), 1), 16)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(
                    (self._new_ping_probe() if target in registered_targets else self._new_hop_ping_probe()).ping,
                    target,
                ): target
                for target in clean_targets
            }
            for future in as_completed(future_map):
                target = future_map[future]
                try:
                    results[target] = future.result()
                except Exception:
                    results[target] = PingResult(target, False, None, STATUS_ERROR, datetime.now())
                if on_result is not None:
                    completed = len(results)
                    total = len(future_map)
                    on_result(results[target], completed, total, total - completed)
        return results

    def _new_ping_probe(self):
        """선택된 probe engine에 맞춰 새 ping probe를 만듭니다."""

        if self.ping_probe_factory:
            return self.ping_probe_factory(self.timeout_ms)
        if self.probe_engine == PROBE_ENGINE_TCP_CONNECT:
            return TcpConnectRunner(timeout_ms=self.timeout_ms, port=self.tcp_port)
        return CommandPingRunner(timeout_ms=self.timeout_ms)

    def _new_hop_ping_probe(self):
        if self.ping_probe_factory:
            return self.ping_probe_factory(self.timeout_ms)
        return CommandPingRunner(timeout_ms=self.timeout_ms)

    def _session_measurement_mode(self) -> str:
        if self.probe_engine == PROBE_ENGINE_TCP_CONNECT:
            return f"{self.measurement_mode}:{self.probe_engine}:port{self.tcp_port}"
        return f"{self.measurement_mode}:{self.probe_engine}"

    def _target_probe_label(self) -> str:
        if self.probe_engine == PROBE_ENGINE_TCP_CONNECT:
            return f"TCP Connect:{self.tcp_port}"
        return "ICMP"

    def _route_probe_label(self) -> str:
        if not self._uses_full_route():
            return "disabled"
        return "tracert/ICMP"

    def _normalize_targets(self, targets: list[str] | None) -> list[str]:
        """입력 대상 목록에서 빈 값과 중복을 제거하되 입력 순서는 유지합니다."""

        source = targets if targets is not None else [self.target]
        normalized, _invalid = parse_ipv4_targets("\n".join(source))
        return normalized[:MAX_IPV4_TARGETS]

    def _format_ping_progress(self, completed: int, total: int, *, waiting_for_trace: bool) -> str:
        pending = max(total - completed, 0)
        trace_prefix = "경로 탐색 중, " if waiting_for_trace else ""
        if pending:
            return f"측정 중... {trace_prefix}대상 Ping {completed}/{total}, {pending}개 응답 대기"
        return f"측정 중... {trace_prefix}대상 Ping {completed}/{total}"


def _error_code_summary(code: str, exc: Exception) -> str:
    return f"{code}: {type(exc).__name__}"


def _merge_error_summaries(current: str, extra: str) -> str:
    if not current:
        return extra
    if not extra or extra in current.split("; "):
        return current
    return f"{current}; {extra}"
