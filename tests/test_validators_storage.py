from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core.models import STATUS_OK, STATUS_TIMEOUT, HopObservation, MetricSnapshot
from app.core.alerts import AlertEvent
from app.storage.alert_action_log import append_alert_action, alert_action_log_path_for_session, read_alert_actions
from app.storage.csv_exporter import export_csv
from app.storage.excel_exporter import export_xlsx
from app.storage.export_annotations import ExportAnnotation, annotations_in_range
from app.storage.report_writer import write_html_report, write_text_report
from app.storage import session_index as session_index_module
from app.storage import session_log as session_log_module
from app.storage.session_index import (
    SESSION_STATE_ACTIVE,
    SESSION_STATE_ARCHIVED,
    SESSION_STATE_PAUSED,
    SESSION_STATE_WILL_DELETE,
    SessionIndexStore,
)
from app.storage.session_log import (
    SessionLogWriter,
    iter_observations_in_range,
    read_observations,
    session_log_directory,
    session_log_bounds,
    session_log_segment_index,
    session_log_segment_index_path,
)
from app.storage.statistics_exporter import (
    TIMEZONE_UTC,
    StatisticsExportOptions,
    export_statistics_xlsx,
    grouped_statistics,
)
from app.ui.export_worker import ExportWorker
from app.utils.filename import safe_target_name
from app.utils.validators import parse_ipv4_targets, validate_target


def test_validate_target_accepts_ipv4_only() -> None:
    assert validate_target("8.8.8.8")[0] is True
    assert validate_target("192.168.0.1")[0] is True
    assert validate_target("10.10.10.1")[0] is True


def test_validate_target_rejects_domain_ipv6_and_bad_ip() -> None:
    for value in ["https://example.com", "google.com", "test.local", "999.999.999.999", "192.168.1", "2001:db8::1"]:
        valid, message = validate_target(value)
        assert valid is False
        assert "IPv4" in message


def test_parse_ipv4_targets_ignores_blanks_and_deduplicates() -> None:
    targets, invalid = parse_ipv4_targets("\n8.8.8.8\n\n192.168.0.1\n8.8.8.8\ngoogle.com\n2001:db8::1\n")
    assert targets == ["8.8.8.8", "192.168.0.1"]
    assert invalid == ["google.com", "2001:db8::1"]


def test_parse_ipv4_targets_accepts_excel_spaces_tabs_and_commas() -> None:
    targets, invalid = parse_ipv4_targets(
        "8.8.8.8\t192.168.0.1\n"
        "10.0.0.1,10.0.0.2 10.0.0.3\n"
        "10.0.0.2"
    )

    assert targets == ["8.8.8.8", "192.168.0.1", "10.0.0.1", "10.0.0.2", "10.0.0.3"]
    assert invalid == []


def test_safe_target_name_removes_unsafe_characters() -> None:
    assert safe_target_name("host/name:443") == "host_name_443"


def test_export_csv_writes_summary_and_samples(tmp_path) -> None:
    path = tmp_path / "result.csv"
    snapshot = MetricSnapshot(
        hop_index=1,
        address="192.168.0.1",
        hostname="router",
        samples=1,
        sent=1,
        received=1,
        timeout_count=0,
        current_latency_ms=1.0,
        avg_latency_ms=1.0,
        min_latency_ms=1.0,
        max_latency_ms=1.0,
        loss_percent=0.0,
        recent_loss_percent=0.0,
        jitter_ms=None,
        status=STATUS_OK,
    )
    observation = HopObservation(
        timestamp=datetime.now(),
        hop_index=1,
        address="192.168.0.1",
        hostname="router",
        success=True,
        latency_ms=1.0,
        status=STATUS_OK,
    )

    export_csv(path, [observation], [snapshot], ["정상"])
    text = path.read_text(encoding="utf-8-sig")
    assert "Summary" in text
    assert "192.168.0.1" in text
    assert "대상IP" in text
    assert "hostname" not in text.lower()


def test_export_csv_writes_evidence_annotations(tmp_path) -> None:
    path = tmp_path / "annotated.csv"
    now = datetime(2026, 1, 1, 12, 0, 0)
    annotation = ExportAnnotation(
        start=now,
        end=now + timedelta(seconds=60),
        source="alert",
        severity="critical",
        title="Loss alert",
        message="Packet loss 25.0% for 3m",
    )

    export_csv(path, [_sample_observation(timestamp=now)], [_sample_snapshot()], ["focus"], [annotation])

    text = path.read_text(encoding="utf-8-sig")
    assert "Annotations" in text
    assert "Loss alert" in text
    assert "Packet loss 25.0% for 3m" in text


def test_export_xlsx_writes_workbook(tmp_path) -> None:
    path = tmp_path / "result.xlsx"
    snapshot = _sample_snapshot()
    observation = _sample_observation()

    export_xlsx(path, "8.8.8.8", [observation], [snapshot], ["정상"])

    assert path.exists()
    assert path.stat().st_size > 0


