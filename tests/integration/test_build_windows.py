# The build harness substitutes a disposable project and fake PyInstaller output.
# It exercises publication and recovery without replacing the deployed application.

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts import build_windows


@pytest.fixture
def build_workspace(tmp_path: Path, monkeypatch):
    # Spaces in the disposable project path exercise argument-list handling.
    # Patch the interpreter identity instead of creating another virtual environment.
    project = tmp_path / "project with spaces"
    spec = project / "packaging" / "metadata-polisher.spec"
    spec.parent.mkdir(parents=True)
    spec.write_text("# Isolated build-command fixture.\n", encoding="utf-8")
    interpreter = project / ".venv" / "Scripts" / "python.exe"
    monkeypatch.setattr(build_windows, "PROJECT_ROOT", project)
    monkeypatch.setattr(build_windows.sys, "executable", str(interpreter))
    monkeypatch.setattr(build_windows.sys, "platform", "win32")
    monkeypatch.setenv("BUILD_TEST_EXISTING", "retained")

    # No virtual environment is created: this path represents the already-running
    # interpreter, and subprocess execution is replaced in every test below.
    return project, interpreter


def test_build_uses_running_repository_venv_and_local_output_cache_paths(build_workspace, monkeypatch, capsys):
    project, interpreter = build_workspace
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        target = Path(command[command.index("--distpath") + 1]) / "MetadataPolisher" / "MetadataPolisher.exe"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"Build-result marker, not an executable.")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(build_windows.subprocess, "run", run)
    assert build_windows.main() == 0
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[:3] == [str(interpreter), "-m", "PyInstaller"]
    assert command[-1] == str(project / "packaging" / "metadata-polisher.spec")
    assert "--noconfirm" in command
    staging_root = Path(command[command.index("--distpath") + 1])
    assert staging_root.resolve().is_relative_to((project / "build").resolve())
    assert staging_root != project / "dist"
    assert command[command.index("--workpath") + 1] == str(project / "build" / "pyinstaller")
    assert kwargs["cwd"] == project
    assert kwargs["env"]["PYINSTALLER_CONFIG_DIR"] == str(project / "build" / "pyinstaller-cache")
    assert kwargs["env"]["BUILD_TEST_EXISTING"] == "retained"
    assert kwargs.get("shell", False) is False
    assert "MetadataPolisher.exe" in capsys.readouterr().out
    assert (project / "dist" / "MetadataPolisher" / "MetadataPolisher.exe").is_file()


def existing_portable_folder(project):
    deployed = project / "dist" / "MetadataPolisher"
    files = {
        "MetadataPolisher.exe": b"Previous working build",
        "_internal/previous.dll": b"Previous bundled dependency",
        # Even a document the application cannot currently parse belongs to the
        # user and must survive a rebuild without rewriting or normalisation.
        "settings.json": b'{"rename": "preserve exact bytes"}\r\n',
        "logs/metadata-polisher.log": b"Existing application log\r\n",
        "logs/archive/metadata-polisher.log.1": b"Older log",
        "reports/APPLY-0001.json": b'{"status":"succeeded"}\n',
        "reports/nested/notes.txt": b"Existing report context",
    }

    for relative, content in files.items():
        target = deployed / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    return deployed


def test_build_staging_inherits_workspace_access_for_the_desktop_user(build_workspace, monkeypatch):
    project, _interpreter = build_workspace
    mkdir = os.mkdir
    directory_modes = {}
    staging = []

    def record_mkdir(path, mode=0o777, **kwargs):
        directory_modes[Path(path)] = mode
        return mkdir(path, mode, **kwargs)

    def run(command, **_kwargs):
        staged_dist = Path(command[command.index("--distpath") + 1])
        staging.append(staged_dist.parent)
        target = staged_dist / "MetadataPolisher" / "MetadataPolisher.exe"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"Fresh build")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(os, "mkdir", record_mkdir)
    monkeypatch.setattr(build_windows.subprocess, "run", run)

    assert build_windows.main() == 0
    assert staging[0].is_relative_to(project / "build")
    # On Python 3.13+ Windows, mode 0700 creates a protected owner-only ACL.
    # Moving that folder to dist retains the ACL and blocks the desktop user
    # when the build runs under a separate workspace sandbox account.
    assert directory_modes[staging[0]] != 0o700


