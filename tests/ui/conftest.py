"""Close disposable windows after assertions without asking to save test state."""

from unittest.mock import patch

import pytest


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_teardown(item):
    # This wrapper also unwinds if teardown fails, preventing its temporary
    # discard policy from leaking into another test's real user interactions.
    # pytest-qt closes registered widgets before fixture teardown. Limit this
    # override to that teardown phase: tests still exercise real close guards.
    with patch("metadata_polisher.ui.main_window.confirm_discard_pending", return_value=True):
        return (yield)
