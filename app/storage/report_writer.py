from __future__ import annotations

from pathlib import Path

from app.core.models import MetricSnapshot
from app.storage.export_annotations import ExportAnnotation


def write_text_report(
    path: Path,
    target: str,
    snapshots: list[MetricSnapshot],
    analysis: list[str],
    annotations: list[ExportAnnotation] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [
        "Network Path Diagnostics Report",
        f"대상IP: {target}",
        "",
        "Analysis:",
    ]
    lines.extend(f"- {line}" for line in analysis)
    if annotations:
        lines.extend(["", "Evidence Annotations:"])
        for annotation in annotations:
            lines.append(
                "[{start} - {end}] {source} {severity} {title}: {message}".format(
                    start=annotation.start.isoformat(timespec="seconds"),
                    end=annotation.end.isoformat(timespec="seconds"),
                    source=annotation.source,
                    severity=annotation.severity.upper() if annotation.severity else "-",
                    title=annotation.title,
                    message=annotation.message,
                )
            )
    lines.extend(["", "Hop Metrics:"])
    for snapshot in snapshots:
        lines.append(
            "Hop {hop} {address} loss={loss:.1f}% avg={avg}ms max={max_ms}ms status={status}".format(
                hop=snapshot.hop_index,
                address=snapshot.address or "Timeout",
                loss=snapshot.loss_percent,
                avg=_fmt(snapshot.avg_latency_ms),
                max_ms=_fmt(snapshot.max_latency_ms),
                status=snapshot.status,
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"
