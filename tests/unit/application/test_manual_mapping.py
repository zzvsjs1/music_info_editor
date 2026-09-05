# Use multiple media to distinguish a track index within the chosen disc from
# a release-wide position; a human assignment must keep that distinction intact.

from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from metadata_polisher.application.changes import ChangeSetStatus
from metadata_polisher.application.review import build_selected_file_results, set_manual_track_assignment
from metadata_polisher.domain.matching import (
    ComposerCredit,
    CreditScope,
    LocalisedText,
    MetadataProvenance,
    ProviderTrack,
    ReleaseCandidate,
    ReleaseMedium,
)
from metadata_polisher.domain.media import LocalMediaFile, MediaReadResult, StreamInfo
from metadata_polisher.domain.metadata import FieldReadState, MetadataField, MetadataSnapshot, Position
from metadata_polisher.matching.release_scoring import MatchClassification, MatchEvidence, MatchReasonCode
from metadata_polisher.matching.track_mapping import TrackMapping, TrackMappingResult
from metadata_polisher.providers.coordinator import CoordinatedCandidate


def inputs() -> tuple[tuple[LocalMediaFile, ...], ReleaseCandidate, TrackMappingResult]:
    files = tuple(
        LocalMediaFile(
            Path(f"library/{number}.flac"),
            "flac",
            MediaReadResult(
                MetadataSnapshot(album="Album", track=Position(number)),
                {
                    field: FieldReadState.PRESENT
                    if field in (MetadataField.ALBUM, MetadataField.TRACK)
                    else FieldReadState.MISSING
                    for field in MetadataField
                },
                StreamInfo(None, None, None, None, "FLAC"),
            ),
        )
        for number in (1, 2)
    )
    candidate = ReleaseCandidate(
        "musicbrainz", "musicbrainz", "release", (LocalisedText("Album", "en", "Latn"),), (), None,
        (
            ReleaseMedium(
                1, "First disc", (ProviderTrack(1, (LocalisedText("Other disc", None, None),), (), (), None),),
            ),
            ReleaseMedium(
                2, "Second disc",
                tuple(
                    ProviderTrack(number, (LocalisedText(title, "en", "Latn"),), (), ("Composer",), None,
                                  (ComposerCredit(("Composer",), CreditScope.TRACK),))
                    for number, title in ((1, "Opening"), (2, "Finale"))
                ),
            ),
        ),
        None,
    )
    first = TrackMapping(
        files[0].file_id, 0, Position(1, 2), Position(2, 2), 91.0, MatchClassification.HIGH,
        (MatchEvidence(MatchReasonCode.TRACK_TITLE_EXACT, 40.0, "Existing automatic pair evidence."),),
    )
    mapping = TrackMappingResult(
        (first,), (files[1].file_id,), (1,), 1, 2, MatchClassification.REVIEW,
        (MatchEvidence(MatchReasonCode.TRACK_MAPPING_PARTIAL, 0.0, "One local track is unmatched."),),
    )

    return files, candidate, mapping


def test_manual_assignment_completes_partition_with_human_evidence_and_retains_other_pair() -> None:
    files, candidate, before = inputs()
    changed = set_manual_track_assignment(
        files, candidate, before, local_file_id=files[1].file_id, provider_track_index=1,
    )

    assert changed.mappings[0] is before.mappings[0]
    assert tuple(item.local_file_id for item in changed.mappings) == tuple(file.file_id for file in files)
    chosen = changed.mappings[1]
    assert chosen.provider_track_index == 1
    assert chosen.track_position == Position(2, 2)
    assert chosen.disc_position == Position(2, 2)
    assert chosen.classification is MatchClassification.HIGH
    assert chosen.score == 0.0
    assert chosen.reason_codes == ("MANUAL_TRACK_ASSIGNMENT",)
    assert chosen.evidence[0].contribution == 0.0
    assert files[1].file_id in chosen.evidence[0].detail
    assert changed.selected_medium_index == 1
    assert changed.selected_medium_number == 2
    assert changed.unmatched_local_file_ids == ()
    assert changed.unmatched_provider_indexes == ()
    assert changed.classification is MatchClassification.HIGH
    assert MatchReasonCode.TRACK_MAPPING_COMPLETE in changed.reason_codes
    assert MatchReasonCode.TRACK_MAPPING_PARTIAL not in changed.reason_codes
    assert before.unmatched_local_file_ids == (files[1].file_id,)


def test_occupied_provider_track_requires_explicit_clear_before_reassignment() -> None:
    files, candidate, before = inputs()

    with pytest.raises(ValueError, match="already|assigned|unused"):
        set_manual_track_assignment(
            files, candidate, before, local_file_id=files[1].file_id, provider_track_index=0,
        )

    cleared = set_manual_track_assignment(
        files, candidate, before, local_file_id=files[0].file_id, provider_track_index=None,
    )
    assert cleared.mappings == ()
    assert cleared.unmatched_local_file_ids == tuple(file.file_id for file in files)
    assert cleared.unmatched_provider_indexes == (0, 1)
    assert "MANUAL_TRACK_UNMAPPED" in cleared.reason_codes

    reassigned = set_manual_track_assignment(
        files, candidate, cleared, local_file_id=files[1].file_id, provider_track_index=0,
    )
    assert reassigned.mappings[0].local_file_id == files[1].file_id
    assert reassigned.mappings[0].provider_track_index == 0
    assert reassigned.unmatched_local_file_ids == (files[0].file_id,)
    assert reassigned.unmatched_provider_indexes == (1,)
    assert MatchReasonCode.TRACK_MAPPING_PARTIAL in reassigned.reason_codes


