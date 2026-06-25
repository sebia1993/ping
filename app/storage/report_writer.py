from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path

from app.core.models import MetricSnapshot
from app.storage.export_annotations import ExportAnnotation


@dataclass(frozen=True)
class CauseEvidence:
    code: str
    evidence: str
    action: str


def write_text_report(
    path: Path,
    target: str,
    snapshots: list[MetricSnapshot],
    analysis: list[str],
    annotations: list[ExportAnnotation] | None = None,
    focus_range: tuple[datetime, datetime] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [
        "MultiPingCheck Report",
        f"Target: {target}",
        f"대상IP: {target}",
    ]
    if focus_range is not None:
        lines.append(f"Range: {_format_range(focus_range)}")
    cause_evidence = _cause_evidence_items(analysis)
    if cause_evidence:
        lines.extend(["", "Cause Evidence Summary:"])
        for item in cause_evidence:
            lines.append(f"- {item.code}: Evidence: {item.evidence} Action: {item.action}")
    lines.extend(["", "Analysis:"])
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


def write_html_report(
    path: Path,
    target: str,
    snapshots: list[MetricSnapshot],
    analysis: list[str],
    annotations: list[ExportAnnotation] | None = None,
    focus_range: tuple[datetime, datetime] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() != ".html":
        path = path.with_suffix(".html")
    html = "\n".join([
        "<!doctype html>",
        '<html lang="ko">',
        "<head>",
        '<meta charset="utf-8">',
        "<title>MultiPingCheck Report</title>",
        "<style>",
        "body{font-family:Segoe UI,Arial,sans-serif;margin:32px;color:#111827;line-height:1.45}",
        "h1{font-size:24px;margin:0 0 8px}",
        "h2{font-size:18px;margin:28px 0 8px;border-bottom:1px solid #d1d5db;padding-bottom:4px}",
        ".meta{color:#4b5563;margin:2px 0}",
        "table{border-collapse:collapse;width:100%;margin-top:8px;font-size:13px}",
        "th,td{border:1px solid #d1d5db;padding:6px 8px;text-align:left}",
        "th{background:#f3f4f6}",
        ".critical{color:#b91c1c;font-weight:600}",
        ".warning{color:#92400e;font-weight:600}",
        ".ok{color:#047857;font-weight:600}",
        "@media print{body{margin:18mm}}",
        "</style>",
        "</head>",
        "<body>",
        "<h1>MultiPingCheck Report</h1>",
        f'<p class="meta"><strong>Target:</strong> {escape(target or "-")}</p>',
        f'<p class="meta"><strong>Range:</strong> {escape(_format_range(focus_range))}</p>',
        "<h2>Cause Evidence Summary</h2>",
        _html_cause_table(_cause_evidence_items(analysis)),
        "<h2>Analysis</h2>",
        _html_list(analysis),
        "<h2>Hop Metrics</h2>",
        _html_hop_table(snapshots),
        "<h2>Evidence Annotations</h2>",
        _html_annotation_table(annotations or []),
        "</body>",
        "</html>",
    ])
    path.write_text(html + "\n", encoding="utf-8")


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def _format_range(focus_range: tuple[datetime, datetime] | None) -> str:
    if focus_range is None:
        return "All available samples"
    start, end = focus_range
    return f"{start.isoformat(timespec='seconds')} - {end.isoformat(timespec='seconds')}"


def _html_list(items: list[str]) -> str:
    if not items:
        return "<p>No analysis messages.</p>"
    return "<ul>" + "".join(f"<li>{escape(item)}</li>" for item in items) + "</ul>"


def _cause_evidence_items(analysis: list[str]) -> list[CauseEvidence]:
    items: list[CauseEvidence] = []
    seen: set[tuple[str, str, str]] = set()
    for line in analysis:
        if not line.startswith("CAUSE_"):
            continue
        parsed = _parse_cause_evidence_line(line)
        if parsed is None:
            continue
        code, evidence, action = parsed
        item = CauseEvidence(code.strip(), evidence.strip(), action.strip())
        key = (item.code, item.evidence, item.action)
        if key in seen:
            continue
        seen.add(key)
        items.append(item)
    return items


def _parse_cause_evidence_line(line: str) -> tuple[str, str, str] | None:
    formats = (
        (": Evidence:", " Action:"),
        (": 근거:", " 조치:"),
    )
    for evidence_marker, action_marker in formats:
        try:
            code, remainder = line.split(evidence_marker, 1)
            evidence, action = remainder.split(action_marker, 1)
        except ValueError:
            continue
        return code, evidence, action
    return None


def _html_cause_table(items: list[CauseEvidence]) -> str:
    if not items:
        return "<p>No cause evidence found in the current analysis.</p>"
    rows = [
        "<table>",
        "<thead><tr><th>Cause Code</th><th>Evidence</th><th>Recommended Action</th></tr></thead>",
        "<tbody>",
    ]
    for item in items:
        rows.append(
            "<tr>"
            f"<td>{escape(item.code)}</td>"
            f"<td>{escape(item.evidence)}</td>"
            f"<td>{escape(item.action)}</td>"
            "</tr>"
        )
    rows.extend(["</tbody>", "</table>"])
    return "".join(rows)


def _html_hop_table(snapshots: list[MetricSnapshot]) -> str:
    if not snapshots:
        return "<p>No hop metrics.</p>"
    rows = [
        "<table>",
        "<thead><tr><th>Hop</th><th>Address</th><th>Status</th><th>Loss</th><th>Avg</th><th>Max</th><th>Sent</th><th>Received</th></tr></thead>",
        "<tbody>",
    ]
    for snapshot in snapshots:
        rows.append(
            "<tr>"
            f"<td>{snapshot.hop_index}</td>"
            f"<td>{escape(snapshot.address or 'Timeout')}</td>"
            f'<td class="{_status_class(snapshot.status)}">{escape(snapshot.status)}</td>'
            f"<td>{snapshot.loss_percent:.1f}%</td>"
            f"<td>{_fmt(snapshot.avg_latency_ms)} ms</td>"
            f"<td>{_fmt(snapshot.max_latency_ms)} ms</td>"
            f"<td>{snapshot.sent}</td>"
            f"<td>{snapshot.received}</td>"
            "</tr>"
        )
    rows.extend(["</tbody>", "</table>"])
    return "".join(rows)


def _html_annotation_table(annotations: list[ExportAnnotation]) -> str:
    if not annotations:
        return "<p>No evidence annotations.</p>"
    rows = [
        "<table>",
        "<thead><tr><th>Start</th><th>End</th><th>Source</th><th>Severity</th><th>Title</th><th>Message</th></tr></thead>",
        "<tbody>",
    ]
    for annotation in annotations:
        rows.append(
            "<tr>"
            f"<td>{annotation.start.isoformat(timespec='seconds')}</td>"
            f"<td>{annotation.end.isoformat(timespec='seconds')}</td>"
            f"<td>{escape(annotation.source)}</td>"
            f'<td class="{_status_class(annotation.severity)}">{escape(annotation.severity or "-")}</td>'
            f"<td>{escape(annotation.title)}</td>"
            f"<td>{escape(annotation.message)}</td>"
            "</tr>"
        )
    rows.extend(["</tbody>", "</table>"])
    return "".join(rows)


def _status_class(status: str) -> str:
    normalized = status.lower()
    if "critical" in normalized or "timeout" in normalized:
        return "critical"
    if "warning" in normalized:
        return "warning"
    if "ok" in normalized:
        return "ok"
    return ""
