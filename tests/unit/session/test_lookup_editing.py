# Loaded multilingual variants allow preference changes without a provider call.
# Search-term changes instead invalidate evidence tied to the previous query.

from dataclasses import replace
from pathlib import Path

import pytest

from metadata_polisher.application.changes import RenameDecision, build_change_set
from metadata_polisher.application.lookup import CandidateLookupResult, LookupSearchResult, SelectedMetadataResult
from metadata_polisher.application.review import (
    build_selected_file_results,
    set_clear_decision,
    set_keep_existing_decision,
    set_manual_decision,
    set_proposal_decision,
)
from metadata_polisher.domain.matching import (
    LocalisedText,
    MetadataProvenance,
    ProviderTrack,
    ReleaseCandidate,
    ReleaseMedium,
    ReleaseSearchQuery,
)
from metadata_polisher.domain.media import LocalMediaFile, MediaReadResult, StreamInfo, UnsupportedMediaFile
from metadata_polisher.domain.metadata import FieldReadState, MetadataField, MetadataSnapshot, Position
from metadata_polisher.domain.review import DecisionOrigin, FieldReviewState, ReviewReasonCode
from metadata_polisher.infrastructure.settings import RenameSettings
from metadata_polisher.matching.release_scoring import build_local_release_evidence, rank_release_candidates
from metadata_polisher.matching.track_mapping import map_tracks
from metadata_polisher.providers.coordinator import CoordinatedCandidate, ProviderCoordinator
from metadata_polisher.scanner.grouping import AlbumGroup, GroupingReason
from metadata_polisher.session.lookup_editing import (
    change_language,
    incomplete_searchable_group_ids,
    set_search_query_override,
)
from metadata_polisher.session.state import (
    GroupSelection,
    GroupState,
    ReleaseSelectionState,
    ReviewedFileState,
    SessionState,
)


def make_query(album: str = "冒険のアルバム") -> ReleaseSearchQuery:
    return ReleaseSearchQuery(album, (), None, None, 1, ("序曲",))


def make_selected_session() -> SessionState:
    metadata = MetadataSnapshot(title="序曲", album="冒険のアルバム", track=Position(number=1))
    states = {field: FieldReadState.MISSING for field in MetadataField}

    for field in (MetadataField.TITLE, MetadataField.ALBUM, MetadataField.TRACK):
        states[field] = FieldReadState.PRESENT

    source = LocalMediaFile(
        path=Path("library/Album/01.flac"),
        format_id="flac",
        read_result=MediaReadResult(metadata, states, StreamInfo(180.0, 48_000, 2, 24, "FLAC")),
    )
    group = AlbumGroup("album", (source,), metadata.album, GroupingReason.DIRECTORY_ALBUM_CONSISTENT)
    candidate = ReleaseCandidate(
        engine_id="vgmdb",
        source_id="vgmdb",
        release_id="123",
        titles=(LocalisedText("冒険のアルバム", "ja", "Jpan"),),
        album_artists=(),
        date=None,
        media=(
            ReleaseMedium(
                medium_number=1,
                title=None,
                tracks=(
                    ProviderTrack(
                        track_number=1,
                        titles=(LocalisedText("序曲", "ja", "Jpan"), LocalisedText("Overture", "en", "Latn")),
                        artists=(),
                        composers=(),
                        duration_seconds=180.0,
                    ),
                ),
            ),
        ),
        source_url="https://vgmdb.net/album/123",
    )
    provenance = MetadataProvenance("vgmdb", "vgmdb", "123", candidate.source_url, None, "LOOKUP-0001")
    coordinated = CoordinatedCandidate(candidate, (provenance,))
    lookup = LookupSearchResult(group.group_id, (make_query(),), (coordinated,), ())
    ranking = rank_release_candidates(build_local_release_evidence(group), (candidate,))
    candidates = CandidateLookupResult(lookup, ranking, (), ())
    mapping = map_tracks(group.files, candidate, selected_medium_index=0)
    reviewed = build_selected_file_results(
        group.files,
        coordinated,
        medium_index=0,
        mapping_result=mapping,
        release_classification=ranking.entries[0].result.classification,
        preferred_language="auto",
    )
    selected = SelectedMetadataResult(candidates, ranking.identities[0], coordinated, mapping, reviewed, ())
    group_state = GroupState(
        group=group,
        lookup_result=lookup,
        release_ranking=ranking,
        candidate_lookup=candidates,
        selected_release=ReleaseSelectionState(coordinated, 0),
        automatic_track_mapping=mapping,
        reviewed_files=tuple(
            ReviewedFileState(item.file_id, item.proposals, item.reviews, item.track_mapping_resolved, item.change_set)
            for item in reviewed
        ),
        selected_metadata=selected,
    )

    return SessionState(root=Path("library"), groups=(group_state,), selection=GroupSelection("album"))