def test_export_xlsx_contains_summary_metrics_and_samples(tmp_path) -> None:
    from openpyxl import load_workbook

    path = tmp_path / "result.xlsx"
    snapshot = _sample_snapshot()
    observation = _sample_observation()

    export_xlsx(path, "8.8.8.8", [observation], [snapshot], ["중간 Hop ICMP 응답 제한 가능성"])

    workbook = load_workbook(path)
    assert workbook.sheetnames == ["Summary", "Hop Metrics", "Samples"]
    assert workbook["Summary"]["B3"].value == "8.8.8.8"
    assert "ICMP 응답 제한" in workbook["Summary"]["A6"].value
    assert workbook["Hop Metrics"]["B1"].value == "대상IP"
    assert workbook["Hop Metrics"]["D2"].value == 1
    assert workbook["Samples"]["A2"].value is not None


def test_export_xlsx_writes_annotations_sheet_when_present(tmp_path) -> None:
    from openpyxl import load_workbook

    path = tmp_path / "annotated.xlsx"
    now = datetime(2026, 1, 1, 12, 0, 0)
    annotation = ExportAnnotation(
        start=now,
        end=now,
        source="route",
        severity="warning",
        title="Route changed",
        message="changed Hop 1",
    )

    export_xlsx(path, "8.8.8.8", [_sample_observation(timestamp=now)], [_sample_snapshot()], ["focus"], [annotation])

    workbook = load_workbook(path)
    assert "Annotations" in workbook.sheetnames
    assert workbook["Annotations"]["E2"].value == "Route changed"
    assert workbook["Annotations"]["F2"].value == "changed Hop 1"


def test_text_report_contains_analysis_and_hop_summary(tmp_path) -> None:
    path = tmp_path / "report.txt"

    write_text_report(path, "8.8.8.8", [_sample_snapshot()], ["대상 서버 구간 문제 가능성"])

    text = path.read_text(encoding="utf-8")
    assert "Network Path Diagnostics Report" in text
    assert "대상IP: 8.8.8.8" in text
    assert "대상 서버 구간 문제 가능성" in text
    assert "Hop 1" in text


def test_text_report_contains_evidence_annotations(tmp_path) -> None:
    path = tmp_path / "annotated_report.txt"
    now = datetime(2026, 1, 1, 12, 0, 0)
    annotation = ExportAnnotation(
        start=now,
        end=now + timedelta(seconds=30),
        source="manual",
        severity="",
        title="operator note",
        message="ISP handoff degraded",
    )

    write_text_report(path, "8.8.8.8", [_sample_snapshot()], ["focus"], [annotation])

    text = path.read_text(encoding="utf-8")
    assert "Evidence Annotations:" in text
    assert "operator note" in text
    assert "ISP handoff degraded" in text


def test_html_report_contains_printable_sections_and_escapes_values(tmp_path) -> None:
    path = tmp_path / "report.html"
    now = datetime(2026, 1, 1, 12, 0, 0)
    annotation = ExportAnnotation(
        start=now,
        end=now + timedelta(seconds=30),
        source="manual",
        severity="warning",
        title="operator <note>",
        message="ISP handoff degraded",
    )

    write_html_report(
        path,
        "8.8.8.8",
        [_sample_snapshot()],
        ["Target path needs review"],
        [annotation],
        (now, now + timedelta(minutes=10)),
    )

    html = path.read_text(encoding="utf-8")
    assert "<!doctype html>" in html
    assert "<strong>Target:</strong> 8.8.8.8" in html
    assert "2026-01-01T12:00:00 - 2026-01-01T12:10:00" in html
    assert "<h2>Analysis</h2>" in html
    assert "Target path needs review" in html
    assert "<h2>Hop Metrics</h2>" in html
    assert "operator &lt;note&gt;" in html
    assert "ISP handoff degraded" in html


def test_annotations_in_range_keeps_overlapping_events() -> None:
    now = datetime(2026, 1, 1, 12, 0, 0)
    annotations = [
        ExportAnnotation(now, now + timedelta(seconds=10), "alert", "warning", "before", "before"),
        ExportAnnotation(now + timedelta(seconds=20), now + timedelta(seconds=30), "alert", "warning", "inside", "inside"),
        ExportAnnotation(now + timedelta(seconds=50), now + timedelta(seconds=60), "alert", "warning", "after", "after"),
    ]

    selected = annotations_in_range(annotations, (now + timedelta(seconds=15), now + timedelta(seconds=45)))

    assert [annotation.title for annotation in selected] == ["inside"]


def test_alert_action_log_appends_comment_and_annotation_actions(tmp_path) -> None:
    path = tmp_path / "alerts.csv"
    now = datetime(2026, 1, 1, 12, 0, 0)
    event = AlertEvent(
        key="target_latency_100ms",
        timestamp=now,
        start=now,
        end=now,
        severity="warning",
        title="Latency alert",
        message="Target latency 125.0 ms >= 100 ms",
    )

    append_alert_action(path, event, actions=["timeline_annotation", "comment"])
    rows = read_alert_actions(path)

    assert rows[0]["source"] == "alert"
    assert rows[0]["title"] == "Latency alert"
    assert rows[0]["actions"] == "timeline_annotation;comment"


