"""Use the same GUI application type as the shipped Qt Quick frontend."""

import pytest
from PySide6.QtGui import QGuiApplication


@pytest.fixture(scope="session")
def qapp_cls():
    # pytest-qt otherwise constructs QApplication and silently keeps the retired
    # Widgets module in every test process. QML needs only QGuiApplication.
    return QGuiApplication
