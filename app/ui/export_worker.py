from __future__ import annotations

import threading
from collections.abc import Iterable
from datetime import datetime
from itertools import chain
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from app.core.analyzer import analyze_path
from app.core.models import HopObservation, MetricSnapshot
from app.core.observation_stats import build_focus_snapshots
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
from app.utils.diagnostics import operation_failure


EXPORT_EMPTY_STATISTICS_MESSAGE = "선택한 내보내기 범위에 해당하는 통계 샘플이 없습니다."
EXPORT_WRITE_FAILED_CODE = "EXPORT_WRITE_FAILED"
EXPORT_UNEXPECTED_ERROR_CODE = "EXPORT_UNEXPECTED_ERROR"
EXPORT_CANCELLED_CODE = "EXPORT_CANCELLED"


class _ExportCancelled(RuntimeError):
    pass


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
        derive_session_summary: bool = False,
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
        self.derive_session_summary = derive_session_summary
        self._cancel_requested = threading.Event()

    def run(self) -> None:
        try:
            self.status_message.emit(f"{self.kind.upper()} 저장 중...")
            if self.derive_session_summary:
                self._derive_summary_from_session()
            observations = self._interruptible_observations(self._source_observations())
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
            if self._is_cancel_requested():
                raise _ExportCancelled
        except _ExportCancelled:
            self.status_message.emit(EXPORT_CANCELLED_CODE)
            return
        except Exception as exc:
            operation_failure(
                _export_error_code(exc),
                "export.run",
                exc,
                target=self.target,
                session_path=self.session_log_path,
            )
            self.error_message.emit(_format_export_error(exc))
            return
        self.export_completed.emit(str(self.path))

    def request_cancel(self) -> None:
        self._cancel_requested.set()
        self.requestInterruption()

    def _source_observations(self) -> Iterable[HopObservation]:
        if self.observations_override is not None:
            return iter(self.observations_override)
        if self.focus_range is not None:
            start, end = self.focus_range
            return iter_observations_in_range(self.session_log_path, start, end, strict=True)
        return iter_observations(self.session_log_path, strict=True)

    def _derive_summary_from_session(self) -> None:
        if self.session_log_path is None:
            raise OSError("session log path is unavailable")
        snapshot_set = build_focus_snapshots(
            self._interruptible_observations(self._source_observations()),
            current_target=self.target,
        )
        self.snapshots = [*snapshot_set.hop_snapshots, *snapshot_set.target_snapshots]
        self.analysis = analyze_path(snapshot_set.hop_snapshots, snapshot_set.target_snapshot)

    def _interruptible_observations(
        self,
        observations: Iterable[HopObservation],
    ) -> Iterable[HopObservation]:
        for observation in observations:
            if self._is_cancel_requested():
                raise _ExportCancelled
            yield observation

    def _is_cancel_requested(self) -> bool:
        return self._cancel_requested.is_set() or self.isInterruptionRequested()

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


def _export_error_code(exc: Exception) -> str:
    if isinstance(exc, RuntimeError) and str(exc) == EXPORT_EMPTY_STATISTICS_MESSAGE:
        return "EXPORT_EMPTY_STATISTICS"
    if isinstance(exc, OSError):
        return EXPORT_WRITE_FAILED_CODE
    return EXPORT_UNEXPECTED_ERROR_CODE
