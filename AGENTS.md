# MultiPingCheck Codex Instructions

## Project Summary

This repository is a Windows-focused PySide6 desktop tool for multi-target ping
monitoring. The product direction is a practical long-running network operations
tool, closer to PingPlotter-style monitoring than a short one-time ping checker.

The current priority is stability, not feature expansion.

## Operating Model

- Use GitHub as the sync point between Windows PC, MacBook, and GitHub Actions.
- MacBook is suitable for code edits, unit tests, deterministic simulated soak
  tests, and Codex-driven stability work.
- Windows PC or GitHub Actions `windows-latest` is required for final Windows
  packaging, EXE validation, and release artifact generation.
- Do not assume a Windows path exists on macOS. Use repository-relative paths
  whenever possible.

## Stability Priorities

Prioritize these areas before adding new user-facing features:

1. Long-running simulated soak stability.
2. UI freeze/event-gap measurement for 10, 20, and 50 targets.
3. Worker start, stop, cancel, timeout, and cleanup behavior.
4. Session save, open, delete, retry-pending-delete, and recovery integrity.
5. CSV, segmented CSV, statistics export, and release verification integrity.
6. Alert/event deduplication and log durability.
7. Windows path, Korean path, file-locking, permission, and encoding failures.

## Testing Rules

- Prefer deterministic tests using fake probes, fixtures, and simulated probe
  runners.
- Do not add tests that require real company devices, real internal networks,
  real ICMP/TCP internet targets, customer data, credentials, or private logs.
- For long-run validation, prefer `scripts/soak_test.py` and
  `scripts/run_stability_soak_suite.py`.
- Keep failures visible. Do not weaken tests only to make CI pass.

Useful verification commands:

```powershell
python -m pytest -q
python scripts\verify_release.py
python scripts\soak_test.py --profile release
python scripts\run_stability_soak_suite.py --dry-run
```

On macOS, use equivalent shell paths:

```bash
python -m pytest -q
python scripts/verify_release.py
python scripts/soak_test.py --profile release
python scripts/run_stability_soak_suite.py --dry-run
```

## Packaging And Release

- Do not commit generated output folders such as `build/`, `dist/`, `release/`,
  `artifacts/`, `exports/`, `outputs/`, or `logs/`.
- Source changes should be committed and pushed through Git.
- Windows EXE and ZIP deliverables should be uploaded through GitHub Releases or
  GitHub Actions artifacts, not committed as source files.
- Before release, run the project tests and release verifier. For Windows
  deliverables, also run the Windows packaging/smoke checks on Windows or a
  GitHub Actions Windows runner.

## UI And Feature Scope

- Preserve the current simplified operator workflow unless the user explicitly
  asks to change it.
- Avoid unnecessary UI expansion. The main screen should stay focused on IP
  input, current target status when needed, and readable per-target real-time
  graphs.
- Stability and readability are more important than adding advanced controls.

## Git Safety

- The worktree may contain user or unfinished Codex changes. Inspect
  `git status --short --branch` before committing.
- Do not revert unrelated changes.
- Stage only the files that belong to the current task.
- Keep commit messages and release notes understandable for a Korean-speaking
  non-developer user when the change is user-facing.