def test_confirming_an_existing_weak_pair_marks_human_confirmation_without_changing_other_pairs() -> None:
    files, candidate, before = inputs()
    weak = replace(before.mappings[0], classification=MatchClassification.REVIEW, score=65.0)
    before = replace(before, mappings=(weak,))
    changed = set_manual_track_assignment(
        files, candidate, before, local_file_id=files[0].file_id, provider_track_index=0,
    )

    assert changed.mappings[0].classification is MatchClassification.HIGH
    assert changed.mappings[0].score == 0.0
    assert changed.mappings[0].reason_codes == ("MANUAL_TRACK_ASSIGNMENT",)
    assert changed.unmatched_local_file_ids == before.unmatched_local_file_ids
    assert weak.classification is MatchClassification.REVIEW


def test_manual_mapping_order_uses_the_same_local_number_policy_as_automatic_mapping() -> None:
    files, candidate, before = inputs()
    reordered = set_manual_track_assignment(
        tuple(reversed(files)), candidate, before, local_file_id=files[1].file_id, provider_track_index=1,
    )
    ordinary = set_manual_track_assignment(
        files, candidate, before, local_file_id=files[1].file_id, provider_track_index=1,
    )

    assert reordered == ordinary


def test_review_builder_uses_manual_track_then_removes_its_proposals_when_unmapped() -> None:
    files, candidate, before = inputs()
    chosen = set_manual_track_assignment(
        files, candidate, before, local_file_id=files[1].file_id, provider_track_index=1,
    )
    selected = CoordinatedCandidate(
        candidate,
        (MetadataProvenance("musicbrainz", "musicbrainz", "release", None, None, "LOOKUP-0001"),),
    )
    results = build_selected_file_results(
        files, selected, medium_index=1, mapping_result=chosen,
        release_classification=MatchClassification.HIGH, preferred_language="auto",
    )

    assert results[1].track_mapping_resolved is True
    assert results[1].change_set.final_metadata.title == "Finale"
    assert results[1].change_set.final_metadata.composers == ("Composer",)
    assert results[1].change_set.final_metadata.track == Position(2)
    assert next(item for item in results[1].proposals if item.field is MetadataField.TRACK).value == Position(2, 2)
    assert results[1].change_set.status is not ChangeSetStatus.BLOCKED

    cleared = set_manual_track_assignment(
        files, candidate, chosen, local_file_id=files[1].file_id, provider_track_index=None,
    )
    results = build_selected_file_results(
        files, selected, medium_index=1, mapping_result=cleared,
        release_classification=MatchClassification.HIGH, preferred_language="auto",
    )
    assert results[1].track_mapping_resolved is False
    assert results[1].change_set.final_metadata.title is None
    assert not any(item.field is MetadataField.TITLE for item in results[1].proposals)


@pytest.mark.parametrize("provider_index", [-1, 2, 9])
def test_provider_index_must_belong_to_the_selected_medium(provider_index: int) -> None:
    files, candidate, before = inputs()

    with pytest.raises(ValueError):
        set_manual_track_assignment(
            files, candidate, before, local_file_id=files[1].file_id, provider_track_index=provider_index,
        )


@pytest.mark.parametrize("provider_index", [True, "1", 1.5])
def test_provider_index_rejects_non_integer_values(provider_index: object) -> None:
    files, candidate, before = inputs()

    with pytest.raises(TypeError):
        set_manual_track_assignment(
            files, candidate, before, local_file_id=files[1].file_id,
            provider_track_index=cast(int, provider_index),
        )


@pytest.mark.parametrize("invalid_state", ["local_partition", "provider_partition", "medium", "number", "position"])
def test_manual_mapping_rejects_stale_or_incomplete_input(invalid_state: str) -> None:
    files, candidate, before = inputs()

    if invalid_state == "local_partition":
        before = replace(before, unmatched_local_file_ids=())
    elif invalid_state == "provider_partition":
        before = replace(before, unmatched_provider_indexes=(7,))
    elif invalid_state == "medium":
        before = replace(before, selected_medium_index=9)
    elif invalid_state == "number":
        before = replace(before, selected_medium_number=1)
    else:
        before = replace(before, mappings=(replace(before.mappings[0], track_position=Position(9, 2)),))

    with pytest.raises(ValueError):
        set_manual_track_assignment(
            files, candidate, before, local_file_id=files[1].file_id, provider_track_index=1,
        )


def test_manual_mapping_rejects_unknown_file_and_duplicate_local_input() -> None:
    files, candidate, before = inputs()

    with pytest.raises(ValueError):
        set_manual_track_assignment(files, candidate, before, local_file_id="unknown", provider_track_index=1)

    with pytest.raises(ValueError):
        set_manual_track_assignment(
            (files[0], files[0]), candidate, before, local_file_id=files[0].file_id, provider_track_index=1,
        )
