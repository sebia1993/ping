from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ExportAnnotation:
    start: datetime
    end: datetime
    source: str
    severity: str
    title: str
    message: str

    @property
    def timestamp(self) -> datetime:
        return self.end


def annotations_in_range(
    annotations: list[ExportAnnotation],
    focus_range: tuple[datetime, datetime] | None,
) -> list[ExportAnnotation]:
    if focus_range is None:
        return list(annotations)
    start, end = focus_range
    if end < start:
        start, end = end, start
    return [annotation for annotation in annotations if annotation.start <= end and annotation.end >= start]
