from __future__ import annotations

import csv

from app.storage.target_summary_exporter import TargetSummaryExportRow, export_target_summary_csv


def test_export_target_summary_csv_writes_operator_summary(tmp_path) -> None:
    saved_path = export_target_summary_csv(
        tmp_path / "targets",
        [
            TargetSummaryExportRow(
                target="198.51.100.10",
                status="CRITICAL",
                current_latency_ms=None,
                avg_latency_ms=44.2,
                min_latency_ms=40.1,
                max_latency_ms=55.7,
                loss_percent=25.0,
                recent_loss_percent=50.0,
                sent=4,
                received=3,
                failed=1,
                timeout_count=1,
                jitter_ms=7.4,
                samples=4,
                score=50107.4,
            )
        ],
    )

    assert saved_path == tmp_path / "targets.csv"
    with saved_path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))

    assert rows == [
        {
            "target": "198.51.100.10",
            "status": "CRITICAL",
            "current_ms": "",
            "avg_ms": "44.2",
            "min_ms": "40.1",
            "max_ms": "55.7",
            "loss_percent": "25.0",
            "recent_loss_percent": "50.0",
            "sent": "4",
            "received": "3",
            "failed": "1",
            "timeout_count": "1",
            "jitter_ms": "7.4",
            "samples": "4",
            "score": "50107.400",
        }
    ]