def portable_file_bytes(folder):
    return {str(path.relative_to(folder)): path.read_bytes() for path in folder.rglob("*") if path.is_file()}


def fake_pyinstaller_output(command, project):
    target = Path(command[command.index("--distpath") + 1]) / "MetadataPolisher"

    # Model --noconfirm's destructive output replacement only inside this test's
    # disposable project. A command targeting a different tree must never run it.
    assert target.resolve().is_relative_to(project.resolve())

    if target.exists():
        shutil.rmtree(target)

    (target / "_internal").mkdir(parents=True)
    (target / "MetadataPolisher.exe").write_bytes(b"Fresh executable")
    (target / "_internal" / "new.dll").write_bytes(b"Fresh bundled dependency")
    return target


def test_successful_rebuild_preserves_runtime_files_and_nested_directories(build_workspace, monkeypatch):
    project, _interpreter = build_workspace
    deployed = existing_portable_folder(project)
    before = portable_file_bytes(deployed)

    def run(command, **_kwargs):
        assert portable_file_bytes(deployed) == before
        fake_pyinstaller_output(command, project)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(build_windows.subprocess, "run", run)

    assert build_windows.main() == 0

    after = portable_file_bytes(deployed)

    for relative, content in before.items():
        if relative == "settings.json" or Path(relative).parts[0] in ("logs", "reports"):
            assert after[relative] == content

    assert (deployed / "MetadataPolisher.exe").read_bytes() == b"Fresh executable"
    assert (deployed / "_internal" / "new.dll").is_file()
    assert not (deployed / "_internal" / "previous.dll").exists()


def test_failed_rebuild_keeps_the_previous_application_and_runtime_files(build_workspace, monkeypatch):
    project, _interpreter = build_workspace
    deployed = existing_portable_folder(project)
    before = portable_file_bytes(deployed)

    def run(command, **_kwargs):
        fake_pyinstaller_output(command, project)
        return subprocess.CompletedProcess(command, 7)

    monkeypatch.setattr(build_windows.subprocess, "run", run)

    assert build_windows.main() == 7
    assert portable_file_bytes(deployed) == before


def test_a_previous_executable_cannot_mask_missing_fresh_build_output(build_workspace, monkeypatch):
    project, _interpreter = build_workspace
    deployed = existing_portable_folder(project)
    before = portable_file_bytes(deployed)
    monkeypatch.setattr(
        build_windows.subprocess, "run", lambda command, **_kwargs: subprocess.CompletedProcess(command, 0),
    )

    assert build_windows.main() != 0
    assert portable_file_bytes(deployed) == before


def test_runtime_copy_failure_keeps_the_previous_portable_folder(build_workspace, monkeypatch, capsys):
    project, _interpreter = build_workspace
    deployed = existing_portable_folder(project)
    before = portable_file_bytes(deployed)
    copy_file = shutil.copy2

    def fail_settings_copy(source, destination, **kwargs):
        if Path(source) == deployed / "settings.json":
            raise OSError("Cannot copy existing settings")

        return copy_file(source, destination, **kwargs)

    def run(command, **_kwargs):
        fake_pyinstaller_output(command, project)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(build_windows.subprocess, "run", run)
    monkeypatch.setattr(shutil, "copy2", fail_settings_copy)

    assert build_windows.main() != 0
    assert portable_file_bytes(deployed) == before
    assert "settings" in capsys.readouterr().err.lower()


