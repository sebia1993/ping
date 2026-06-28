from __future__ import annotations

import csv
from collections.abc import Iterable
from pathlib import Path

from app.core.models import HopObservation, MetricSnapshot
from app.storage.atomic_write import atomic_write_path
from app.storage.export_annotations import ExportAnnotation


def export_csv(
    path: Path,
    observations: Iterable[HopObservation],
    snapshots: Iterable[MetricSnapshot],
    analysis: list[str],
    annotations: list[ExportAnnotation] | None = None,
) -> None:
    atomic_write_path(
        path,
        lambda temp_path: _write_export_csv(temp_path, observations, snapshots, analysis, annotations),
    )


def _write_export_csv(
    path: Path,
    observations: Iterable[HopObservation],
    snapshots: Iterable[MetricSnapshot],
    analysis: list[str],
    annotations: list[ExportAnnotation] | None = None,
) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Summary"])
        for line in analysis:
            writer.writerow([line])

        _write_annotations(writer, annotations or [])

        writer.writerow([])
        writer.writerow([
            "측정시간",
            "대상IP",
            "구분",
            "Hop",
            "상태",
            "지연시간",
            "평균지연",
            "최소지연",
            "최대지연",
            "손실률",
            "송신",
            "수신",
            "실패",
            "최근손실률",
            "Jitter",
        ])
        for snapshot in snapshots:
            failed = snapshot.sent - snapshot.received
            writer.writerow([
                "",
                snapshot.address or "",
                "대상" if snapshot.is_target else "Hop",
                snapshot.hop_index,
                snapshot.status,
                _fmt(snapshot.current_latency_ms),
                _fmt(snapshot.avg_latency_ms),
                _fmt(snapshot.min_latency_ms),
                _fmt(snapshot.max_latency_ms),
                f"{snapshot.loss_percent:.1f}",
                snapshot.sent,
                snapshot.received,
                failed,
                f"{snapshot.recent_loss_percent:.1f}",
                _fmt(snapshot.jitter_ms),
            ])

        writer.writerow([])
        writer.writerow(["측정시간", "대상IP", "구분", "Hop", "성공", "지연시간", "상태"])
        for observation in observations:
            writer.writerow([
                observation.timestamp.isoformat(timespec="seconds"),
                observation.address or "",
                "대상" if observation.is_target else "Hop",
                observation.hop_index,
                observation.success,
                _fmt(observation.latency_ms),
                observation.status,
            ])


def _write_annotations(writer: object, annotations: list[ExportAnnotation]) -> None:
    if not annotations:
        return
    writer.writerow([])
    writer.writerow(["Annotations"])
    writer.writerow(["start", "end", "source", "severity", "title", "message"])
    for annotation in annotations:
        writer.writerow([
            annotation.start.isoformat(timespec="seconds"),
            annotation.end.isoformat(timespec="seconds"),
            annotation.source,
            annotation.severity,
            annotation.title,
            annotation.message,
        ])


def _fmt(value: float | None) -> str:
    return "" if value is None else f"{value:.1f}"