def title_review(state: SessionState) -> FieldReviewState:
    return next(review for review in state.groups[0].reviewed_files[0].reviews if review.field is MetadataField.TITLE)


def test_language_override_reranks_existing_variants_and_rebuilds_changes_without_provider_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = make_selected_session()
    before = state.groups[0]
    old_change = before.reviewed_files[0].change_set

    def unexpected_provider_call(*_args: object, **_kwargs: object) -> None:
        pytest.fail("A session language edit must reuse the loaded provider evidence")

    monkeypatch.setattr(ProviderCoordinator, "search_release_queries", unexpected_provider_call)
    monkeypatch.setattr(ProviderCoordinator, "enrich_release", unexpected_provider_call)
    assert title_review(state).proposals[0].value == "序曲"

    rename_settings = RenameSettings(template="%tracknumber% - %title%", minimum_track_digits=3)
    changed = change_language(state, "album", "en", rename_settings)

    assert title_review(changed).proposals[0].value == "Overture"
    assert ReviewReasonCode.LANGUAGE_OVERRIDE in title_review(changed).reason_codes
    assert changed.groups[0].language_override == "en"
    assert changed.groups[0].reviewed_files[0].proposals == before.reviewed_files[0].proposals
    assert changed.groups[0].reviewed_files[0].change_set is not old_change
    assert changed.groups[0].reviewed_files[0].change_set is not None
    assert changed.groups[0].reviewed_files[0].change_set.final_metadata.title == "序曲"
    assert changed.groups[0].reviewed_files[0].change_set.rename_preview is not None
    assert changed.groups[0].reviewed_files[0].change_set.rename_preview.new_path.name == "001 - 序曲.flac"
    assert changed.groups[0].reviewed_files[0].change_set.rename_change is None
    assert changed.groups[0].selected_release is before.selected_release
    assert changed.groups[0].selected_metadata is None
    assert changed.provider_cache is state.provider_cache
    assert changed.revision == state.revision + 1
    assert changed.groups[0].revision == before.revision + 1
    assert changed.library_revision == state.library_revision
    assert title_review(state).proposals[0].value == "序曲"

    automatic = change_language(changed, "album", "auto", RenameSettings())
    assert title_review(automatic).proposals[0].value == "序曲"


@pytest.mark.parametrize("decision", ["keep", "proposal", "manual", "clear"])
def test_language_reranking_preserves_authoritative_user_decisions(decision: str) -> None:
    state = make_selected_session()
    group = state.groups[0]
    reviewed = group.reviewed_files[0]
    current = title_review(state)

    if decision == "keep":
        chosen = set_keep_existing_decision(current)
    elif decision == "proposal":
        chosen = set_proposal_decision(current, current.proposals[0])
    elif decision == "manual":
        chosen = set_manual_decision(current, "My chosen title")
    else:
        chosen = set_clear_decision(current)

    reviews = tuple(chosen if review.field is MetadataField.TITLE else review for review in reviewed.reviews)
    change_set = build_change_set(group.group.files[0], reviews, RenameDecision.KEEP_FILENAME)
    revised_file = replace(reviewed, reviews=reviews, change_set=change_set)
    state = replace(state, groups=(replace(group, reviewed_files=(revised_file,), selected_metadata=None),))

    changed = change_language(state, "album", "en", RenameSettings())
    result = title_review(changed)

    assert result.proposals[0].value == "Overture"
    assert result.decision is chosen.decision
    assert result.decision_origin is DecisionOrigin.USER
    assert result.manual_value == chosen.manual_value
    assert ReviewReasonCode.USER_DECISION_PRESERVED in result.reason_codes
    assert changed.groups[0].reviewed_files[0].change_set is not None
    assert changed.groups[0].reviewed_files[0].change_set.final_metadata == change_set.final_metadata

    if chosen.selected_proposal is not None:
        assert result.selected_proposal is not None
        assert result.selected_proposal.value == chosen.selected_proposal.value
        assert result.selected_proposal.provenances == chosen.selected_proposal.provenances


