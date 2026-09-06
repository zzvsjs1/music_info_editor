"""Identity uncertainty must survive the actual lookup-to-review pipeline.

Only the network boundary is replaced. Real immutable models, ranking, mapping,
proposal decisions and ChangeSets expose accidental confidence promotion here.
No writer is constructed, so these checks never submit a disk transaction.
"""

from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from metadata_polisher.application.lookup import LookupService
from metadata_polisher.application.review import set_manual_track_assignment
from metadata_polisher.domain.matching import LocalisedText, ProviderTrack, ReleaseCandidate, ReleaseMedium
from metadata_polisher.domain.media import FilenameHints, LocalMediaFile, MediaReadResult, StreamInfo
from metadata_polisher.domain.metadata import FieldReadState, MetadataField, MetadataSnapshot, Position
from metadata_polisher.domain.review import FieldConfidence, FieldDecisionKind
from metadata_polisher.matching.release_scoring import MatchClassification, MatchReasonCode
from metadata_polisher.matching.track_mapping import map_tracks
from metadata_polisher.providers.base import ProviderCapabilities, RequestContext
from metadata_polisher.providers.coordinator import ProviderCoordinator
from metadata_polisher.scanner.grouping import AlbumGroup, GroupingReason


@dataclass
class NativeFixtureProvider:
    """Replace only the network boundary, retaining real domain and service code."""

    candidate: ReleaseCandidate
    engine_id: str = "fixture"

    def capabilities(self):
        return ProviderCapabilities(True, True, False, True)

    def search_releases(self, _query, _context):
        return (self.candidate,)

    def load_release_media(self, candidate, _context):
        return candidate

    def enrich_release(self, candidate, _context):
        return candidate


def local(index, title, duration, *, number=None):
    metadata = MetadataSnapshot(
        title=title, album="Album", album_artists=("Example Artist",),
        track=Position(number), disc=Position(1), date="2020",
    )
    states = {field: FieldReadState.MISSING for field in MetadataField}

    for field in (MetadataField.ALBUM, MetadataField.ALBUM_ARTISTS, MetadataField.DISC, MetadataField.DATE):
        states[field] = FieldReadState.PRESENT

    if title is not None:
        states[MetadataField.TITLE] = FieldReadState.PRESENT

    if number is not None:
        states[MetadataField.TRACK] = FieldReadState.PRESENT

    return LocalMediaFile(
        Path("synthetic") / f"local-{index}.flac", "flac",
        MediaReadResult(metadata, states, StreamInfo(duration, 44100, 2, 16, "FLAC")),
        FilenameHints(), file_id=f"local-{index}",
    )


def release(titles, durations):
    tracks = tuple(
        ProviderTrack(
            index, (LocalisedText(title, "en", "Latn"),), (f"Performer {index}",), (), duration,
        )
        for index, (title, duration) in enumerate(zip(titles, durations, strict=True), start=1)
    )
    return ReleaseCandidate(
        "fixture", "fixture", "release", (LocalisedText("Album", "en", "Latn"),),
        ("Example Artist",), "2020", (ReleaseMedium(1, None, tracks),), None,
    )


def select(files, candidate):
    group = AlbumGroup("group", tuple(files), "Album", GroupingReason.DIRECTORY_ALBUM_CONSISTENT)
    context = RequestContext("review-reproduction", "auto")
    service = LookupService(ProviderCoordinator((NativeFixtureProvider(candidate),)))
    lookup = service.search_and_rank_group(group, context)
    selected = service.select_candidate_metadata(group, lookup, lookup.release_ranking.identities[0], context)
    return lookup, selected


def repeated_fixture(finding):
    files = (local(1, "Interlude", 60), local(2, "Interlude", 60))

    if finding == "R1":
        candidate = release(("Interlude", "Interlude"), (60, None))
    else:
        candidate = release(("Interlude", "XYZ"), (60, 60))

    return files, candidate


@pytest.mark.parametrize("finding,score", [("R1", 100), ("R2", 85)])
def test_ambiguous_identity_cannot_default_select_a_missing_performer(finding, score):
    files, candidate = repeated_fixture(finding)
    lookup, selected = select(files, candidate)

    assert lookup.release_ranking.entries[0].result.score == score
    assert lookup.release_ranking.entries[0].result.classification is MatchClassification.HIGH
    assert selected.track_mapping.classification is not MatchClassification.HIGH
    assert len(selected.reviewed_files) == len(files)

    # Either an unresolved association or a retained REVIEW suggestion is safe.
    # A group-level warning alone is insufficient: the field consumer uses the
    # individual pair confidence when selecting a missing value by default.
    for reviewed in selected.reviewed_files:
        artist = next(review for review in reviewed.reviews if review.field is MetadataField.ARTISTS)

        assert artist.decision is not FieldDecisionKind.USE_PROPOSAL
        assert reviewed.change_set.final_metadata.artists == ()
        assert all(proposal.confidence is not FieldConfidence.HIGH for proposal in artist.proposals)

    # The source snapshots remain untouched. This service creates in-memory
    # review state; disk submission belongs to the separate confirmed Apply path.
    assert all(file.read_result.metadata.artists == () for file in files)


