from __future__ import annotations

from datetime import datetime, timedelta

from app.core.analyzer import analyze_path
from app.core.metrics import HopMetricTracker
from app.core.models import STATUS_OK, STATUS_TIMEOUT, HopInfo, HopObservation, MetricSnapshot, PingResult
from app.core.observation_stats import build_focus_snapshots, observations_in_range


def test_metric_tracker_calculates_loss_and_latency() -> None:
    tracker = HopMetricTracker(HopInfo(index=1, address="192.168.0.1"))
    tracker.add_result(PingResult("192.168.0.1", True, 2.0, STATUS_OK, datetime.now()))
    tracker.add_result(PingResult("192.168.0.1", False, None, STATUS_TIMEOUT, datetime.now()))
    tracker.add_result(PingResult("192.168.0.1", True, 4.0, STATUS_OK, datetime.now()))

    snapshot = tracker.snapshot()
    assert snapshot.sent == 3
    assert snapshot.received == 2
    assert round(snapshot.loss_percent, 1) == 33.3
    assert snapshot.avg_latency_ms == 3.0
    assert snapshot.max_latency_ms == 4.0


def test_focus_snapshot_builder_recalculates_selected_range() -> None:
    now = datetime.now()
    observations = [
        HopObservation(now, 1, "192.0.2.1", "gw", True, 1.0, STATUS_OK),
        HopObservation(now + timedelta(seconds=1), 1, "192.0.2.1", "gw", False, None, STATUS_TIMEOUT),
        HopObservation(now + timedelta(seconds=2), 0, "198.51.100.10", "Target", True, 10.0, STATUS_OK, True),
        HopObservation(now + timedelta(seconds=3), 0, "198.51.100.10", "Target", False, None, STATUS_TIMEOUT, True),
        HopObservation(now + timedelta(seconds=10), 1, "192.0.2.1", "gw", True, 2.0, STATUS_OK),
    ]

    selected = observations_in_range(observations, now, now + timedelta(seconds=3))
    focus = build_focus_snapshots(selected, current_target="198.51.100.10")

    assert len(selected) == 4
    assert len(focus.hop_snapshots) == 1
    assert focus.hop_snapshots[0].sent == 2
    assert focus.hop_snapshots[0].received == 1
    assert focus.hop_snapshots[0].loss_percent == 50.0
    assert focus.target_snapshot is not None
    assert focus.target_snapshot.sent == 2
    assert focus.target_snapshot.loss_percent == 50.0


def test_analyzer_flags_isolated_middle_hop_loss_as_icmp_limit() -> None:
    snapshots = [
        _snapshot(1, loss=0, status=STATUS_OK),
        _snapshot(2, loss=60, status=STATUS_TIMEOUT),
        _snapshot(3, loss=0, status=STATUS_OK),
    ]
    target = _snapshot(0, loss=0, status=STATUS_OK)
    analysis = analyze_path(snapshots, target)
    assert any("ICMP 응답 제한" in line for line in analysis)
    assert any(line.startswith("ANALYSIS_MIDDLE_HOP_ICMP_RATE_LIMIT:") for line in analysis)
    assert any(line.startswith("CAUSE_INTERMEDIATE_HOP_ICMP_RATE_LIMIT:") for line in analysis)


def test_analyzer_flags_first_hop_loss() -> None:
    snapshots = [
        _snapshot(1, loss=50, status=STATUS_TIMEOUT),
        _snapshot(2, loss=50, status=STATUS_TIMEOUT),
        _snapshot(3, loss=50, status=STATUS_TIMEOUT),
    ]
    target = _snapshot(0, loss=50, status=STATUS_TIMEOUT)
    analysis = analyze_path(snapshots, target)
    assert any("단말, 무선, AP, 게이트웨이" in line for line in analysis)
    assert any(line.startswith("ANALYSIS_FIRST_HOP_LAN_WIFI:") for line in analysis)
    assert any(line.startswith("CAUSE_LOCAL_LAN_WIFI:") for line in analysis)


def test_analyzer_flags_segment_loss_after_specific_hop() -> None:
    snapshots = [
        _snapshot(1, loss=0, status=STATUS_OK),
        _snapshot(2, loss=40, status=STATUS_TIMEOUT),
        _snapshot(3, loss=45, status=STATUS_TIMEOUT),
        _snapshot(4, loss=50, status=STATUS_TIMEOUT),
    ]
    target = _snapshot(0, loss=50, status=STATUS_TIMEOUT)

    analysis = analyze_path(snapshots, target)

    assert any("Hop 2 이후" in line and "해당 구간 이후 장애 가능성" in line for line in analysis)
    assert any(line.startswith("ANALYSIS_SEGMENT_LOSS_AFTER_HOP:") and "Hop 2" in line for line in analysis)
    assert any(line.startswith("CAUSE_ISP_OR_UPSTREAM_SEGMENT:") and "Hop 2" in line for line in analysis)


