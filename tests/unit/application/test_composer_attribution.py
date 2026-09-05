"""Track proposals require attributable credits, independently of album matching."""

# Deliberately strong release matches isolate the independent attribution rule:
# an album credit still needs track/work scope before it can become a track proposal.


import json
from dataclasses import replace
from pathlib import Path

import pytest

from metadata_polisher.application.review import build_selected_file_results, set_manual_decision
from metadata_polisher.domain import matching
from metadata_polisher.domain.matching import (
    LocalisedText,
    MetadataProvenance,
    ProviderTrack,
    ReleaseCandidate,
    ReleaseMedium,
)
from metadata_polisher.domain.metadata import MetadataField, MetadataSnapshot, Position
from metadata_polisher.domain.review import DecisionOrigin, FieldConfidence, FieldDecisionKind
from metadata_polisher.infrastructure.settings import RenameSettings
from metadata_polisher.matching.release_scoring import MatchClassification
from metadata_polisher.matching.track_mapping import map_tracks
from metadata_polisher.providers.coordinator import CoordinatedCandidate
from metadata_polisher.providers.musicbrainz.parser import parse_release_detail
from metadata_polisher.providers.vgmdb.parser import parse_album_detail
from metadata_polisher.session.review_editing import accept_safe_additions
from tests.unit.session.test_review_editing import make_selected_session, make_source

FIXTURES = Path(__file__).parents[2] / "fixtures" / "providers"


def _album_html(names: tuple[str, ...]) -> str:
    links = ", ".join(f'<a href="/artist/{index}"><span class="artistname" lang="en">{name}</span></a>'
                      for index, name in enumerate(names, 1))

    return (
        '<h1><span class="albumtitle" lang="en">Album</span></h1>'
        f'<div id="collapse_credits"><table><tr><td>Composer</td><td>{links}</td></tr></table></div>'
        '<div id="tracklist"><span><span>Disc 1</span><table class="role">'
        '<tr><td>1</td><td>Overture</td><td class="time">3:00</td></tr>'
        '<tr><td>2</td><td>Finale</td><td class="time">3:00</td></tr>'
        '</table></span></div>'
    )


def _review(candidate: ReleaseCandidate):
    # Force high mapping confidence so any rejected composer proposal is explained
    # by credit scope, not by an unrelated weak release or track score.
    files = tuple(make_source(f"{index}.flac", metadata=MetadataSnapshot(
        title=track.titles[0].value, album="Album", track=Position(index),
    )) for index, track in enumerate(candidate.media[0].tracks, 1))
    provenance = MetadataProvenance(candidate.engine_id, candidate.source_id, candidate.release_id,
                                    candidate.source_url, None, "LOOKUP-COMPOSER")
    mapped = map_tracks(files, candidate, selected_medium_index=0)
    mapped = replace(mapped, mappings=tuple(replace(item, classification=MatchClassification.HIGH)
                                           for item in mapped.mappings))

    return files, build_selected_file_results(
        files, CoordinatedCandidate(candidate, (provenance,)), medium_index=0,
        mapping_result=mapped, release_classification=MatchClassification.HIGH, preferred_language="auto",
    )


def _candidate(tracks: tuple[ProviderTrack, ...]) -> ReleaseCandidate:
    return ReleaseCandidate("fixture", "fixture", "release", (LocalisedText("Album", None, None),),
                            (), None, (ReleaseMedium(1, None, tracks),), "https://catalogue.invalid/release")


def _track(composers: tuple[str, ...] = ()) -> ProviderTrack:
    return ProviderTrack(1, (LocalisedText("Overture", None, None),), (), composers, 180.0)


@pytest.mark.parametrize("names", [("Only Album Composer",), ("First Composer", "Second Composer")])
def test_album_only_credits_never_become_track_credits_even_for_one_name(names: tuple[str, ...]) -> None:
    candidate = parse_album_detail(_album_html(names), source_url="https://vgmdb.net/album/4242")

    assert all(track.composers == () for medium in candidate.media for track in medium.tracks)
    assert tuple(name for credit in candidate.album_credits for name in credit.names) == names
    assert all(credit.scope is matching.CreditScope.ALBUM for credit in candidate.album_credits)


@pytest.mark.parametrize("existing", [(), ("Already on disk",)])
def test_album_only_credits_are_skipped_by_safe_additions_and_existing_values_are_not_erased(
    existing: tuple[str, ...],
) -> None:
    candidate = parse_album_detail(_album_html(("Album Composer",)), source_url="https://vgmdb.net/album/4242")
    _, results = _review(candidate)
    state = make_selected_session(results[0].proposals, metadata=MetadataSnapshot(
        title="Overture", album="Album", track=Position(1), composers=existing,
    ))
    changed = accept_safe_additions(state, "album", RenameSettings())

    assert all(item.change_set.final_metadata.composers == existing for item in changed.groups[0].reviewed_files)
    assert all(proposal.field is not MetadataField.COMPOSERS for item in results for proposal in item.proposals)