def test_publication_failure_restores_the_previous_portable_folder(build_workspace, monkeypatch, capsys):
    project, _interpreter = build_workspace
    deployed = existing_portable_folder(project)
    before = portable_file_bytes(deployed)
    replace_path = os.replace

    def fail_fresh_publication(source, destination):
        if Path(destination) == deployed and (Path(source) / "_internal" / "new.dll").exists():
            raise OSError("Cannot publish the fresh build")

        return replace_path(source, destination)

    def run(command, **_kwargs):
        fake_pyinstaller_output(command, project)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(build_windows.subprocess, "run", run)
    monkeypatch.setattr(os, "replace", fail_fresh_publication)

    assert build_windows.main() != 0
    assert portable_file_bytes(deployed) == before
    assert "publish" in capsys.readouterr().err.lower()


def test_build_failure_returns_nonzero_with_a_clear_message(build_workspace, monkeypatch, capsys):
    def run(command, **_kwargs):
        return subprocess.CompletedProcess(command, 7)

    monkeypatch.setattr(build_windows.subprocess, "run", run)
    assert build_windows.main() == 7
    assert "PyInstaller" in capsys.readouterr().err


def test_build_isolates_dll_discovery_from_unrelated_ambient_tools(build_workspace, monkeypatch):
    project, interpreter = build_workspace
    python_base = project / "base Python"
    windows = project / "Windows"
    unrelated_tools = project / "external Poppler" / "bin"
    monkeypatch.setattr(build_windows.sys, "base_prefix", str(python_base))
    monkeypatch.setenv("SYSTEMROOT", str(windows))
    monkeypatch.setenv("PATH", str(unrelated_tools))
    captured = {}

    def run(command, **kwargs):
        captured.update(kwargs["env"])
        return subprocess.CompletedProcess(command, 7)

    monkeypatch.setattr(build_windows.subprocess, "run", run)
    assert build_windows.main() == 7

    # Qt imports Windows' unversioned ICU API. An unrelated encoder or PDF tool
    # on PATH may provide an incompatible namesake DLL, which must not be bundled.
    assert captured["PATH"].split(os.pathsep) == [
        str(interpreter.parent),
        str(python_base),
        str(python_base / "DLLs"),
        str(windows / "System32"),
        str(windows),
    ]
    assert str(unrelated_tools) not in captured["PATH"]
    assert captured["BUILD_TEST_EXISTING"] == "retained"


def test_build_rejects_missing_output_after_a_successful_process(build_workspace, monkeypatch, capsys):
    monkeypatch.setattr(
        build_windows.subprocess, "run", lambda command, **_kwargs: subprocess.CompletedProcess(command, 0),
    )
    assert build_windows.main() != 0
    assert "MetadataPolisher.exe" in capsys.readouterr().err


def test_build_reports_subprocess_start_failure(build_workspace, monkeypatch, capsys):
    def run(*_args, **_kwargs):
        raise OSError("Could not start the existing interpreter")

    monkeypatch.setattr(build_windows.subprocess, "run", run)
    assert build_windows.main() != 0
    assert "Could not start" in capsys.readouterr().err


@pytest.mark.parametrize("problem", ["platform", "interpreter", "missing_spec"])
def test_build_rejects_unusable_starting_conditions_without_launching_tools(
    build_workspace, monkeypatch, capsys, problem,
):
    project, _interpreter = build_workspace

    def unexpected_run(*_args, **_kwargs):
        pytest.fail("Invalid build conditions must be checked before launching PyInstaller")

    monkeypatch.setattr(build_windows.subprocess, "run", unexpected_run)

    if problem == "platform":
        monkeypatch.setattr(build_windows.sys, "platform", "linux")
    elif problem == "interpreter":
        monkeypatch.setattr(build_windows.sys, "executable", str(project / "system-python.exe"))
    else:
        (project / "packaging" / "metadata-polisher.spec").unlink()

    assert build_windows.main() != 0
    assert capsys.readouterr().err
