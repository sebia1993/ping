from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from app.core.models import HopObservation, MetricSnapshot
from app.storage.export_annotations import ExportAnnotation


def export_xlsx(
    path: Path,
    target: str,
    observations: Iterable[HopObservation],
    snapshots: Iterable[MetricSnapshot],
    analysis: list[str],
    annotations: list[ExportAnnotation] | None = None,
) -> None:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
    except ImportError as exc:
        raise RuntimeError("XLSX 저장에는 openpyxl 패키지가 필요합니다.") from exc

    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()

    summary = workbook.active
    summary.title = "Summary"
    summary["A1"] = "Network Path Diagnostics"
    summary["A1"].font = Font(bold=True, size=14)
    summary["A3"] = "대상IP"
    summary["B3"] = target
    summary["A5"] = "Analysis"
    summary["A5"].font = Font(bold=True)
    for row_index, line in enumerate(analysis, start=6):
        summary.cell(row=row_index, column=1, value=line)
    summary.column_dimensions["A"].width = 120
    summary.column_dimensions["B"].width = 35

    if annotations:
        _write_annotations_sheet(workbook, annotations, Font, PatternFill)

    hops_sheet = workbook.create_sheet("Hop Metrics")
    hop_headers = [
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
    ]
    hops_sheet.append(hop_headers)
    for cell in hops_sheet[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="D9EAF7")
    for snapshot in snapshots:
        failed = snapshot.sent - snapshot.received
        hops_sheet.append([
            None,
            snapshot.address,
            "대상" if snapshot.is_target else "Hop",
            snapshot.hop_index,
            snapshot.status,
            snapshot.current_latency_ms,
            snapshot.avg_latency_ms,
            snapshot.min_latency_ms,
            snapshot.max_latency_ms,
            snapshot.loss_percent,
            snapshot.sent,
            snapshot.received,
            failed,
            snapshot.recent_loss_percent,
            snapshot.jitter_ms,
        ])
    _autosize(hops_sheet)

    samples_sheet = workbook.create_sheet("Samples")
    samples_sheet.append(["측정시간", "대상IP", "구분", "Hop", "성공", "지연시간", "상태"])
    for cell in samples_sheet[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="D9EAF7")
    for observation in observations:
        samples_sheet.append([
            observation.timestamp.isoformat(timespec="seconds"),
            observation.address,
            "대상" if observation.is_target else "Hop",
            observation.hop_index,
            observation.success,
            observation.latency_ms,
            observation.status,
        ])
    _autosize(samples_sheet)

    workbook.save(path)


def _write_annotations_sheet(workbook, annotations: list[ExportAnnotation], font_cls, fill_cls) -> None:
    annotations_sheet = workbook.create_sheet("Annotations")
    annotations_sheet.append(["start", "end", "source", "severity", "title", "message"])
    for cell in annotations_sheet[1]:
        cell.font = font_cls(bold=True)
        cell.fill = fill_cls("solid", fgColor="FDECC8")
    for annotation in annotations:
        annotations_sheet.append([
            annotation.start.isoformat(timespec="seconds"),
            annotation.end.isoformat(timespec="seconds"),
            annotation.source,
            annotation.severity,
            annotation.title,
            annotation.message,
        ])
    _autosize(annotations_sheet)


def _autosize(sheet) -> None:
    from openpyxl.utils import get_column_letter

    for column_cells in sheet.columns:
        max_length = 0
        column = get_column_letter(column_cells[0].column)
        for cell in column_cells:
            value = "" if cell.value is None else str(cell.value)
            max_length = max(max_length, len(value))
        sheet.column_dimensions[column].width = min(max(max_length + 2, 10), 60)