def test_search_query_override_invalidates_only_derived_lookup_state() -> None:
    state = make_selected_session()
    group = replace(state.groups[0], language_override="ja", disc_number_override=2)
    state = replace(state, groups=(group,))
    query = make_query("Edited album terms")

    changed = set_search_query_override(state, "album", query)
    result = changed.groups[0]

    assert result.search_query_override == query
    assert result.group is group.group
    assert result.language_override == "ja"
    assert result.disc_number_override == 2
    assert result.lookup_result is None
    assert result.release_ranking is None
    assert result.candidate_lookup is None
    assert result.selected_release is None
    assert result.automatic_track_mapping is None
    assert result.reviewed_files == ()
    assert result.selected_metadata is None
    assert result.selection_failure is None
    assert changed.provider_cache is state.provider_cache
    assert changed.selection == state.selection
    assert changed.revision == state.revision + 1
    assert result.revision == group.revision + 1
    assert changed.library_revision == state.library_revision
    assert state.groups[0].search_query_override is None
    assert set_search_query_override(changed, "album", query) is changed


def test_find_all_incomplete_selects_only_searchable_groups_with_missing_fields() -> None:
    selected = make_selected_session()
    original = selected.groups[0].group.files[0]
    groups: list[GroupState] = []

    for group_id, state_kind in (
        ("incomplete", "partial"),
        ("complete", "complete"),
        ("unreadable", "unreadable"),
        ("unsupported-fields", "unsupported"),
        ("no-evidence", "empty"),
        ("edited-evidence", "empty"),
        ("needs-rescan", "partial"),
    ):
        read_result = original.read_result
        album_title = original.read_result.metadata.album

        if state_kind == "complete":
            read_result = replace(
                read_result,
                metadata=MetadataSnapshot(
                    title="Title",
                    artists=("Artist",),
                    album="Album",
                    album_artists=("Artist",),
                    composers=("Composer",),
                    track=Position(1, 1),
                    disc=Position(1, 1),
                    date="2024",
                    genres=("Genre",),
                ),
                field_states={field: FieldReadState.PRESENT for field in MetadataField},
            )
        elif state_kind in {"unreadable", "unsupported", "empty"}:
            read_state = {
                "unreadable": FieldReadState.UNREADABLE,
                "unsupported": FieldReadState.UNSUPPORTED,
                "empty": FieldReadState.MISSING,
            }[state_kind]
            read_result = replace(
                read_result,
                metadata=MetadataSnapshot(),
                field_states={field: read_state for field in MetadataField},
            )
            album_title = None

        source = replace(
            original, path=Path("library") / group_id / "01.flac", file_id=group_id, read_result=read_result
        )
        group = AlbumGroup(group_id, (source,), album_title, GroupingReason.DIRECTORY_ALBUM_CONSISTENT)
        groups.append(
            GroupState(
                group=group,
                requires_rescan=group_id == "needs-rescan",
                search_query_override=make_query("Explicit album") if group_id == "edited-evidence" else None,
            )
        )

    state = SessionState(
        root=Path("library"),
        groups=tuple(groups),
        unsupported_files=(UnsupportedMediaFile(Path("library/unknown.opus")),),
    )

    assert incomplete_searchable_group_ids(state) == ("incomplete", "edited-evidence")
