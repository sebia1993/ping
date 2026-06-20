from __future__ import annotations

import os

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication


@pytest.fixture
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])