def test_session_log_round_trips_observations(tmp_path) -> None:
    path = tmp_path / "session.csv"
    writer = SessionLogWriter(path)
    writer.write_many([_sample_observation()])
    writer.close()

    observations = read_observations(path)

    assert len(observations) == 1
    assert observations[0].address == "192.168.0.1"
    assert observations[0].success is True


def test_session_log_rotates_and_reads_all_segments(tmp_path) -> None:
    path = tmp_path / "session.csv"
    writer = SessionLogWriter(path, max_rows_per_file=2)
    observations = [_sample_observation() for _ in range(5)]

    writer.write_many(observations)
    writer.close()

    assert len(writer.paths) == 3
    assert all(segment.exists() for segment in writer.paths)
    assert len(read_observations(path)) == 5


def test_session_log_recovers_valid_rows_after_malformed_tail(tmp_path) -> None:
    path = tmp_path / "session.csv"
    writer = SessionLogWriter(path)
    writer.write_many([_sample_observation()])
    writer.close()
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not-a-timestamp,broken,Target,not-a-hop,,,,\n")

    observations = read_observations(path)

    assert len(observations) == 1
    assert observations[0].address == "192.168.0.1"


def test_session_log_reads_observations_in_selected_range(tmp_path) -> None:
    now = datetime.now()
    path = tmp_path / "session.csv"
    writer = SessionLogWriter(path)
    writer.write_many([
        _sample_observation(timestamp=now),
        _sample_observation(timestamp=now + timedelta(seconds=10), address="192.168.0.2"),
        _sample_observation(timestamp=now + timedelta(seconds=20), address="192.168.0.3"),
    ])
    writer.close()

    observations = list(iter_observations_in_range(path, now + timedelta(seconds=5), now + timedelta(seconds=15)))

    assert [observation.address for observation in observations] == ["192.168.0.2"]


def test_session_log_segment_index_reports_bounds_and_skips_ranges(tmp_path) -> None:
    now = datetime(2026, 1, 1, 12, 0, 0)
    path = tmp_path / "session.csv"
    writer = SessionLogWriter(path, max_rows_per_file=2)
    writer.write_many([
        _sample_observation(timestamp=now, address="192.168.0.1"),
        _sample_observation(timestamp=now + timedelta(seconds=1), address="192.168.0.2"),
        _sample_observation(timestamp=now + timedelta(hours=1), address="192.168.0.3"),
    ])
    writer.close()

    segments = session_log_segment_index(path)
    bounds = session_log_bounds(path)
    observations = list(iter_observations_in_range(path, now + timedelta(minutes=59), now + timedelta(minutes=61)))

    assert len(segments) == 2
    assert [segment.rows for segment in segments] == [2, 1]
    assert bounds == (now, now + timedelta(hours=1))
    assert [observation.address for observation in observations] == ["192.168.0.3"]


def test_session_log_segment_index_uses_persisted_metadata(tmp_path, monkeypatch) -> None:
    now = datetime(2026, 1, 1, 12, 0, 0)
    path = tmp_path / "session.csv"
    writer = SessionLogWriter(path, max_rows_per_file=2)
    writer.write_many([
        _sample_observation(timestamp=now, address="192.168.0.1"),
        _sample_observation(timestamp=now + timedelta(seconds=1), address="192.168.0.2"),
        _sample_observation(timestamp=now + timedelta(hours=1), address="192.168.0.3"),
    ])
    writer.close()

    index_path = session_log_segment_index_path(path)
    assert index_path.exists()

    def fail_scan(_path):
        raise AssertionError("CSV segment scan should not run when metadata is current")

    monkeypatch.setattr(session_log_module, "_index_segment", fail_scan)

    segments = session_log_segment_index(path)

    assert [segment.rows for segment in segments] == [2, 1]
    assert [(segment.start, segment.end) for segment in segments] == [
        (now, now + timedelta(seconds=1)),
        (now + timedelta(hours=1), now + timedelta(hours=1)),
    ]


def test_session_log_segment_index_falls_back_when_metadata_is_stale(tmp_path) -> None:
    now = datetime(2026, 1, 1, 12, 0, 0)
    path = tmp_path / "session.csv"
    writer = SessionLogWriter(path, max_rows_per_file=2)
    writer.write_many([
        _sample_observation(timestamp=now, address="192.168.0.1"),
        _sample_observation(timestamp=now + timedelta(seconds=1), address="192.168.0.2"),
        _sample_observation(timestamp=now + timedelta(hours=1), address="192.168.0.3"),
    ])
    writer.close()
    session_log_segment_index_path(path).write_text(
        '{"version": 1, "base": "session.csv", "segments": []}',
        encoding="utf-8",
    )

    segments = session_log_segment_index(path)

    assert [segment.rows for segment in segments] == [2, 1]
    assert segments[-1].end == now + timedelta(hours=1)


def test_session_log_create_uses_target_month_flex_directory(tmp_path) -> None:
    writer = SessionLogWriter.create("198.51.100.10", root=tmp_path)
    try:
        expected_parent = session_log_directory("198.51.100.10", root=tmp_path)

        assert writer.path.parent == expected_parent
        assert writer.path.name.startswith("network_trace_198.51.100.10_")
        assert writer.path.name.endswith(".samples.csv")
    finally:
        writer.close()


