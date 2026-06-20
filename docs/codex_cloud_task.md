# Codex Cloud Task

Continue improving this Windows-focused PingPlotter-like network diagnostics tool toward PingPlotter-grade operational stability.

## Current State

- Python/PySide6 desktop app.
- Multi-target measurement is implemented.
- Session logs, session index, open/export/delete, and segmented CSV storage are implemented.
- All Targets Summary has problem-first sorting and double-click target switching.
- Alert UI supports loss, latency, sample-count alert, route-change annotation, and alert-ended events.
- Probe engine UI supports ICMP and TCP Connect, with diagnostics that clarify TCP Connect measures the final target service port while route discovery still uses Windows tracert/ICMP.
- Statistics export supports grouping, timezone, and scope selection: All time, Visible timeline, Focus period.

## Constraints

- Do not require real company logs, device captures, credentials, or customer data.
- Use fixtures, mocks, and simulated probes for tests.
- Keep sensitive data out of logs and generated artifacts.
- Preserve Windows and Windows PowerShell compatibility.
- Avoid live network changes.
- Prefer deterministic tests over real network dependency.
- Do not commit generated `build/`, `dist/`, `artifacts/`, `release/`, `exports/`, or `logs/` files.

## Recommended Next Work

1. Improve Timeline UX.
   - Add clearer visible range controls on the main graph or detail graph.
   - Support reset-to-current/current-window behavior consistently.
   - Make timeline range and focus range visually distinct.

2. Expand Export/Report options.
   - Add explicit Start/End Date export range controls.
   - Consider image export options for Tracegraph, Timegraph, or both.
   - Ensure visible-time exports work from session logs and live buffers.

3. Strengthen analysis logic.
   - Add clearer cause codes for bandwidth saturation, ISP segment issue, ICMP rate-limit, firewall block, and local LAN/Wi-Fi issue.
   - Keep final-destination-first interpretation.

4. Add longer deterministic soak tests.
   - Cover 50+ targets with mocked probes and no real network dependency.
   - Assert worker loop delay, queue depth, memory-bounded recent observations, and session-log writes.

5. Keep packaging healthy.
   - Run `python -m pytest -q`.
   - Run `python scripts\verify_release.py`.
   - On Windows, run `python scripts\verify_release.py --exe`.
   - Build with `powershell -NoProfile -ExecutionPolicy Bypass -File .\build_windows_exe.ps1`.

## Expected Completion Report

- Summarize changed behavior and files.
- Report test commands and results.
- Mention any limitations, especially if Windows-only packaging or live network behavior could not be verified in Cloud.
