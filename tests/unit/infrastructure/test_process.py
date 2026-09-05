import subprocess
from collections.abc import Sequence
from pathlib import Path

from metadata_polisher.infrastructure.process import (
    ExternalToolProbe,
    ExternalToolResolver,
    ExternalToolStatus,
    ProcessResult,
    ProcessRunner,
)


# Record requested executable arguments without launching a process. Resolver
# tests can then prove that invalid paths never trigger implicit discovery.
class RecordingRunner:
    def __init__(self, result: ProcessResult) -> None:
        self.result = result
        self.calls: list[tuple[tuple[str, ...], float]] = []

    def run(
        self,
        arguments: Sequence[str],
        *,
        timeout_seconds: float,
    ) -> ProcessResult:
        self.calls.append((tuple(arguments), timeout_seconds))
        return self.result


def test_process_runner_uses_argument_list_and_shell_false(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(arguments, **kwargs):
        captured["arguments"] = arguments
        captured.update(kwargs)
        return subprocess.CompletedProcess(arguments, 0, "tool 1.0", "")

    # Intercept at the subprocess boundary to assert shell=False and exact
    # argument separation without depending on any external tool installation.
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = ProcessRunner().run(
        [r"C:\Tools\probe.exe", "--version"],
        timeout_seconds=4.0,
    )

    assert captured["arguments"] == [r"C:\Tools\probe.exe", "--version"]
    assert captured["shell"] is False
    assert result == ProcessResult(return_code=0, stdout="tool 1.0", stderr="")


def test_configured_valid_path_is_accepted_after_specific_probe(tmp_path: Path) -> None:
    executable = tmp_path / "tool.exe"
    executable.touch()
    runner = RecordingRunner(ProcessResult(return_code=0, stdout="tool 2.1", stderr=""))
    resolver = ExternalToolResolver(runner)
    probe = ExternalToolProbe(arguments=("--version",), timeout_seconds=3.0)

    result = resolver.resolve_configured(str(executable), probe)

    assert result.status is ExternalToolStatus.READY
    assert result.path == executable
    assert runner.calls == [((str(executable), "--version"), 3.0)]


def test_missing_or_invalid_configured_path_needs_user_selection(tmp_path: Path) -> None:
    runner = RecordingRunner(ProcessResult(return_code=0, stdout="unused", stderr=""))
    resolver = ExternalToolResolver(runner)
    probe = ExternalToolProbe(arguments=("--version",))

    missing = resolver.resolve_configured(None, probe)
    invalid = resolver.resolve_configured(str(tmp_path / "missing.exe"), probe)

    assert missing.status is ExternalToolStatus.NEEDS_USER_SELECTION
    assert invalid.status is ExternalToolStatus.NEEDS_USER_SELECTION
    assert missing.path is None
    assert invalid.path is None
    assert runner.calls == []


def test_newly_selected_executable_is_probed_and_returned_for_persistence(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "selected.exe"
    executable.touch()
    runner = RecordingRunner(ProcessResult(return_code=0, stdout="selected 1.0", stderr=""))
    resolver = ExternalToolResolver(runner)
    probe = ExternalToolProbe(arguments=("-version",), timeout_seconds=7.5)

    # Native file-dialog APIs return a string path, so this boundary accepts that
    # value directly before returning a typed Path for persistence.
    result = resolver.validate_selected(str(executable), probe)

    assert result.status is ExternalToolStatus.READY
    assert result.path == executable
    assert runner.calls == [((str(executable), "-version"), 7.5)]


def test_failed_probe_needs_user_selection_and_exposes_no_search_api(tmp_path: Path) -> None:
    executable = tmp_path / "wrong-tool.exe"
    executable.touch()
    runner = RecordingRunner(ProcessResult(return_code=2, stdout="", stderr="wrong executable"))
    resolver = ExternalToolResolver(runner)

    result = resolver.resolve_configured(
        str(executable),
        ExternalToolProbe(arguments=("--identify",)),
    )

    assert result.status is ExternalToolStatus.NEEDS_USER_SELECTION
    assert result.path is None
    assert result.detail == "wrong executable"
    assert not hasattr(resolver, "search")
    assert not hasattr(resolver, "discover")
