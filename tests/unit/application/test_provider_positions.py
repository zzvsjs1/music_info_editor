"""Source completeness and printed numbering constrain automatic position proposals."""

# Provider list length is not automatically an authoritative total. These cases
# exercise incomplete listings, printed numbers and preservation of known components.


from dataclasses import replace
from pathlib import Path

import pytest

from metadata_polisher.application.review import build_selected_file_results, set_manual_track_assignment
from metadata_polisher.domain.matching import MetadataProvenance
from metadata_polisher.domain.metadata import FieldReadState, MetadataField, Position
from metadata_polisher.matching.release_scoring import (
    LocalReleaseEvidence,
    LocalTrackEvidence,
    MatchClassification,
    MatchReasonCode,
    score_release_medium,
)
from metadata_polisher.matching.track_mapping import map_tracks
from metadata_polisher.providers.coordinator import CoordinatedCandidate
from metadata_polisher.providers.musicbrainz.parser import parse_release_detail
from metadata_polisher.providers.vgmdb.parser import parse_album_detail
from tests.unit.matching.test_track_mapping import make_local


def _track(position: int | None, title: str, printed: str | None = None) -> dict[str, object]:
    return {"position": position, "number": printed, "title": title, "length": 180000}


def _release(medium: dict[str, object]) -> dict[str, object]:
    return {"id": "fixture-release", "title": "Album", "media": [medium]}


def _mapped_positions(payload: dict[str, object]):
    # A single local file can match a larger medium. The provider listing, rather
    # than the local folder size, must determine whether its total is trustworthy.
    candidate = parse_release_detail(payload)
    local = (make_local("first", title="Opening", tagged_number=None),)
    mapping = map_tracks(local, candidate, selected_medium_index=0)
    assert len(mapping.mappings) == 1
    return candidate, local, mapping


@pytest.mark.parametrize(
    "medium",
    [
        {"position": 1, "track-count": 3, "track-offset": 0, "tracks": [_track(1, "Opening")]},
        {"position": 1, "track-count": 1, "track-offset": 2, "tracks": [_track(3, "Opening")]},
        {"position": 1, "tracks": [_track(1, "Opening")]},
    ],
)
def test_partial_or_undeclared_provider_tracks_do_not_become_authoritative_totals(medium) -> None:
    candidate, local, mapping = _mapped_positions(_release(medium))

    assert mapping.mappings[0].track_position.total is None
    updated = set_manual_track_assignment(
        local, candidate, mapping, local_file_id="first", provider_track_index=0,
    )
    assert updated.mappings[0].track_position.total is None


def test_complete_declared_provider_listing_supplies_total_despite_partial_local_folder() -> None:
    payload = _release({
        "position": 1, "track-count": 2, "track-offset": 0,
        "tracks": [_track(1, "Opening", "1"), _track(2, "Finale", "2")],
    })
    _, _, mapping = _mapped_positions(payload)

    assert mapping.mappings[0].track_position == Position(1, 2)
    assert mapping.mappings[0].disc_position == Position(1, 1)


@pytest.mark.parametrize("member", [None, "damaged", 12, []])
def test_malformed_track_members_cannot_silently_shrink_a_medium(member) -> None:
    payload = _release({"position": 1, "track-count": 2, "tracks": [_track(1, "Opening"), member]})

    with pytest.raises(ValueError, match="MusicBrainz.*tracks"):
        parse_release_detail(payload)


@pytest.mark.parametrize("members", [[None], [{"position": 1, "tracks": []}, None], "damaged"])
def test_malformed_media_arrays_cannot_silently_shrink_disc_totals(members) -> None:
    with pytest.raises(ValueError, match="MusicBrainz.*media"):
        parse_release_detail({"id": "fixture-release", "title": "Album", "media": members})


def test_non_contiguous_media_numbers_do_not_invent_a_disc_total() -> None:
    payload = _release({"position": 2, "track-count": 1, "tracks": [_track(1, "Opening")]})
    _, _, mapping = _mapped_positions(payload)

    assert mapping.mappings[0].disc_position == Position(2, None)


@pytest.mark.parametrize("printed", ["A1", "B2", "3"])
def test_ambiguous_printed_number_is_retained_without_automatic_position(printed: str) -> None:
    candidate, _, mapping = _mapped_positions(_release({
        "position": 1, "track-count": 1, "tracks": [_track(1, "Opening", printed)],
    }))

    assert candidate.media[0].tracks[0].printed_number == printed
    assert mapping.mappings[0].track_position == Position()


def test_pregap_zero_is_retained_and_mixed_media_do_not_fabricate_audio_totals() -> None:
    candidate = parse_release_detail(_release({
        "position": 1, "track-count": 1, "track-offset": 0,
        "pregap": _track(0, "Hidden opening", "0"),
        "tracks": [_track(1, "Opening", "1")],
        "data-tracks": [_track(2, "Computer content", "2")],
    }))

    assert [track.track_number for track in candidate.media[0].tracks] == [0, 1]
    assert candidate.media[0].has_data_tracks
    assert candidate.media[0].track_total is None
    local = (make_local("pregap", title="Hidden opening", tagged_number=None),)
    mapping = map_tracks(local, candidate, selected_medium_index=0)
    assert mapping.mappings[0].track_position == Position()


