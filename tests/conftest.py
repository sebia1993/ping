from __future__ import annotations

import os

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication


@pytest.fixture
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def isolated_application_directories(tmp_path, monkeypatch):
    """Keep tests from reading or writing the real user's saved sessions."""

    monkeypatch.setenv("MULTIPINGCHECK_DATA_DIR", str(tmp_path / "app-data"))
    monkeypatch.setenv("MULTIPINGCHECK_EXPORT_DIR", str(tmp_path / "exports"))
