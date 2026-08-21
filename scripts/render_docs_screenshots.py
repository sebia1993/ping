from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.core.models import STATUS_OK, STATUS_TIMEOUT, HopObservation, MetricSnapshot
from app.storage.session_index import SESSION_STATE_ARCHIVED, SessionIndexStore
from app.ui.main_window import MainWindow


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "docs" / "images"


def _snapshot(
    address: str,
    *,
    current: float | None,
    average: float | None,
    minimum: float | None,
    maximum: float | None,
    loss: float,
    jitter: float | None,
    status: str,
    samples: int = 120,
) -> MetricSnapshot:
    received = round(samples * (100.0 - loss) / 100.0)
    return MetricSnapshot(
        hop_index=0,
        address=address,
        hostname=None,
        samples=samples,
        sent=samples,
        received=received,
        timeout_count=samples - received,
        current_latency_ms=current,
        avg_latency_ms=average,
        min_latency_ms=minimum,
        max_latency_ms=maximum,
        loss_percent=loss,
        recent_loss_percent=loss,
        jitter_ms=jitter,
        status=status,
        is_target=True,
    )


def _observations(address: str, *, base_ms: float, count: int, timeout_every: int = 0) -> list[HopObservation]:
    start = datetime(2026, 8, 21, 19, 55, 0)
    rows: list[HopObservation] = []
    for index in range(count):
        timeout = bool(timeout_every and (index + 1) % timeout_every == 0)
        latency = None if timeout else base_ms + ((index % 9) - 4) * 0.35
        rows.append(
            HopObservation(
                timestamp=start + timedelta(seconds=index),
                hop_index=0,
                address=address,
                hostname=None,
                success=not timeout,
                latency_ms=latency,
                status=STATUS_TIMEOUT if timeout else STATUS_OK,
                is_target=True,
            )
        )
    return rows


def _prepare_measurement_window(window: MainWindow, app: QApplication) -> None:
    targets = ["192.0.2.10", "198.51.100.20", "203.0.113.30"]
    snapshots = [
        _snapshot(
            targets[0],
            current=4.2,
            average=4.5,
            minimum=3.8,
            maximum=7.1,
            loss=0.0,
            jitter=0.8,
            status=STATUS_OK,
        ),
        _snapshot(
            targets[1],
            current=28.4,
            average=24.7,
            minimum=18.1,
            maximum=66.2,
            loss=5.8,
            jitter=11.6,
            status=STATUS_OK,
        ),
        _snapshot(
            targets[2],
            current=None,
            average=87.5,
            minimum=71.0,
            maximum=146.8,
            loss=25.0,
            jitter=31.4,
            status=STATUS_TIMEOUT,
        ),
    ]
    observations = [
        *_observations(targets[0], base_ms=4.5, count=120),
        *_observations(targets[1], base_ms=24.0, count=120, timeout_every=17),
        *_observations(targets[2], base_ms=88.0, count=120, timeout_every=4),
    ]

    window.current_target = targets[0]
    window.current_targets = list(targets)
    window.target_aliases = {
        targets[0]: "Gateway-A",
        targets[1]: "Core-Service",
        targets[2]: "Remote-Site",
    }
    window.target_input.setPlainText("\n".join(targets))
    window.on_measurement_updated(
        snapshots,
        snapshots[0],
        snapshots,
        [
            "Gateway-A는 안정적인 응답을 유지하고 있습니다.",
            "Core-Service는 일부 손실과 지연 변동이 있어 추이를 확인합니다.",
            "Remote-Site는 높은 손실이 관측되어 관련 경로와 장비 상태를 함께 확인합니다.",
        ],
        observations,
        [row for row in observations if row.address == targets[0]],
    )
    window.status_label.setText("측정 중 · 3개 IP")
    window._set_state_chip("측정", "success")
    window.target_input.hide()
    window.running_target_summary_label.setText("측정 IP 3개 · 1초 주기 · ICMP")
    window.running_target_summary_label.show()
    window.graph_panel.show()
    window.right_panel.hide()
    window.resize(1460, 940)
    window.show()
    app.processEvents()
    window._request_graph_render(force=True)
    app.processEvents()


def _prepare_session_window(window: MainWindow, app: QApplication, root: Path) -> None:
    store = SessionIndexStore.create(root)
    base_time = datetime(2026, 8, 21, 17, 0, 0)
    sessions = [
        ("192.0.2.10", 3600, "full_route"),
        ("198.51.100.20", 5400, "final_hop_only"),
        ("203.0.113.30", 7200, "final_hop_only"),
    ]
    for index, (target, samples, mode) in enumerate(sessions):
        sample_path = root / target / "2026-08" / f"session-{index}.samples.csv"
        sample_path.parent.mkdir(parents=True, exist_ok=True)
        sample_path.write_text("timestamp,hop_index,address\n", encoding="utf-8")
        started = base_time + timedelta(minutes=index * 45)
        record = store.register_session(
            target=target,
            sample_path=sample_path,
            route_path=sample_path.with_name(f"session-{index}.routes.csv"),
            started_at=started,
            interval_seconds=1,
            measurement_mode=mode,
            target_count=3,
        )
        store.add_samples(
            record.session_id,
            samples,
            started + timedelta(seconds=samples),
            segments=[sample_path],
        )
        store.finish_session(
            record.session_id,
            state=SESSION_STATE_ARCHIVED,
            ended_at=started + timedelta(seconds=samples),
        )

    window.session_index_store = store
    window._sync_sessions_box()
    window.target_input.setPlainText("192.0.2.10\n198.51.100.20\n203.0.113.30")
    window.status_label.setText("저장된 측정 세션 확인")
    window.right_panel.show()
    window.graph_panel.hide()
    window.footer_panel.hide()
    window.resize(1540, 940)
    window.show()
    window.main_splitter.setSizes([620, 860])
    app.processEvents()


def _save_window(window: MainWindow, path: Path) -> None:
    image = window.grab()
    if image.isNull():
        raise RuntimeError(f"문서 화면을 캡처하지 못했습니다: {path.name}")
    if image.width() < 1200 or image.height() < 700:
        raise RuntimeError(
            f"문서 화면 해상도가 너무 작습니다: {path.name} {image.width()}x{image.height()}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    if not image.save(str(path), "PNG"):
        raise RuntimeError(f"PNG 저장에 실패했습니다: {path}")


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    main_window = MainWindow()
    try:
        _prepare_measurement_window(main_window, app)
        _save_window(main_window, OUTPUT_DIR / "multiping-main.png")
    finally:
        main_window.close()
        app.processEvents()

    with tempfile.TemporaryDirectory(prefix="multiping-doc-session-") as tmp:
        session_window = MainWindow()
        try:
            _prepare_session_window(session_window, app, Path(tmp))
            _save_window(session_window, OUTPUT_DIR / "multiping-sessions.png")
        finally:
            session_window.close()
            app.processEvents()

    for path in (OUTPUT_DIR / "multiping-main.png", OUTPUT_DIR / "multiping-sessions.png"):
        if not path.is_file() or path.stat().st_size < 10_000:
            raise RuntimeError(f"생성된 문서 PNG가 유효하지 않습니다: {path}")
        print(f"generated {path.relative_to(ROOT)} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
