from __future__ import annotations

import logging

from app.utils.diagnostics import DIAGNOSTICS_LOGGER_NAME, operation_failure, reference


def test_reference_is_stable_without_exposing_original_value() -> None:
    value = "192.0.2.10|C:\\Users\\operator\\secret-session.csv"

    first = reference(value)
    second = reference(value)

    assert first == second
    assert len(first) == 12
    assert value not in first


def test_operation_failure_logs_code_and_sanitizes_operational_values(caplog) -> None:
    target = "198.51.100.20"
    session_path = r"C:\Users\operator\secret-session.csv"

    with caplog.at_level(logging.ERROR, logger=DIAGNOSTICS_LOGGER_NAME):
        operation_failure(
            "SESSION_LOG_WRITE_FAILED",
            "measurement.session_log_write",
            PermissionError("access denied: " + session_path),
            target=target,
            session_path=session_path,
        )

    assert "event=SESSION_LOG_WRITE_FAILED" in caplog.text
    assert "stage=measurement.session_log_write" in caplog.text
    assert "exception=PermissionError" in caplog.text
    assert target not in caplog.text
    assert session_path not in caplog.text
    assert "access denied" not in caplog.text
