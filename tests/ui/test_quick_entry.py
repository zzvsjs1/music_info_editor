"""The sole QML runtime checks portable storage before starting services."""

import pytest


def test_quick_storage_failure_prevents_service_construction(qapp, monkeypatch):
    from metadata_polisher.infrastructure.paths import WritableCheckResult
    from metadata_polisher.ui.quick import application

    shown = []
    monkeypatch.setattr(
        application,
        "ensure_writable_application_dir",
        lambda _: WritableCheckResult(False, "Read-only location"),
        raising=False,
    )
    monkeypatch.setattr(application, "QuickBackend", lambda **_: pytest.fail("Backend started before storage check"))
    monkeypatch.setattr(application, "_show_startup_error", lambda app, message: shown.append(message), raising=False)
    assert application.run_quick(["metadata-polisher", "--qml"]) == 1
    assert shown == ["Read-only location"]
