from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QTableWidgetItem

from app.core.models import STATUS_OK, STATUS_TIMEOUT, HopObservation, MetricSnapshot
from app.ui.main_window import MainWindow


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "docs" / "images"


def _snapshot(
    address: str,
    *,
    current: float | None,
    average: float | None,
    loss: float,
    jitter: float | None,
    status: str,
) -> MetricSnapshot:
    samples = 120
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
        min_latency_ms=None if current is None else max(current - 1.2, 0.1),
        max_latency_ms=None if current is None else current + 18.0,
        loss_percent=loss,
        recent_loss_percent=loss,
        jitter_ms=jitter,
        status=status,
        is_target=True,
    )


def _history(address: str, base_ms: float, *, timeout_every: int = 0) -> list[HopObservation]:
    start = datetime(2026, 8, 21, 19, 55, 0)
    rows: list[HopObservation] = []
    for index in range(60):
        timeout = bool(timeout_every and (index + 1) % timeout_every == 0)
        rows.append(
            HopObservation(
                timestamp=start + timedelta(seconds=index),
                hop_index=0,
                address=address,
                hostname=None,
                success=not timeout,
                latency_ms=None if timeout else base_ms + ((index % 7) - 3) * 0.4,
                status=STATUS_TIMEOUT if timeout else STATUS_OK,
                is_target=True,
            )
        )
    return rows


def _save(window: MainWindow, path: Path, app: QApplication) -> None:
    window.resize(1460, 940)
    window.show()
    app.processEvents()
    image = window.grab()
    if image.isNull() or image.width() < 1200 or image.height() < 700:
        raise RuntimeError(f"문서 화면 캡처 실패: {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not image.save(str(path), "PNG"):
        raise RuntimeError(f"PNG 저장 실패: {path}")
    window.hide()
    app.processEvents()


def _render_measurement(app: QApplication) -> None:
    window = MainWindow()
    targets = ["192.0.2.10", "198.51.100.20", "203.0.113.30"]
    snapshots = [
        _snapshot(targets[0], current=4.2, average=4.5, loss=0.0, jitter=0.8, status=STATUS_OK),
        _snapshot(targets[1], current=28.4, average=24.7, loss=5.8, jitter=11.6, status=STATUS_OK),
        _snapshot(targets[2], current=None, average=87.5, loss=25.0, jitter=31.4, status=STATUS_TIMEOUT),
    ]
    observations = [
        *_history(targets[0], 4.5),
        *_history(targets[1], 24.0, timeout_every=17),
        *_history(targets[2], 88.0, timeout_every=4),
    ]

    window.current_target = targets[0]
    window.current_targets = targets
    window.target_snapshots = snapshots
    window.target_snapshot = snapshots[0]
    window.target_aliases = {
        targets[0]: "Gateway-A",
        targets[1]: "Core-Service",
        targets[2]: "Remote-Site",
    }
    window.target_input.setPlainText("\n".join(targets))
    window._remember_live_graph_observations(observations)
    window._sync_target_graph_rows(snapshots)
    window._update_target_summary(snapshots[0])
    window._update_all_targets_summary(snapshots)
    window.status_label.setText("측정 중 · 3개 IP · 1초 주기 · ICMP")
    window._set_state_chip("측정", "success")
    window.target_input.hide()
    window.running_target_summary_label.setText("Gateway-A · Core-Service · Remote-Site")
    window.running_target_summary_label.show()
    window.graph_panel.show()
    window.right_panel.hide()
    _save(window, OUTPUT_DIR / "multiping-main.png", app)
    window.close()


def _render_sessions(app: QApplication) -> None:
    window = MainWindow()
    window.target_input.setPlainText("192.0.2.10\n198.51.100.20\n203.0.113.30")
    window.right_panel.show()
    window.graph_panel.hide()
    window.status_label.setText("저장된 측정 세션 · 비식별 문서 예시")

    rows = [
        ["2026-08-21 17:00", "192.0.2.10", "보관", "3,600", "1초", "ICMP"],
        ["2026-08-21 17:45", "198.51.100.20", "보관", "5,400", "1초", "ICMP"],
        ["2026-08-21 18:30", "203.0.113.30", "보관", "7,200", "1초", "ICMP"],
    ]
    window.session_table.setRowCount(len(rows))
    for row_index, values in enumerate(rows):
        for column_index, value in enumerate(values):
            if column_index >= window.session_table.columnCount():
                break
            window.session_table.setItem(row_index, column_index, QTableWidgetItem(value))
    window.main_splitter.setSizes([650, 810])
    _save(window, OUTPUT_DIR / "multiping-sessions.png", app)
    window.close()


def main() -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    _render_measurement(app)
    _render_sessions(app)

    for path in (OUTPUT_DIR / "multiping-main.png", OUTPUT_DIR / "multiping-sessions.png"):
        if not path.is_file() or path.stat().st_size < 10_000:
            raise RuntimeError(f"생성된 PNG가 유효하지 않습니다: {path}")
        print(f"generated {path.relative_to(ROOT)} ({path.stat().st_size} bytes)", flush=True)

    app.quit()
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
