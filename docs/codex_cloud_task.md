# Codex Cloud Task

Continue improving this Windows-focused PingPlotter-like network diagnostics tool toward PingPlotter-grade operational stability.

## Current State

- Python/PySide6 desktop app.
- Multi-target measurement is implemented.
- Session logs, session index, open/export/delete, and segmented CSV storage are implemented.
- Session segment metadata is written through atomic temp-file replace with transient PermissionError retries, matching the session index hardening style.
- All Targets Summary has problem-first sorting, double-click target switching, selected/visible/problem/all batch controls, selected-target group saving, and live selected-target count in the summary line.
- Alert UI supports loss, latency, jitter, sample-count, timer, MOS, route-IP, route-change, alert-ended events, and start/end action trigger controls.
- Probe engine UI supports ICMP and TCP Connect, with diagnostics and session metadata that clarify TCP Connect measures the final target service port while route discovery still uses Windows tracert/ICMP.
- Alert action logging distinguishes failed external actions as `email_failed`, `rest_failed`, and `executable_failed` so operators can see when an action was attempted but did not complete.
- Timeline UX has a separate main-screen status chip for visible timeline range, distinct from the focus-period chip.
- Main-screen timeline controls can apply 60s/10m/1h/6h/24h/48h visible ranges and reset focus/timeline back to current samples.
- Statistics export supports grouping, timezone, empty-range protection, and scope selection: All time, Visible timeline, Focus period, Custom range.
- Report export supports TXT and printable HTML templates with target, range, analysis, evidence annotations, and hop metrics.
- PNG image export supports Timeline graph, Trace table, and Both scopes from the main export panel.
- Release verification includes a deterministic 50-target soak smoke test with simulated probes, timeout backoff checks, and session-log persistence checks.
- `scripts\soak_test.py` supports named profiles: `release` for fast 50-target release smoke, `long` for 30-minute 50-target stability, and `ui` for offscreen MainWindow wiring.
- Analysis logic now distinguishes middle-hop-only latency/jitter from inherited end-to-end symptoms, reducing false bandwidth-saturation or Wi-Fi/congestion diagnoses when the final target is healthy.
- Analysis logic now adds provider/border handoff cause codes when inherited loss starts at a specific hop, separates full target-only timeout as possible ICMP/firewall blocking, and includes report-ready evidence guidance for escalation.

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
   - Main-screen visible range status is now shown separately from Focus period.
   - Reset-to-current behavior now clears both Focus period and visible timeline range from the main screen.
   - Main graph and detail graph both expose 60s/10m/1h/6h/24h/48h visible range controls.
   - Continue refining keyboard shortcuts and direct drag/scale operator guidance.

2. Expand Export/Report options.
   - Explicit Start/End Date export range controls are implemented for statistics exports as Custom range.
   - TXT and HTML report templates are implemented; keep refining operator-friendly report sections and optional image bundling.
   - Image export scope is implemented for Timeline graph, Trace table, and Both.
   - Ensure visible-time exports work from session logs and live buffers.

3. Strengthen analysis logic.
   - Cause codes now cover local LAN/Wi-Fi, local access link, ISP/upstream segment, provider/border handoff, provider/border congestion, intermediate-hop ICMP rate-limit/deprioritization, target ICMP/firewall block, and target/service filtering.
   - Keep final-destination-first interpretation.
   - Continue improving operator-facing explanations and report evidence grouping for provider escalation.

4. Extend deterministic soak coverage.
   - The release verifier now covers a short 50-target simulated soak.
   - Manual long-run check: `python scripts\soak_test.py --profile long`.
   - Manual UI wiring check: `python scripts\soak_test.py --profile ui`.
   - Add longer manual or scheduled soak profiles for 30+ minute runs, UI-driven mode, and packaging-only environments.
   - Keep asserting worker loop delay, queue depth, memory-bounded recent observations, timeout backoff, and session-log writes.

5. Keep packaging healthy.
   - Run `python -m pytest -q`.
   - Run `python scripts\verify_release.py`.
   - On Windows, run `python scripts\verify_release.py --exe`.
   - Build with `powershell -NoProfile -ExecutionPolicy Bypass -File .\build_windows_exe.ps1`.

## Official PingPlotter Documentation Priority Notes

Based on the official PingPlotter documentation, the current project is already fairly close to basic PingPlotter-style measurement, graphing, focus range handling, and export behavior. However, it still needs several improvement areas before it can be considered a PingPlotter-grade stable operations tool.

Core conclusion: prioritize multi-target long-run stability, session save/restore architecture, alert conditions/actions, and probe engine choice before adding cosmetic features.

