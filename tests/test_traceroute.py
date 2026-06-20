from __future__ import annotations

from app.core.traceroute import ensure_target_hop, parse_tracert_output


def test_parse_english_tracert_output() -> None:
    output = """
Tracing route to dns.google [8.8.8.8]
  1    <1 ms    <1 ms    <1 ms  router.local [192.168.0.1]
  2     5 ms     6 ms     5 ms  10.10.0.1
  3    20 ms    19 ms    20 ms  dns.google [8.8.8.8]
"""
    hops = parse_tracert_output(output)
    assert len(hops) == 3
    assert hops[0].index == 1
    assert hops[0].address == "192.168.0.1"
    assert hops[0].hostname == "router.local"
    assert hops[2].address == "8.8.8.8"


def test_parse_timeout_hop() -> None:
    output = """
  1     *        *        *     Request timed out.
  2    10 ms    11 ms    10 ms  192.0.2.1
"""
    hops = parse_tracert_output(output)
    assert hops[0].timed_out is True
    assert hops[0].address is None
    assert hops[1].address == "192.0.2.1"


def test_ensure_target_hop_adds_missing_target() -> None:
    hops = parse_tracert_output("  1    1 ms    1 ms    1 ms  192.168.0.1")
    ensured = ensure_target_hop(hops, "8.8.8.8", "8.8.8.8")
    assert ensured[-1].is_target is True
    assert ensured[-1].address == "8.8.8.8"


def test_ensure_target_hop_marks_existing_target() -> None:
    hops = parse_tracert_output(
        """
  1    <1 ms    <1 ms    <1 ms  router.local [192.168.0.1]
  2    20 ms    19 ms    20 ms  dns.google [8.8.8.8]
"""
    )

    ensured = ensure_target_hop(hops, "dns.google", "8.8.8.8")

    assert len(ensured) == 2
    assert ensured[-1].is_target is True
    assert ensured[-1].hostname == "dns.google"
