from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from itertools import chain
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from app.core.models import HopObservation, MetricSnapshot
from app.storage.csv_exporter import export_csv
from app.storage.excel_exporter import export_xlsx
from app.storage.export_annotations import ExportAnnotation
from app.storage.report_writer import write_html_report, write_text_report
from app.storage.session_log import iter_observations, iter_observations_in_range
from app.storage.statistics_exporter import (
    StatisticsExportOptions,
    export_statistics_csv,
    export_statistics_xlsx,
)


EXPORT_EMPTY_STATISTICS_MESSAGE = "선택한 내보내기 범위에 해당하는 통계 샘플이 없습니다."
EXPORT_WRITE_FAILED_CODE = "EXPORT_WRITE_FAILED"
EXPORT_UNEXPECTED_ERROR_CODE = "EXPORT_UNEXPECTED_ERROR"


class ExportWorker(QThread):
    status_message = Signal(str)
    export_completed = Signal(str)
    error_message = Signal(str)

    def __init__(
        self,
        *,
        kind: str,
        path: Path,
        target: str,
        session_log_path: Path | None,
        snapshots: list[MetricSnapshot],
        analysis: list[str],
        annotations: list[ExportAnnotation] | None = None,
        focus_range: tuple[datetime, datetime] | None = None,
        observations_override: list[HopObservation] | None = None,
        statistics_options: StatisticsExportOptions | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.kind = kind
        self.path = path
        self.target = target
        self.session_log_path = session_log_path
        self.snapshots = snapshots
        self.analysis = analysis
        self.annotations = list(annotations or [])
        self.focus_range = focus_range
        self.observations_override = observations_override
        self.statistics_options = statistics_options or StatisticsExportOptions()

    def run(self) -> None:
        try:
            self.status_message.emit(f"{self.kind.upper()} 저장 중...")
            if self.observations_override is not None:
                observations = iter(self.observations_override)
            elif self.focus_range is not None:
                start, end = self.focus_range
                observations = iter_observations_in_range(self.session_log_path, start, end)
            else:
                observations = iter_observations(self.session_log_path)
            if self.kind == "csv":
                export_csv(self.path, observations, self.snapshots, self.analysis, self.annotations)
            elif self.kind == "xlsx":
                export_xlsx(self.path, self.target, observations, self.snapshots, self.analysis, self.annotations)
            elif self.kind == "stats_csv":
                observations = self._non_empty_statistics_observations(observations)
                export_statistics_csv(self.path, observations, self.statistics_options)
            elif self.kind == "stats_xlsx":
                observations = self._non_empty_statistics_observations(observations)
                export_statistics_xlsx(self.path, self.target, observations, self.statistics_options)
            elif self.kind == "txt":
                write_text_report(
                    self.path,
                    self.target,
                    self.snapshots,
                    self.analysis,
                    self.annotations,
                    self.focus_range,
                )
            elif self.kind == "html":
                write_html_report(
                    self.path,
                    self.target,
                    self.snapshots,
                    self.analysis,
                    self.annotations,
                    self.focus_range,
                )
            else:
                raise RuntimeError(f"지원하지 않는 저장 형식입니다: {self.kind}")
        except Exception as exc:
            self.error_message.emit(_format_export_error(exc))
            return
        self.export_completed.emit(str(self.path))

    @staticmethod
    def _non_empty_statistics_observations(
        observations: Iterable[HopObservation],
    ) -> Iterable[HopObservation]:
        iterator = iter(observations)
        try:
            first = next(iterator)
        except StopIteration as exc:
            raise RuntimeError(EXPORT_EMPTY_STATISTICS_MESSAGE) from exc
        return chain([first], iterator)


def _format_export_error(exc: Exception) -> str:
    message = str(exc)
    if isinstance(exc, RuntimeError) and message == EXPORT_EMPTY_STATISTICS_MESSAGE:
        return message
    if isinstance(exc, OSError):
        return f"{EXPORT_WRITE_FAILED_CODE}: {type(exc).__name__}: {message}"
    return f"{EXPORT_UNEXPECTED_ERROR_CODE}: {type(exc).__name__}: {message}"
