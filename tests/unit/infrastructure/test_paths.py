import sys
from pathlib import Path

from metadata_polisher.infrastructure import paths as paths_module
from metadata_polisher.infrastructure.paths import (
    ApplicationPaths,
    ensure_writable_application_dir,
)


def assert_runtime_paths_are_children(paths: ApplicationPaths) -> None:
    assert paths.settings_file == paths.app_dir / "settings.json"
    assert paths.logs_dir == paths.app_dir / "logs"
    assert paths.reports_dir == paths.app_dir / "reports"


def test_detect_uses_executable_directory_when_frozen(
    monkeypatch,
    tmp_path: Path,
) -> None:
    executable = tmp_path / "MetadataPolisher" / "MetadataPolisher.exe"
    # Simulate the packaged runtime without building an executable; every
    # application-owned path must follow this executable's directory.
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))

    paths = ApplicationPaths.detect()

    assert paths.app_dir == executable.parent
    assert_runtime_paths_are_children(paths)


def test_detect_uses_current_directory_when_running_from_source(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.chdir(tmp_path)

    paths = ApplicationPaths.detect()

    assert paths.app_dir == tmp_path
    assert_runtime_paths_are_children(paths)


def test_writable_check_creates_log_directory_without_changing_settings(
    tmp_path: Path,
) -> None:
    settings_file = tmp_path / "settings.json"
    # Existing JSON is a preservation sentinel: the writability probe may
    # create temporary files but must not repair or rewrite these contents.
    settings_file.write_text('{"keep": "unchanged"}', encoding="utf-8")
    paths = ApplicationPaths(
        app_dir=tmp_path,
        settings_file=settings_file,
        logs_dir=tmp_path / "logs",
        reports_dir=tmp_path / "reports",
    )

    result = ensure_writable_application_dir(paths)

    assert result.ok
    assert result.message is None
    assert paths.logs_dir.is_dir()
    assert settings_file.read_text(encoding="utf-8") == '{"keep": "unchanged"}'
    assert list(tmp_path.glob(".metadata-polisher-write-test-*")) == []
    assert list(paths.logs_dir.iterdir()) == []


def test_writable_check_rejects_missing_application_directory(tmp_path: Path) -> None:
    app_dir = tmp_path / "missing"
    paths = ApplicationPaths(
        app_dir=app_dir,
        settings_file=app_dir / "settings.json",
        logs_dir=app_dir / "logs",
        reports_dir=app_dir / "reports",
    )

    result = ensure_writable_application_dir(paths)

    assert not result.ok
    assert result.message is not None
    assert not app_dir.exists()


def test_writable_check_rejects_settings_path_that_is_a_directory(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.mkdir()
    paths = ApplicationPaths(
        app_dir=tmp_path,
        settings_file=settings_file,
        logs_dir=tmp_path / "logs",
        reports_dir=tmp_path / "reports",
    )

    result = ensure_writable_application_dir(paths)

    assert not result.ok
    assert result.message is not None


def test_writable_check_rejects_log_path_that_is_a_file(tmp_path: Path) -> None:
    logs_file = tmp_path / "logs"
    logs_file.write_text("not a directory", encoding="utf-8")
    paths = ApplicationPaths(
        app_dir=tmp_path,
        settings_file=tmp_path / "settings.json",
        logs_dir=logs_file,
        reports_dir=tmp_path / "reports",
    )

    result = ensure_writable_application_dir(paths)

    assert not result.ok
    assert result.message is not None
    assert list(tmp_path.glob(".metadata-polisher-write-test-*")) == []


def test_existing_writable_settings_still_require_permission_to_create_atomic_save_file(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    paths = ApplicationPaths.detect()
    paths.settings_file.write_text("{}", encoding="utf-8")
    paths.logs_dir.mkdir()

    def probe(directory):
        if directory == paths.app_dir:
            raise PermissionError("Cannot create a temporary settings file")

    monkeypatch.setattr(paths_module, "_probe_directory", probe)

    result = ensure_writable_application_dir(paths)

    assert not result.ok
    assert "writable directory" in result.message
    assert paths.settings_file.read_text(encoding="utf-8") == "{}"