def test_analyzer_flags_target_only_loss() -> None:
    snapshots = [
        _snapshot(1, loss=0, status=STATUS_OK),
        _snapshot(2, loss=0, status=STATUS_OK),
        _snapshot(3, loss=0, status=STATUS_OK),
    ]
    target = _snapshot(0, loss=30, status=STATUS_TIMEOUT)

    analysis = analyze_path(snapshots, target)

    assert any("대상 서버, 방화벽, 서비스 구간 문제 가능성" in line for line in analysis)
    assert any(line.startswith("ANALYSIS_TARGET_ONLY_LOSS_OR_FILTER:") for line in analysis)
    assert any(line.startswith("CAUSE_FIREWALL_OR_TARGET_FILTER:") for line in analysis)


def test_analyzer_flags_latency_jump_after_hop() -> None:
    snapshots = [
        _snapshot(1, loss=0, status=STATUS_OK, avg_latency=5.0),
        _snapshot(2, loss=0, status=STATUS_OK, avg_latency=10.0),
        _snapshot(3, loss=0, status=STATUS_OK, avg_latency=75.0),
    ]
    target = _snapshot(0, loss=0, status=STATUS_OK, avg_latency=80.0)

    analysis = analyze_path(snapshots, target)

    assert any("Hop 3 이후 평균 지연시간" in line for line in analysis)
    assert any(line.startswith("ANALYSIS_BANDWIDTH_SATURATION_OR_CONGESTION:") for line in analysis)
    assert any(line.startswith("CAUSE_BANDWIDTH_SATURATION:") for line in analysis)


def test_analyzer_treats_middle_hop_only_latency_as_icmp_deprioritization() -> None:
    snapshots = [
        _snapshot(1, loss=0, status=STATUS_OK, avg_latency=5.0),
        _snapshot(2, loss=0, status=STATUS_OK, avg_latency=90.0),
        _snapshot(3, loss=0, status=STATUS_OK, avg_latency=7.0),
    ]
    target = _snapshot(0, loss=0, status=STATUS_OK, avg_latency=8.0)

    analysis = analyze_path(snapshots, target)

    assert any(line.startswith("ANALYSIS_MIDDLE_HOP_LATENCY_DEPRIORITIZED:") for line in analysis)
    assert any(line.startswith("CAUSE_INTERMEDIATE_HOP_ICMP_DEPRIORITIZATION:") for line in analysis)
    assert not any(line.startswith("ANALYSIS_BANDWIDTH_SATURATION_OR_CONGESTION:") for line in analysis)
    assert not any(line.startswith("CAUSE_BANDWIDTH_SATURATION:") for line in analysis)


def test_analyzer_flags_jitter_with_stable_code() -> None:
    snapshots = [
        _snapshot(1, loss=0, status=STATUS_OK, jitter=5.0),
        _snapshot(2, loss=0, status=STATUS_OK, jitter=35.0),
        _snapshot(3, loss=0, status=STATUS_OK, jitter=0.0),
    ]
    target = _snapshot(0, loss=0, status=STATUS_OK)

    analysis = analyze_path(snapshots, target)

    assert any("지연 편차" in line for line in analysis)
    assert any(line.startswith("ANALYSIS_JITTER_OR_WIRELESS_CONGESTION:") for line in analysis)
    assert any(line.startswith("CAUSE_JITTER_OR_LOCAL_CONGESTION:") for line in analysis)


def test_analyzer_reports_no_clear_issue_with_stable_code() -> None:
    snapshots = [
        _snapshot(1, loss=0, status=STATUS_OK, avg_latency=5.0),
        _snapshot(2, loss=0, status=STATUS_OK, avg_latency=7.0),
        _snapshot(3, loss=0, status=STATUS_OK, avg_latency=9.0),
    ]
    target = _snapshot(0, loss=0, status=STATUS_OK)

    analysis = analyze_path(snapshots, target)

    assert any(line.startswith("ANALYSIS_NO_CLEAR_PATH_ISSUE:") for line in analysis)


def _snapshot(
    index: int,
    loss: float,
    status: str,
    avg_latency: float = 1.0,
    jitter: float | None = None,
) -> MetricSnapshot:
    received = 0 if loss >= 100 else 3
    latency = avg_latency if received else None
    return MetricSnapshot(
        hop_index=index,
        address=f"192.0.2.{index}",
        hostname=None,
        samples=3,
        sent=3,
        received=received,
        timeout_count=3 - received,
        current_latency_ms=latency,
        avg_latency_ms=latency,
        min_latency_ms=latency,
        max_latency_ms=latency,
        loss_percent=loss,
        recent_loss_percent=loss,
        jitter_ms=jitter,
        status=status,
    )
