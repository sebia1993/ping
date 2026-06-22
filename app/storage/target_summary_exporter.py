from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path


TARGET_SUMMARY_HEADERS = [
    "target",
    "status",
    "current_ms",
    "avg_ms",
    "min_ms",
    "max_ms",
    "loss_percent",
    "recent_loss_percent",
    "sent",
    "received",
    "failed",
    "timeout_count",
    "jitter_ms",
    "samples",
    "interval_seconds",
    "interval_source",
    "score",
]


@dataclass(frozen=True)
class TargetSummaryExportRow:
    target: str
    status: str
    current_latency_ms: float | None
    avg_latency_ms: float | None
    min_latency_ms: float | None
    max_latency_ms: float | None
    loss_percent: float
    recent_loss_percent: float
    sent: int
    received: int
    failed: int
    timeout_count: int
    jitter_ms: float | None
    samples: int
    score: float
    interval_seconds: int | None = None
    interval_source: str = ""


def export_target_summary_csv(path: Path, rows: list[TargetSummaryExportRow]) -> Path:
    if path.suffix.lower() != ".csv":
        path = path.with_suffix(".csv")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(TARGET_SUMMARY_HEADERS)
        for row in rows:
            writer.writerow([
                row.target,
                row.status,
                _fmt(row.current_latency_ms),
                _fmt(row.avg_latency_ms),
                _fmt(row.min_latency_ms),
                _fmt(row.max_latency_ms),
                f"{row.loss_percent:.1f}",
                f"{row.recent_loss_percent:.1f}",
                row.sent,
                row.received,
                row.failed,
                row.timeout_count,
                _fmt(row.jitter_ms),
                row.samples,
                "" if row.interval_seconds is None else row.interval_seconds,
                row.interval_source,
                f"{row.score:.3f}",
            ])
    return path


def _fmt(value: float | None) -> str:
    return "" if value is None else f"{value:.1f}"
