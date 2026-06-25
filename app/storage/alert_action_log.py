from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from app.core.alerts import AlertEvent, is_route_alert_key


ALERT_ACTION_HEADERS = [
    "timestamp",
    "start",
    "end",
    "source",
    "severity",
    "title",
    "message",
    "actions",
]


def alert_action_log_path_for_session(session_log_path: Path | None) -> Path | None:
    if session_log_path is None:
        return None
    return session_log_path.with_name(f"{session_log_path.stem}.alerts.csv")


def append_alert_action(
    path: Path | None,
    event: AlertEvent,
    *,
    actions: list[str],
    source: str | None = None,
) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=ALERT_ACTION_HEADERS)
        if write_header:
            writer.writeheader()
        writer.writerow(
            {
                "timestamp": _format_dt(event.timestamp),
                "start": _format_dt(event.start),
                "end": _format_dt(event.end),
                "source": source or ("route" if is_route_alert_key(event.key) else "alert"),
                "severity": event.severity,
                "title": event.title,
                "message": event.message,
                "actions": ";".join(actions),
            }
        )


def read_alert_actions(path: Path | None) -> list[dict[str, str]]:
    if path is None or not path.exists():
        return []
    try:
        with path.open("r", newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))
    except (OSError, csv.Error):
        return []


def _format_dt(value: datetime) -> str:
    return value.isoformat(timespec="seconds")