def test_session_index_registers_updates_and_filters_sessions(tmp_path) -> None:
    store = SessionIndexStore.create(tmp_path)
    now = datetime(2026, 1, 1, 12, 0, 0)
    sample_path = tmp_path / "198.51.100.10" / "2026-01" / "session.samples.csv"
    route_path = sample_path.with_name("session.routes.csv")

    record = store.register_session(
        target="198.51.100.10",
        sample_path=sample_path,
        route_path=route_path,
        started_at=now,
        interval_seconds=1,
        measurement_mode="full_route",
        target_count=3,
    )
    store.add_samples(record.session_id, 5, now + timedelta(seconds=5), segments=[sample_path])
    store.finish_session(record.session_id, state=SESSION_STATE_ARCHIVED, ended_at=now + timedelta(seconds=10))

    sessions = store.list_sessions(target="198.51.100.10")
    active_sessions = store.list_sessions(state=SESSION_STATE_PAUSED)

    assert len(sessions) == 1
    assert sessions[0].samples == 5
    assert sessions[0].state == SESSION_STATE_ARCHIVED
    assert sessions[0].route_path == route_path
    assert sessions[0].target_count == 3
    assert active_sessions == []


def test_session_index_recovers_stale_active_sessions_as_paused(tmp_path) -> None:
    store = SessionIndexStore.create(tmp_path)
    now = datetime(2026, 1, 1, 12, 0, 0)
    stale_path = tmp_path / "198.51.100.10" / "2026-01" / "stale.samples.csv"
    recent_path = tmp_path / "203.0.113.20" / "2026-01" / "recent.samples.csv"
    archived_path = tmp_path / "203.0.113.30" / "2026-01" / "archived.samples.csv"
    stale = store.register_session(
        target="198.51.100.10",
        sample_path=stale_path,
        route_path=stale_path.with_name("stale.routes.csv"),
        started_at=now - timedelta(hours=3),
        interval_seconds=5,
        measurement_mode="full_route",
        target_count=1,
    )
    store.add_samples(stale.session_id, 1, now - timedelta(hours=2), segments=[stale_path])
    recent = store.register_session(
        target="203.0.113.20",
        sample_path=recent_path,
        route_path=recent_path.with_name("recent.routes.csv"),
        started_at=now - timedelta(minutes=15),
        interval_seconds=5,
        measurement_mode="full_route",
        target_count=1,
    )
    archived = store.register_session(
        target="203.0.113.30",
        sample_path=archived_path,
        route_path=archived_path.with_name("archived.routes.csv"),
        started_at=now - timedelta(hours=4),
        interval_seconds=5,
        measurement_mode="full_route",
        target_count=1,
    )
    store.finish_session(archived.session_id, state=SESSION_STATE_ARCHIVED, ended_at=now - timedelta(hours=3))

    recovered = store.recover_stale_active_sessions(stale_after=timedelta(hours=1), now=now)

    assert [record.session_id for record in recovered] == [stale.session_id]
    stale_record = store.find_session(stale.session_id)
    recent_record = store.find_session(recent.session_id)
    archived_record = store.find_session(archived.session_id)
    assert stale_record is not None
    assert stale_record.state == SESSION_STATE_PAUSED
    assert stale_record.end == now - timedelta(hours=2)
    assert stale_record.last_error == "Recovered stale active session after restart"
    assert recent_record is not None
    assert recent_record.state == SESSION_STATE_ACTIVE
    assert archived_record is not None
    assert archived_record.state == SESSION_STATE_ARCHIVED


def test_session_index_reconciles_missing_session_files_as_will_delete(tmp_path) -> None:
    store = SessionIndexStore.create(tmp_path)
    now = datetime(2026, 1, 1, 12, 0, 0)
    existing_path = tmp_path / "198.51.100.10" / "2026-01" / "existing.samples.csv"
    missing_path = tmp_path / "203.0.113.20" / "2026-01" / "missing.samples.csv"
    existing_path.parent.mkdir(parents=True)
    existing_path.write_text("header\n", encoding="utf-8")
    existing = store.register_session(
        target="198.51.100.10",
        sample_path=existing_path,
        route_path=existing_path.with_name("existing.routes.csv"),
        started_at=now,
        interval_seconds=5,
        measurement_mode="full_route",
        target_count=1,
    )
    missing = store.register_session(
        target="203.0.113.20",
        sample_path=missing_path,
        route_path=missing_path.with_name("missing.routes.csv"),
        started_at=now + timedelta(minutes=1),
        interval_seconds=5,
        measurement_mode="full_route",
        target_count=1,
    )

    marked = store.reconcile_missing_session_files()

    assert [record.session_id for record in marked] == [missing.session_id]
    existing_record = store.find_session(existing.session_id)
    missing_record = store.find_session(missing.session_id)
    assert existing_record is not None
    assert existing_record.state == SESSION_STATE_ACTIVE
    assert missing_record is not None
    assert missing_record.state == SESSION_STATE_WILL_DELETE
    assert missing_record.last_error == f"Session log missing: {missing_path}"