### High Priority

1. Strengthen multi-target operations.
   - PingPlotter provides an All Targets Summary that lets operators compare many targets at a glance and change interval/config settings for multiple selected targets.
   - The current project can measure multiple IPs, filter/sort the summary, batch-control selected/visible/problem/all target sets, save selected targets as reusable groups, and show the active selected-target count before batch actions.
   - Remaining parity work is deeper grouping/profile management and richer summary-history views.
   - References:
     - https://www.pingplotter.com/manual/summary_graphs/
     - https://www.pingplotter.com/manual/tracing_to_multiple_targets/

2. Improve long-term storage and session recovery.
   - PingPlotter keeps saving sessions and allows operators to reopen, export, and manage them later through Session Manager.
   - PingPlotter Flex Storage separates storage by target and time range so many targets and long history can still load efficiently.
   - The current project has segmented CSV storage, session index recovery, stale active-session recovery, retention/delete controls, resume preparation with source-session lineage metadata, and atomic segment-index replacement.
   - Remaining parity work is a richer session database, stronger resume semantics, retention policy presets, and faster history browsing across many target-month buckets.
   - References:
     - https://www.pingplotter.com/manual/auto-saving-data/
     - https://www.pingplotter.com/manual/session-manager/
     - https://www.pingplotter.com/manual/flex-storage/

3. Expand the alert system.
   - The current project supports configurable loss, latency, jitter, sample-count, timer, MOS, route-IP, route-change, and alert-ended events.
   - It can run selected actions on alert start and/or recovery: timeline annotation, comment, log, beep, image save, email, REST call, and executable launch.
   - External action failures are now visible in the alert action log instead of being hidden behind the configured action name.
   - Route Adjustment is exposed as an alert action for Final Hop Only sessions: target alerts can switch to Full Route, respect the configured alert thresholds, and optionally restore Final Hop Only on recovery.
   - Remaining parity work includes richer action templates and stronger operator guidance around alert presets.
   - References:
     - https://www.pingplotter.com/manual/alert-conditions/
     - https://www.pingplotter.com/manual/help_alerts/
     - https://www.pingplotter.com/manual/final-hop-only/

4. Add meaningful probe engine choice.
   - The current project is centered on Windows ICMP ping and tracert behavior.
   - PingPlotter supports ICMP and TCP SYN-style tracing, which is important when ICMP/UDP is blocked and the operator needs to inspect a real service path such as TCP 443.
   - This matters in company networks, VPNs, and firewall-heavy environments.
   - References:
     - https://www.pingplotter.com/manual/packetoptions/
     - https://www.pingplotter.com/manual/tcp-packets/

### Important Second Priority

5. Improve Timeline UX.
   - The current project has focus range, graph zoom, and some navigation behavior.
   - PingPlotter connects timeline dragging, mouse-wheel zooming, 60-second to 48-hour scales, Reset Focus to Current, and per-hop timeline visibility in a more natural workflow.
   - This is important when tracking the exact failure window.
   - Reference:
     - https://www.pingplotter.com/manual/time_line_graphing/

6. Expand export and report options.
   - The current project supports CSV, XLSX, TXT, printable HTML reports, and PNG.
   - PingPlotter-style parity should continue improving report presentation, optional image bundling, and clear errors when a selected range has no samples.
   - References:
     - https://www.pingplotter.com/manual/export-statistics/
     - https://www.pingplotter.com/manual/save-an-image/

7. Strengthen analysis logic.
   - PingPlotter documentation emphasizes checking the final destination first, then finding the first hop where the same symptom begins.
   - The current analyzer already follows that direction and now classifies bandwidth saturation, ISP/upstream segment issues, provider/border handoff or congestion, intermediate-hop ICMP rate limiting/deprioritization, target ICMP/firewall blocking, target/service filtering, and Wi-Fi/LAN issues with clearer cause codes and recommended actions.
   - Remaining parity work is richer root-cause grouping, report-ready evidence timelines, and better operator guidance when multiple symptoms overlap.
   - Reference:
     - https://www.pingplotter.com/manual/voiptroubleshooting/

### Recommended Implementation Order

1. Session Manager and Flex Storage-style persistence.
2. All Targets Summary with batch pause/resume/interval controls.
3. Alert Rule UI with actions.
4. TCP/ICMP probe engine selection.
5. Export and Timeline UX refinement.

The analysis above is a planning instruction. It does not mean these items are already implemented.

## Expected Completion Report

- Summarize changed behavior and files.
- Report test commands and results.
- Mention any limitations, especially if Windows-only packaging or live network behavior could not be verified in Cloud.