def test_unattributed_track_tuple_is_not_provider_confirmed_evidence() -> None:
    _, results = _review(_candidate((_track(("Unscoped Composer",)),)))

    assert all(proposal.field is not MetadataField.COMPOSERS for proposal in results[0].proposals)


@pytest.mark.parametrize("scope", ["TRACK", "WORK", "ALL_TRACKS"])
def test_explicit_applicable_credit_retains_scope_and_independent_confidence(scope: str) -> None:
    credit = matching.ComposerCredit(
        names=("Track Composer",), scope=getattr(matching.CreditScope, scope),
        confidence=matching.CreditConfidence.REVIEW, record_id="work-or-track-1",
        source_url="https://catalogue.invalid/credit/1",
    )
    track = replace(_track(("Track Composer",)), composer_credits=(credit,))
    candidate = _candidate((track,))

    if scope == "ALL_TRACKS":
        candidate = replace(_candidate((_track(),)), album_credits=(credit,))

    _, results = _review(candidate)
    proposal = next(item for item in results[0].proposals if item.field is MetadataField.COMPOSERS)

    assert proposal.value == ("Track Composer",)
    assert proposal.confidence is FieldConfidence.REVIEW
    assert proposal.credit_evidence == (credit,)
    review = next(item for item in results[0].reviews if item.field is MetadataField.COMPOSERS)
    assert review.decision is FieldDecisionKind.UNRESOLVED


def test_different_explicit_track_assignments_remain_distinct_and_exclude_arrangers() -> None:
    first = matching.ComposerCredit(("First Composer",), matching.CreditScope.TRACK)
    second = matching.ComposerCredit(("Second Composer",), matching.CreditScope.TRACK)
    arranger = matching.ComposerCredit(("An Arranger",), matching.CreditScope.TRACK, role="arranger")
    tracks = (
        replace(_track(), composer_credits=(first, arranger)),
        replace(_track(), track_number=2, composer_credits=(second,)),
    )
    _, results = _review(_candidate(tracks))

    values = [next(item.value for item in result.proposals if item.field is MetadataField.COMPOSERS)
              for result in results]
    assert values == [("First Composer",), ("Second Composer",)]


def test_high_confidence_explicit_credit_can_be_accepted_as_a_safe_addition() -> None:
    credit = matching.ComposerCredit(("Known Composer",), matching.CreditScope.TRACK)
    candidate = _candidate((replace(_track(), composer_credits=(credit,)),))
    _, results = _review(candidate)
    state = make_selected_session(results[0].proposals)
    changed = accept_safe_additions(state, "album", RenameSettings())
    reviewed = changed.groups[0].reviewed_files[0]
    composer = next(item for item in reviewed.reviews if item.field is MetadataField.COMPOSERS)

    assert reviewed.change_set.final_metadata.composers == ("Known Composer",)
    assert composer.decision_origin is DecisionOrigin.USER
    assert composer.selected_proposal.members[0].credit_evidence == (credit,)


def test_musicbrainz_work_relationship_is_preserved_as_credit_evidence() -> None:
    payload = json.loads((FIXTURES / "musicbrainz" / "release_detail.json").read_text(encoding="utf-8"))
    candidate = parse_release_detail(payload)
    first = candidate.media[0].tracks[0]

    assert first.composers == ("K. Kondo",)
    assert len(first.composer_credits) == 1
    assert first.composer_credits[0].scope is matching.CreditScope.WORK
    assert first.composer_credits[0].record_id == "44444444-4444-4444-8444-444444444441"
    assert first.composer_credits[0].role == "composer"
    assert candidate.media[1].tracks[0].composer_credits == ()


def test_deliberate_common_composer_is_manual_and_keeps_provider_evidence_separate() -> None:
    candidate = parse_album_detail(_album_html(("Album Composer",)), source_url="https://vgmdb.net/album/4242")
    _, results = _review(candidate)
    composer = next(item for item in results[0].reviews if item.field is MetadataField.COMPOSERS)
    edited = set_manual_decision(composer, ("My reviewed common composer",))

    assert edited.manual_value == ("My reviewed common composer",)
    assert edited.decision_origin is DecisionOrigin.USER
    assert edited.decision is FieldDecisionKind.USE_MANUAL
    assert edited.proposals == ()