def test_session_index_recovers_sessions_from_existing_logs_when_index_missing(tmp_path) -> None:
    store = SessionIndexStore.create(tmp_path)
    now = datetime(2026, 1, 1, 12, 0, 0)
    sample_path = tmp_path / "198.51.100.10" / "2026-01" / "session.samples.csv"
    with SessionLogWriter(sample_path, max_rows_per_file=2) as writer:
        writer.write_many(
            [
                HopObservation(now, 0, "198.51.100.10", "Target", True, 10.0, STATUS_OK, True),
                HopObservation(now + timedelta(seconds=1), 0, "203.0.113.20", "Target", True, 20.0, STATUS_OK, True),
                HopObservation(now + timedelta(seconds=2), 1, "192.0.2.1", "gateway", True, 2.0, STATUS_OK),
            ]
        )

    sessions = store.list_sessions()

    assert len(sessions) == 1
    record = sessions[0]
    assert record.target == "198.51.100.10"
    assert record.sample_path == sample_path
    assert record.start == now
    assert record.end == now + timedelta(seconds=2)
    assert record.samples == 3
    assert record.state == SESSION_STATE_ARCHIVED
    assert record.target_count == 2
    assert len(record.segments) == 2
    assert record.last_error == "Recovered from session log scan"
    assert store.path.exists()
    assert store.find_session(record.session_id) is not None


def test_session_index_recovers_sessions_from_logs_when_index_is_corrupt(tmp_path) -> None:
    store = SessionIndexStore.create(tmp_path)
    now = datetime(2026, 1, 1, 12, 0, 0)
    sample_path = tmp_path / "198.51.100.10" / "2026-01" / "session.samples.csv"
    with SessionLogWriter(sample_path) as writer:
        writer.write_many([HopObservation(now, 0, "198.51.100.10", "Target", True, 10.0, STATUS_OK, True)])
    store.path.write_text("{bad json", encoding="utf-8")

    sessions = store.list_sessions()

    assert len(sessions) == 1
    assert sessions[0].target == "198.51.100.10"
    assert sessions[0].samples == 1
    assert store.find_session(sessions[0].session_id) is not None


def test_session_index_merges_missing_log_sessions_into_valid_index(tmp_path) -> None:
    store = SessionIndexStore.create(tmp_path)
    now = datetime(2026, 1, 1, 12, 0, 0)
    indexed_path = tmp_path / "198.51.100.10" / "2026-01" / "indexed.samples.csv"
    orphan_path = tmp_path / "203.0.113.20" / "2026-01" / "orphan.samples.csv"
    indexed = store.register_session(
        target="198.51.100.10",
        sample_path=indexed_path,
        route_path=indexed_path.with_name("indexed.routes.csv"),
        started_at=now,
        interval_seconds=5,
        measurement_mode="full_route",
        target_count=1,
    )
    store.finish_session(indexed.session_id, state=SESSION_STATE_ARCHIVED, ended_at=now)
    with SessionLogWriter(orphan_path) as writer:
        writer.write_many(
            [
                HopObservation(now + timedelta(minutes=1), 0, "203.0.113.20", "Target", True, 12.0, STATUS_OK, True),
                HopObservation(now + timedelta(minutes=2), 0, "203.0.113.21", "Target", True, 15.0, STATUS_OK, True),
            ]
        )

    sessions = store.list_sessions(recover_missing=True)

    assert {session.target for session in sessions} == {"198.51.100.10", "203.0.113.20"}
    indexed_session = store.find_session(indexed.session_id)
    orphan_session = store.find_session(orphan_path.stem)
    assert indexed_session is not None
    assert indexed_session.interval_seconds == 5
    assert orphan_session is not None
    assert orphan_session.samples == 2
    assert orphan_session.target_count == 2
    assert orphan_session.last_error == "Recovered from session log scan"


