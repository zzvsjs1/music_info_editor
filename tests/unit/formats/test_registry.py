import logging
from pathlib import Path

import pytest

from metadata_polisher.domain.errors import Issue, MediaErrorCode
from metadata_polisher.formats.base import VerificationResult
from metadata_polisher.formats.flac import FlacAdapter
from metadata_polisher.formats.mp3 import Mp3Adapter
from metadata_polisher.formats.mp4 import Mp4Adapter
from metadata_polisher.formats.registry import FormatRegistry
from metadata_polisher.formats.tak import TakAdapter
from metadata_polisher.formats.wave import WaveAdapter


class FakeAdapter:
    def __init__(
        self,
        format_id: str,
        extensions: frozenset[str],
        *,
        probe_result: bool = False,
        probe_error: Exception | None = None,
    ) -> None:
        self.format_id = format_id
        self.extensions = extensions
        self.probe_result = probe_result
        self.probe_error = probe_error
        self.probed_paths: list[Path] = []

    def can_handle(self, path: Path) -> bool:
        self.probed_paths.append(path)

        if self.probe_error is not None:
            raise self.probe_error

        return self.probe_result


def test_detect_filters_by_extension_then_probes_candidates_in_registration_order(
    tmp_path: Path,
) -> None:
    path = tmp_path / "TRACK.FLAC"
    # Four adapters distinguish extension filtering, a rejected content probe,
    # first-match selection and the short-circuit after a successful probe.
    unrelated = FakeAdapter("mp3", frozenset({".mp3"}), probe_result=True)
    rejected = FakeAdapter("flac-first", frozenset({".flac"}), probe_result=False)
    accepted = FakeAdapter("flac-second", frozenset({".flac"}), probe_result=True)
    unreached = FakeAdapter("flac-third", frozenset({".flac"}), probe_result=True)
    registry = FormatRegistry((unrelated, rejected, accepted, unreached))

    detected = registry.detect(path)

    assert detected is accepted
    assert unrelated.probed_paths == []
    assert rejected.probed_paths == [path]
    assert accepted.probed_paths == [path]
    assert unreached.probed_paths == []


def test_detect_returns_none_for_unsupported_extension_without_probing(tmp_path: Path) -> None:
    path = tmp_path / "track.opus"
    flac = FakeAdapter("flac", frozenset({".flac"}), probe_result=True)
    mp3 = FakeAdapter("mp3", frozenset({".mp3"}), probe_result=True)
    registry = FormatRegistry((flac, mp3))

    detected = registry.detect(path)

    assert detected is None
    assert flac.probed_paths == []
    assert mp3.probed_paths == []


def test_registry_exposes_normalised_registered_extensions() -> None:
    first = FakeAdapter("first", frozenset({".FLAC", ".m4a"}))
    second = FakeAdapter("second", frozenset({".Mp4", ".M4A"}))

    registry = FormatRegistry((first, second))

    assert registry.supported_extensions == frozenset({".flac", ".m4a", ".mp4"})


def test_probe_failure_is_logged_and_does_not_block_later_candidate(
    caplog,
    tmp_path: Path,
) -> None:
    path = tmp_path / "ambiguous.mp4"
    broken = FakeAdapter(
        "broken-mp4",
        frozenset({".mp4"}),
        probe_error=RuntimeError("invalid atom table"),
    )
    accepted = FakeAdapter("audio-mp4", frozenset({".mp4"}), probe_result=True)
    registry = FormatRegistry((broken, accepted))

    with caplog.at_level(logging.WARNING, logger="metadata_polisher.formats.registry"):
        detected = registry.detect(path)

    assert detected is accepted
    assert broken.probed_paths == [path]
    assert accepted.probed_paths == [path]
    assert "broken-mp4" in caplog.text
    assert str(path) in caplog.text


@pytest.mark.parametrize(
    ("filename", "expected_type"),
    [
        ("track.flac", FlacAdapter),
        ("track.mp3", Mp3Adapter),
        ("track.m4a", Mp4Adapter),
        ("track.mp4", Mp4Adapter),
        ("track.wav", WaveAdapter),
        ("track.tak", TakAdapter),
    ],
)
def test_default_registry_contains_every_v1_adapter(
    filename: str,
    expected_type: type,
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(expected_type, "can_handle", lambda self, path: True)

    detected = FormatRegistry().detect(tmp_path / filename)

    assert isinstance(detected, expected_type)


def test_verification_result_is_immutable_and_normalises_issues_to_tuple() -> None:
    issue = Issue(
        code=MediaErrorCode.VERIFICATION_FAILED,
        message="Written metadata did not match the reviewed values.",
    )
    source_issues = [issue]

    result = VerificationResult(ok=False, issues=source_issues)  # type: ignore[arg-type]
    source_issues.clear()

    assert result.issues == (issue,)