def test_number_only_pair_requires_review_even_when_other_tracks_establish_the_release():
    titles = ("Opening", "Middle", "Finale", "Encore")
    files = tuple(local(index, title, 60, number=index) for index, title in enumerate(titles[:3], start=1))
    files += (local(4, None, None, number=4),)
    lookup, selected = select(files, release(titles, (60, 60, 60, 60)))

    assert lookup.release_ranking.entries[0].result.classification is MatchClassification.HIGH
    pair = next(mapping for mapping in selected.track_mapping.mappings if mapping.local_file_id == "local-4")
    assert pair.classification is MatchClassification.REVIEW
    assert pair.score == 100
    title = next(review for review in selected.reviewed_files[3].reviews if review.field is MetadataField.TITLE)
    assert title.decision is FieldDecisionKind.UNRESOLVED
    assert title.requires_review
    assert selected.reviewed_files[3].change_set.final_metadata.title is None


@pytest.mark.parametrize("tracks_complete,media_complete", [(False, True), (True, False), (False, False)])
def test_incomplete_listing_release_cap_protects_default_field_choices(tracks_complete, media_complete):
    candidate = release(("Opening", "Finale"), (60, 120))
    candidate = replace(
        candidate, media=(replace(candidate.media[0], tracks_complete=tracks_complete),),
        media_complete=media_complete,
    )
    files = (local(1, "Opening", 60, number=1), local(2, "Finale", 120, number=2))
    lookup, selected = select(files, candidate)

    assert lookup.release_ranking.entries[0].result.classification is MatchClassification.REVIEW
    assert selected.track_mapping.classification is MatchClassification.REVIEW
    assert MatchReasonCode.PROVIDER_LIST_INCOMPLETE in selected.track_mapping.reason_codes
    assert all(pair.classification is MatchClassification.HIGH for pair in selected.track_mapping.mappings)

    for reviewed in selected.reviewed_files:
        artist = next(review for review in reviewed.reviews if review.field is MetadataField.ARTISTS)
        assert artist.proposals[0].confidence is FieldConfidence.REVIEW
        assert artist.decision is FieldDecisionKind.UNRESOLVED
        assert artist.requires_review

    assert candidate.media[0].track_total == (2 if tracks_complete else None)
    assert candidate.disc_total == (1 if media_complete else None)


@pytest.mark.parametrize("missing_number", [1, 5, 10])
def test_correct_numbered_partial_album_reaches_mapping_without_shifted_duration_conflicts(missing_number):
    titles = tuple("ABCDEFGHIJ")
    durations = tuple(100 + 20 * number for number in range(1, 11))
    candidate = release(titles, durations)
    files = tuple(
        local(number, title, duration, number=number)
        for number, (title, duration) in enumerate(zip(titles, durations, strict=True), start=1)
        if number != missing_number
    )
    lookup, selected = select(files, candidate)
    ranked = lookup.release_ranking.entries[0].result
    reasons = tuple(item.code for item in ranked.evidence)

    # A missing track must still request review, but it cannot make B's real
    # duration contradict A's duration merely by shifting positional zipping.
    assert ranked.classification is MatchClassification.REVIEW
    assert MatchReasonCode.TRACK_COUNT_CONTRADICTION in reasons
    assert MatchReasonCode.DURATION_LARGE_MISMATCH not in reasons
    assert selected.track_mapping.classification is MatchClassification.REVIEW
    assert len(selected.track_mapping.mappings) == 9
    assert selected.track_mapping.unmatched_provider_indexes == (missing_number - 1,)
    assert selected.track_mapping.unmatched_local_file_ids == ()
    assert all(pair.classification is MatchClassification.HIGH for pair in selected.track_mapping.mappings)


@pytest.mark.parametrize("tracks_complete,media_complete", [(False, True), (True, False), (False, False)])
def test_manual_pair_confirmation_does_not_claim_the_provider_listing_is_complete(tracks_complete, media_complete):
    candidate = release(("Opening", "Finale"), (60, 120))
    candidate = replace(
        candidate, media=(replace(candidate.media[0], tracks_complete=tracks_complete),),
        media_complete=media_complete,
    )
    files = (local(1, "Opening", 60, number=1), local(2, "Finale", 120, number=2))
    mapping = map_tracks(files, candidate, selected_medium_index=0)
    confirmed = set_manual_track_assignment(
        files, candidate, mapping, local_file_id=files[0].file_id, provider_track_index=0,
    )

    # Human confirmation resolves the chosen association. It says nothing about
    # rows omitted by the catalogue, so the independent listing warning survives.
    assert confirmed.mappings[0].classification is MatchClassification.HIGH
    assert confirmed.mappings[0].reason_codes == (MatchReasonCode.MANUAL_TRACK_ASSIGNMENT,)
    assert confirmed.classification is MatchClassification.REVIEW
    assert confirmed.reason_codes.count(MatchReasonCode.PROVIDER_LIST_INCOMPLETE) == 1