def test_no_position_evidence_produces_no_position_proposal() -> None:
    candidate, local, mapping = _mapped_positions(_release({"tracks": [_track(None, "Opening")]}))
    provenance = MetadataProvenance(candidate.engine_id, candidate.source_id, candidate.release_id,
                                    candidate.source_url, None, "LOOKUP-POSITIONS")
    reviewed = build_selected_file_results(
        local, CoordinatedCandidate(candidate, (provenance,)), medium_index=0, mapping_result=mapping,
        release_classification=MatchClassification.HIGH, preferred_language="auto",
    )

    assert not any(item.field in {MetadataField.TRACK, MetadataField.DISC} for item in reviewed[0].proposals)


def test_unsubstantiated_mapping_total_is_rejected_before_building_proposals() -> None:
    candidate, local, mapping = _mapped_positions(_release({"position": 1, "tracks": [_track(1, "Opening")]}))
    forged = replace(mapping, mappings=(replace(mapping.mappings[0], track_position=Position(1, 99)),))
    provenance = MetadataProvenance(candidate.engine_id, candidate.source_id, candidate.release_id,
                                    candidate.source_url, None, "LOOKUP-POSITIONS")

    with pytest.raises(ValueError, match="positions"):
        build_selected_file_results(
            local, CoordinatedCandidate(candidate, (provenance,)), medium_index=0, mapping_result=forged,
            release_classification=MatchClassification.HIGH, preferred_language="auto",
        )


def test_absent_provider_total_preserves_the_supported_existing_total() -> None:
    candidate, local, mapping = _mapped_positions(_release({"position": 1, "tracks": [_track(1, "Opening")]}))
    source = local[0]
    local = (replace(source, read_result=replace(
        source.read_result,
        metadata=replace(source.read_result.metadata, track=Position(1, 12)),
        field_states={**source.read_result.field_states, MetadataField.TRACK: FieldReadState.PRESENT},
    )),)
    provenance = MetadataProvenance(candidate.engine_id, candidate.source_id, candidate.release_id,
                                    candidate.source_url, None, "LOOKUP-POSITIONS")
    reviewed = build_selected_file_results(
        local, CoordinatedCandidate(candidate, (provenance,)), medium_index=0, mapping_result=mapping,
        release_classification=MatchClassification.HIGH, preferred_language="auto",
    )

    assert next(item.value for item in reviewed[0].proposals if item.field is MetadataField.TRACK) == Position(1, 12)


def test_vgmdb_html_without_a_verified_count_contract_keeps_totals_unknown() -> None:
    fixture = Path(__file__).parents[2] / "fixtures" / "providers" / "vgmdb" / "album_detail.html"
    candidate = parse_album_detail(fixture.read_text(encoding="utf-8"), source_url="https://vgmdb.net/album/4242")
    local = (make_local("first", title="Opening & Dawn", tagged_number=1, duration=181.0),)
    mapping = map_tracks(local, candidate, selected_medium_index=0)

    assert mapping.mappings[0].track_position == Position(1, None)
    assert mapping.mappings[0].disc_position == Position(1, None)


def test_incomplete_source_count_does_not_earn_exact_count_or_full_coverage_confidence() -> None:
    candidate = parse_release_detail(_release({
        "position": 1, "track-count": 3, "track-offset": 0, "tracks": [_track(1, "Opening")],
    }))
    local = LocalReleaseEvidence(
        album_title="Album", artists=(), year=None, disc_number=1, disc_total=1,
        tracks=(LocalTrackEvidence(title="Opening", duration_seconds=180.0),),
    )
    score = score_release_medium(local, candidate, candidate.media[0])
    codes = {item.code for item in score.evidence}

    assert MatchReasonCode.TRACK_COUNT_EXACT not in codes
    assert MatchReasonCode.TRACK_COUNT_UNAVAILABLE in codes
    assert score.classification is MatchClassification.REVIEW
    assert "PROVIDER_LIST_INCOMPLETE" in codes


def test_unknown_source_disc_total_does_not_create_a_scoring_contradiction() -> None:
    candidate = parse_release_detail(_release({
        "position": 2, "track-count": 1, "tracks": [_track(1, "Opening")],
    }))
    local = LocalReleaseEvidence(
        album_title="Album", artists=(), year=None, disc_number=2, disc_total=8,
        tracks=(LocalTrackEvidence(title="Opening", duration_seconds=180.0),),
    )
    score = score_release_medium(local, candidate, candidate.media[0])
    disc = next(item for item in score.evidence if item.code in {
        MatchReasonCode.DISC_EXACT, MatchReasonCode.DISC_CONTRADICTION,
    })

    assert disc.code is MatchReasonCode.DISC_EXACT
    assert "disc total" not in disc.detail


def test_vgmdb_unnumbered_rows_do_not_merge_with_a_later_numeric_track() -> None:
    html = (
        '<h1><span class="albumtitle">Album</span></h1>'
        '<div id="tracklist"><span><span>Disc 1</span><table class="role">'
        '<tr><td>A1</td><td>Printed label</td><td class="time">3:00</td></tr>'
        '<tr><td>1</td><td>Numeric label</td><td class="time">4:00</td></tr>'
        '</table></span></div>'
    )
    candidate = parse_album_detail(html, source_url="https://vgmdb.net/album/4242")
    tracks = candidate.media[0].tracks

    assert len(tracks) == 2
    assert tracks[0].printed_number == "A1"
    assert tracks[0].track_number is None
    assert [track.titles[0].value for track in tracks] == ["Printed label", "Numeric label"]
    assert candidate.media[0].track_position(0) == Position()
    assert candidate.media[0].track_position(1) == Position(1, None)