def test_session_index_retries_transient_replace_permission_error(tmp_path, monkeypatch) -> None:
    store = SessionIndexStore.create(tmp_path)
    now = datetime(2026, 1, 1, 12, 0, 0)
    sample_path = tmp_path / "198.51.100.10" / "2026-01" / "session.samples.csv"
    attempts = 0
    original_replace = session_index_module._replace_path

    def flaky_replace(source, target):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("temporarily locked")
        return original_replace(source, target)

    monkeypatch.setattr(session_index_module, "SESSION_INDEX_IO_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(session_index_module, "_replace_path", flaky_replace)

    record = store.register_session(
        target="198.51.100.10",
        sample_path=sample_path,
        route_path=sample_path.with_name("session.routes.csv"),
        started_at=now,
        interval_seconds=1,
        measurement_mode="full_route",
        target_count=1,
    )

    assert attempts == 3
    assert store.find_session(record.session_id) is not None


def test_session_index_retries_transient_read_permission_error(tmp_path, monkeypatch) -> None:
    store = SessionIndexStore.create(tmp_path)
    now = datetime(2026, 1, 1, 12, 0, 0)
    sample_path = tmp_path / "198.51.100.10" / "2026-01" / "session.samples.csv"
    record = store.register_session(
        target="198.51.100.10",
        sample_path=sample_path,
        route_path=sample_path.with_name("session.routes.csv"),
        started_at=now,
        interval_seconds=1,
        measurement_mode="full_route",
        target_count=1,
    )
    attempts = 0
    original_read = session_index_module._read_text_path

    def flaky_read(path):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("temporarily locked")
        return original_read(path)

    monkeypatch.setattr(session_index_module, "SESSION_INDEX_IO_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(session_index_module, "_read_text_path", flaky_read)

    assert store.find_session(record.session_id) is not None
    assert attempts == 3


def test_session_index_cleans_temp_file_after_persistent_replace_error(tmp_path, monkeypatch) -> None:
    store = SessionIndexStore.create(tmp_path)
    now = datetime(2026, 1, 1, 12, 0, 0)
    sample_path = tmp_path / "198.51.100.10" / "2026-01" / "session.samples.csv"

    def locked_replace(source, target):
        raise PermissionError("locked")

    monkeypatch.setattr(session_index_module, "SESSION_INDEX_IO_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(session_index_module, "_replace_path", locked_replace)

    try:
        store.register_session(
            target="198.51.100.10",
            sample_path=sample_path,
            route_path=sample_path.with_name("session.routes.csv"),
            started_at=now,
            interval_seconds=1,
            measurement_mode="full_route",
            target_count=1,
        )
    except PermissionError:
        pass
    else:
        raise AssertionError("expected PermissionError")

    temp_files = [path for path in tmp_path.iterdir() if path.name.startswith("tmp")]
    assert temp_files == []


def test_session_index_delete_removes_record_and_managed_files(tmp_path) -> None:
    store = SessionIndexStore.create(tmp_path)
    now = datetime(2026, 1, 1, 12, 0, 0)
    sample_path = tmp_path / "198.51.100.10" / "2026-01" / "session.samples.csv"
    part_path = sample_path.with_name("session.samples.part001.csv")
    route_path = sample_path.with_name("session.routes.csv")
    alert_path = alert_action_log_path_for_session(sample_path)
    segment_index_path = session_log_segment_index_path(sample_path)
    sample_path.parent.mkdir(parents=True)
    for path in (sample_path, part_path, route_path, alert_path, segment_index_path):
        assert path is not None
        path.write_text("managed\n", encoding="utf-8")

    record = store.register_session(
        target="198.51.100.10",
        sample_path=sample_path,
        route_path=route_path,
        started_at=now,
        interval_seconds=1,
        measurement_mode="full_route",
        target_count=1,
    )
    store.add_samples(record.session_id, 2, now + timedelta(seconds=1), segments=[sample_path, part_path])

    deleted = store.delete_session(record.session_id)

    assert deleted is not None
    assert deleted.session_id == record.session_id
    assert store.list_sessions() == []
    assert not sample_path.exists()
    assert not part_path.exists()
    assert not route_path.exists()
    assert alert_path is not None and not alert_path.exists()
    assert not segment_index_path.exists()


def test_session_index_prunes_old_inactive_sessions_and_keeps_active(tmp_path) -> None:
    store = SessionIndexStore.create(tmp_path)
    now = datetime(2026, 1, 31, 12, 0, 0)
    old_path = tmp_path / "198.51.100.10" / "2026-01" / "old.samples.csv"
    active_path = tmp_path / "198.51.100.20" / "2026-01" / "active.samples.csv"
    recent_path = tmp_path / "198.51.100.30" / "2026-01" / "recent.samples.csv"
    for path in (old_path, active_path, recent_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("managed\n", encoding="utf-8")

    old = store.register_session(
        target="198.51.100.10",
        sample_path=old_path,
        route_path=old_path.with_name("old.routes.csv"),
        started_at=now - timedelta(days=40),
        interval_seconds=1,
        measurement_mode="full_route",
        target_count=1,
    )
    store.finish_session(old.session_id, state=SESSION_STATE_ARCHIVED, ended_at=now - timedelta(days=39))
    active = store.register_session(
        target="198.51.100.20",
        sample_path=active_path,
        route_path=active_path.with_name("active.routes.csv"),
        started_at=now - timedelta(days=40),
        interval_seconds=1,
        measurement_mode="full_route",
        target_count=1,
    )
    recent = store.register_session(
        target="198.51.100.30",
        sample_path=recent_path,
        route_path=recent_path.with_name("recent.routes.csv"),
        started_at=now - timedelta(days=2),
        interval_seconds=1,
        measurement_mode="full_route",
        target_count=1,
    )
    store.finish_session(recent.session_id, state=SESSION_STATE_ARCHIVED, ended_at=now - timedelta(days=1))

    pruned = store.prune_sessions_older_than(older_than=timedelta(days=30), now=now)

    assert [record.session_id for record in pruned] == [old.session_id]
    assert store.find_session(old.session_id) is None
    assert store.find_session(active.session_id) is not None
    assert store.find_session(recent.session_id) is not None
    assert not old_path.exists()
    assert active_path.exists()
    assert recent_path.exists()


def test_export_worker_writes_csv_from_session_log(tmp_path) -> None:
    session_path = tmp_path / "session.csv"
    writer = SessionLogWriter(session_path)
    writer.write_many([_sample_observation()])
    writer.close()

    export_path = tmp_path / "worker_export.csv"
    completed: list[str] = []
    errors: list[str] = []
    worker = ExportWorker(
        kind="csv",
        path=export_path,
        target="8.8.8.8",
        session_log_path=session_path,
        snapshots=[_sample_snapshot(address="203.0.113.5")],
        analysis=["정상"],
    )
    worker.export_completed.connect(completed.append)
    worker.error_message.connect(errors.append)

    worker.run()

    assert errors == []
    assert completed == [str(export_path)]
    assert "192.168.0.1" in export_path.read_text(encoding="utf-8-sig")


def test_export_worker_includes_focus_annotations(tmp_path) -> None:
    now = datetime(2026, 1, 1, 12, 0, 0)
    session_path = tmp_path / "session.csv"
    writer = SessionLogWriter(session_path)
    writer.write_many([_sample_observation(timestamp=now)])
    writer.close()

    export_path = tmp_path / "worker_annotations.csv"
    errors: list[str] = []
    worker = ExportWorker(
        kind="csv",
        path=export_path,
        target="8.8.8.8",
        session_log_path=session_path,
        snapshots=[_sample_snapshot(address="203.0.113.5")],
        analysis=["focus"],
        annotations=[
            ExportAnnotation(now, now + timedelta(seconds=1), "alert", "critical", "Loss alert", "loss evidence")
        ],
        focus_range=(now, now + timedelta(seconds=10)),
    )
    worker.error_message.connect(errors.append)

    worker.run()

    text = export_path.read_text(encoding="utf-8-sig")
    assert errors == []
    assert "Loss alert" in text
    assert "loss evidence" in text


def test_export_worker_filters_session_log_by_focus_range(tmp_path) -> None:
    now = datetime.now()
    session_path = tmp_path / "session.csv"
    writer = SessionLogWriter(session_path)
    writer.write_many([
        _sample_observation(timestamp=now, address="192.168.0.1"),
        _sample_observation(timestamp=now + timedelta(seconds=10), address="192.168.0.2"),
    ])
    writer.close()

    export_path = tmp_path / "worker_focus_export.csv"
    errors: list[str] = []
    worker = ExportWorker(
        kind="csv",
        path=export_path,
        target="8.8.8.8",
        session_log_path=session_path,
        snapshots=[_sample_snapshot(address="203.0.113.5")],
        analysis=["focus"],
        focus_range=(now + timedelta(seconds=5), now + timedelta(seconds=15)),
    )
    worker.error_message.connect(errors.append)

    worker.run()

    text = export_path.read_text(encoding="utf-8-sig")
    assert errors == []
    assert "192.168.0.2" in text
    assert "192.168.0.1" not in text


def test_export_worker_writes_html_report_with_focus_range(tmp_path) -> None:
    now = datetime(2026, 1, 1, 12, 0, 0)
    export_path = tmp_path / "worker_report.html"
    completed: list[str] = []
    errors: list[str] = []
    worker = ExportWorker(
        kind="html",
        path=export_path,
        target="8.8.8.8",
        session_log_path=None,
        snapshots=[_sample_snapshot(address="203.0.113.5")],
        analysis=["Target path needs review"],
        annotations=[
            ExportAnnotation(now, now + timedelta(seconds=1), "alert", "critical", "Loss alert", "loss evidence")
        ],
        focus_range=(now, now + timedelta(minutes=10)),
    )
    worker.export_completed.connect(completed.append)
    worker.error_message.connect(errors.append)

    worker.run()

    html = export_path.read_text(encoding="utf-8")
    assert errors == []
    assert completed == [str(export_path)]
    assert "Target path needs review" in html
    assert "203.0.113.5" in html
    assert "Loss alert" in html
    assert "2026-01-01T12:00:00 - 2026-01-01T12:10:00" in html


def test_grouped_statistics_aggregates_by_period_and_hop() -> None:
    now = datetime(2026, 1, 1, 12, 3, 0)
    observations = [
        HopObservation(now, 0, "198.51.100.10", "Target", True, 10.0, STATUS_OK, True),
        HopObservation(now + timedelta(seconds=30), 0, "198.51.100.10", "Target", False, None, STATUS_TIMEOUT, True),
        HopObservation(now + timedelta(minutes=3), 0, "198.51.100.10", "Target", True, 20.0, STATUS_OK, True),
        HopObservation(now + timedelta(seconds=40), 1, "192.0.2.1", "router", True, 2.0, STATUS_OK),
    ]

    rows = grouped_statistics(observations, StatisticsExportOptions(grouping_seconds=300))
    target_first = next(
        row for row in rows if row.kind == "Target" and row.period_start == datetime(2026, 1, 1, 12, 0, 0)
    )
    target_second = next(
        row for row in rows if row.kind == "Target" and row.period_start == datetime(2026, 1, 1, 12, 5, 0)
    )
    hop_first = next(row for row in rows if row.kind == "Hop")

    assert target_first.sent == 2
    assert target_first.received == 1
    assert target_first.failed == 1
    assert target_first.loss_percent == 50.0
    assert target_first.avg_latency_ms == 10.0
    assert target_first.status_counts == "OK:1;TIMEOUT:1"
    assert target_second.sent == 1
    assert target_second.avg_latency_ms == 20.0
    assert hop_first.address == "192.0.2.1"


def test_grouped_statistics_can_export_utc_periods() -> None:
    timestamp = datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone(timedelta(hours=9)))
    observations = [
        HopObservation(timestamp, 0, "198.51.100.10", "Target", True, 10.0, STATUS_OK, True),
    ]

    rows = grouped_statistics(observations, StatisticsExportOptions(grouping_seconds=60, timezone_mode=TIMEZONE_UTC))

    assert rows[0].period_start == datetime(2026, 1, 1, 0, 0, 0)
    assert rows[0].timezone_mode == TIMEZONE_UTC


def test_export_worker_writes_grouped_statistics_csv_from_focus_range(tmp_path) -> None:
    now = datetime(2026, 1, 1, 12, 0, 0)
    session_path = tmp_path / "session.csv"
    writer = SessionLogWriter(session_path)
    writer.write_many([
        _sample_observation(timestamp=now, address="192.168.0.1"),
        _sample_observation(timestamp=now + timedelta(seconds=10), address="192.168.0.2"),
    ])
    writer.close()

    export_path = tmp_path / "worker_statistics.csv"
    errors: list[str] = []
    worker = ExportWorker(
        kind="stats_csv",
        path=export_path,
        target="8.8.8.8",
        session_log_path=session_path,
        snapshots=[],
        analysis=[],
        focus_range=(now + timedelta(seconds=5), now + timedelta(seconds=15)),
        statistics_options=StatisticsExportOptions(grouping_seconds=300),
    )
    worker.error_message.connect(errors.append)

    worker.run()

    text = export_path.read_text(encoding="utf-8-sig")
    assert errors == []
    assert "period_start" in text
    assert "192.168.0.2" in text
    assert "192.168.0.1" not in text


def test_export_worker_rejects_empty_statistics_csv_range(tmp_path) -> None:
    now = datetime(2026, 1, 1, 12, 0, 0)
    session_path = tmp_path / "session.csv"
    writer = SessionLogWriter(session_path)
    writer.write_many([_sample_observation(timestamp=now, address="192.168.0.1")])
    writer.close()

    export_path = tmp_path / "empty_statistics.csv"
    completed: list[str] = []
    errors: list[str] = []
    worker = ExportWorker(
        kind="stats_csv",
        path=export_path,
        target="8.8.8.8",
        session_log_path=session_path,
        snapshots=[],
        analysis=[],
        focus_range=(now + timedelta(minutes=5), now + timedelta(minutes=10)),
        statistics_options=StatisticsExportOptions(grouping_seconds=300),
    )
    worker.export_completed.connect(completed.append)
    worker.error_message.connect(errors.append)

    worker.run()

    assert completed == []
    assert errors == ["No statistics samples matched the selected export range."]
    assert not export_path.exists()


def test_export_worker_rejects_empty_statistics_xlsx_override(tmp_path) -> None:
    export_path = tmp_path / "empty_statistics.xlsx"
    completed: list[str] = []
    errors: list[str] = []
    worker = ExportWorker(
        kind="stats_xlsx",
        path=export_path,
        target="8.8.8.8",
        session_log_path=None,
        snapshots=[],
        analysis=[],
        observations_override=[],
        statistics_options=StatisticsExportOptions(grouping_seconds=300),
    )
    worker.export_completed.connect(completed.append)
    worker.error_message.connect(errors.append)

    worker.run()

    assert completed == []
    assert errors == ["No statistics samples matched the selected export range."]
    assert not export_path.exists()


def test_export_statistics_xlsx_writes_statistics_sheet(tmp_path) -> None:
    path = tmp_path / "statistics.xlsx"
    export_statistics_xlsx(
        path,
        "198.51.100.10",
        [_sample_observation(address="198.51.100.10")],
        StatisticsExportOptions(grouping_seconds=300),
    )

    from openpyxl import load_workbook

    workbook = load_workbook(path)

    assert "Statistics" in workbook.sheetnames
    assert workbook["Summary"]["B3"].value == "198.51.100.10"
    assert workbook["Statistics"]["A1"].value == "period_start"


def _sample_snapshot(address: str = "192.168.0.1") -> MetricSnapshot:
    return MetricSnapshot(
        hop_index=1,
        address=address,
        hostname="router",
        samples=1,
        sent=1,
        received=1,
        timeout_count=0,
        current_latency_ms=1.0,
        avg_latency_ms=1.0,
        min_latency_ms=1.0,
        max_latency_ms=1.0,
        loss_percent=0.0,
        recent_loss_percent=0.0,
        jitter_ms=None,
        status=STATUS_OK,
    )


def _sample_observation(
    *,
    timestamp: datetime | None = None,
    address: str = "192.168.0.1",
) -> HopObservation:
    return HopObservation(
        timestamp=timestamp or datetime.now(),
        hop_index=1,
        address=address,
        hostname="router",
        success=True,
        latency_ms=1.0,
        status=STATUS_OK,
    )
