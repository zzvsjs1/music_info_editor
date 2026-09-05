# Construct labelled immutable snapshots to exercise ownership and revision rules.
# Navigation, library replacement and group edits intentionally have different scopes.

from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from metadata_polisher.application.changes import RenameDecision, build_change_set
from metadata_polisher.application.lookup import (
    CandidateLookupResult,
    GroupLookupResult,
    LookupSearchResult,
    SelectedMetadataResult,
)
from metadata_polisher.application.review import (
    build_field_review_state,
    build_selected_file_results,
    set_manual_decision,
)
from metadata_polisher.domain.errors import (
    Issue,
    MatchingErrorCode,
    MediaErrorCode,
    ProviderErrorCode,
)
from metadata_polisher.domain.matching import (
    LocalisedText,
    MetadataProvenance,
    ProviderTrack,
    ReleaseCandidate,
    ReleaseMedium,
    ReleaseSearchQuery,
)
from metadata_polisher.domain.media import (
    LocalMediaFile,
    MediaReadResult,
    StreamInfo,
    UnsupportedMediaFile,
)
from metadata_polisher.domain.metadata import (
    FieldReadState,
    MetadataField,
    MetadataSnapshot,
    Position,
)
from metadata_polisher.domain.review import FieldConfidence, FieldProposal, FieldReviewState
from metadata_polisher.matching.release_scoring import (
    MatchClassification,
    MatchEvidence,
    RankedReleaseMedium,
    ReleaseRanking,
    ReleaseScore,
)
from metadata_polisher.matching.track_mapping import TrackMapping, TrackMappingResult
from metadata_polisher.providers.cache import MemoryCache
from metadata_polisher.providers.coordinator import (
    CandidateHydrationNotice,
    CandidateHydrationReasonCode,
    CoordinatedCandidate,
    ProviderFailure,
)
from metadata_polisher.scanner.grouping import (
    AlbumGroup,
    GroupingReason,
    GroupingWarning,
    GroupingWarningCode,
    GroupingWarningReason,
)
from metadata_polisher.session.state import (
    ActiveOperation,
    GroupResultEnvelope,
    GroupSelection,
    GroupState,
    GroupVersion,
    OperationKind,
    ProviderCacheKey,
    ProviderCacheValue,
    ReleaseSelectionState,
    ResultApplicationStatus,
    ReviewedFileState,
    ScanResultEnvelope,
    SessionState,
    StaleResultReason,
    UnsupportedSelection,
    apply_group_lookup_result,
    apply_group_result,
    apply_scan_result,
    begin_operation,
    finish_operation,
    mark_groups_requires_rescan,
    set_selection,
)


def make_file(
    name: str = "01.flac",
    *,
    title: str = "Opening",
    file_id: str | None = None,
) -> LocalMediaFile:
    field_states = {field: FieldReadState.MISSING for field in MetadataField}
    field_states[MetadataField.TITLE] = FieldReadState.PRESENT
    field_states[MetadataField.TRACK] = FieldReadState.PRESENT

    return LocalMediaFile(
        path=Path("library") / "Album" / name,
        format_id="flac",
        read_result=MediaReadResult(
            metadata=MetadataSnapshot(
                title=title,
                track=Position(number=1),
            ),
            field_states=field_states,
            stream_info=StreamInfo(
                duration_seconds=180.0,
                sample_rate=48_000,
                channels=2,
                bit_depth=24,
                codec="FLAC",
            ),
        ),
        file_id=file_id or f"file-{name}",
    )


def make_group(
    group_id: str = "group-0001",
    *,
    files: tuple[LocalMediaFile, ...] | None = None,
) -> AlbumGroup:
    return AlbumGroup(
        group_id=group_id,
        files=files or (make_file(),),
        album_title="Album",
        reason=GroupingReason.DIRECTORY_ALBUM_CONSISTENT,
    )


def make_reviews(source: LocalMediaFile) -> tuple[FieldReviewState, ...]:
    metadata = source.read_result.metadata
    existing_values: dict[MetadataField, object | None] = {
        MetadataField.TITLE: metadata.title,
        MetadataField.ARTISTS: None,
        MetadataField.ALBUM: None,
        MetadataField.ALBUM_ARTISTS: None,
        MetadataField.COMPOSERS: None,
        MetadataField.TRACK: metadata.track,
        MetadataField.DISC: None,
        MetadataField.DATE: None,
        MetadataField.GENRES: None,
    }

    return tuple(
        build_field_review_state(
            field=field,
            read_state=source.read_result.field_states[field],
            existing_value=existing_values[field],  # type: ignore[arg-type]
            proposals=(),
        )
        for field in MetadataField
    )


def make_reviewed_file(source: LocalMediaFile) -> ReviewedFileState:
    reviews = make_reviews(source)

    return ReviewedFileState(
        file_id=source.file_id,
        proposals=(),
        reviews=reviews,
        change_set=build_change_set(
            source,
            reviews,
            RenameDecision.KEEP_FILENAME,
        ),
    )


def make_cache() -> MemoryCache[ProviderCacheKey, ProviderCacheValue]:
    return MemoryCache(capacity=16)


def make_query(album: str = "Album") -> ReleaseSearchQuery:
    return ReleaseSearchQuery(
        album=album,
        artists=(),
        year=None,
        disc_hint=None,
        local_track_count=1,
        distinctive_titles=("Opening",),
    )


def make_candidate(
    *,
    release_id: str = "release-1",
    title: str = "Album",
) -> CoordinatedCandidate:
    candidate = ReleaseCandidate(
        engine_id="engine",
        source_id="catalogue",
        release_id=release_id,
        titles=(LocalisedText(title, "eng", "Latn"),),
        album_artists=("Artist",),
        date="2024",
        media=(
            ReleaseMedium(
                medium_number=1,
                title=None,
                tracks=(
                    ProviderTrack(
                        track_number=1,
                        titles=(LocalisedText("Opening", "eng", "Latn"),),
                        artists=("Artist",),
                        composers=(),
                        duration_seconds=180.0,
                    ),
                ),
            ),
        ),
        source_url=None,
    )

    return CoordinatedCandidate(
        candidate=candidate,
        provenance=(
            MetadataProvenance(
                engine_id="engine",
                source_id="catalogue",
                record_id=release_id,
                source_url=None,
                language=None,
                operation_id="LOOKUP-0001",
            ),
        ),
    )


def make_derived_group_state(
    group_id: str = "group-0001",
    *,
    source: LocalMediaFile | None = None,
    revision: int = 0,
) -> GroupState:
    # Assemble a full consistent chain from source through ranking and review.
    # Later tests alter one link to check that stale or mismatched state is rejected.
    local_file = source or make_file()
    album_group = make_group(group_id, files=(local_file,))
    coordinated = make_candidate()
    lookup = LookupSearchResult(
        group_id=group_id,
        queries=(make_query(),),
        candidates=(coordinated,),
        failures=(),
    )
    score = ReleaseScore(
        score=100.0,
        classification=MatchClassification.HIGH,
        evidence=(MatchEvidence("ALBUM_TITLE_EXACT", 1.0, "The titles agree."),),
    )
    ranking = ReleaseRanking(
        entries=(
            RankedReleaseMedium(
                release=coordinated.candidate,
                medium=coordinated.candidate.media[0],
                medium_index=0,
                result=score,
            ),
        ),
        ambiguous=False,
    )
    selection = ReleaseSelectionState(candidate=coordinated, medium_index=0)
    mapping = TrackMappingResult(
        mappings=(
            TrackMapping(
                local_file_id=local_file.file_id,
                provider_track_index=0,
                track_position=Position(number=1, total=1),
                disc_position=Position(number=1, total=1),
                score=100.0,
                classification=MatchClassification.HIGH,
                evidence=(MatchEvidence("TRACK_TITLE_EXACT", 1.0, "The titles agree."),),
            ),
        ),
        unmatched_local_file_ids=(),
        unmatched_provider_indexes=(),
        selected_medium_index=0,
        selected_medium_number=1,
        classification=MatchClassification.HIGH,
        evidence=(MatchEvidence("TRACK_MAPPING_COMPLETE", 1.0, "Every track is mapped."),),
    )

    return GroupState(
        group=album_group,
        lookup_result=lookup,
        release_ranking=ranking,
        selected_release=selection,
        automatic_track_mapping=mapping,
        reviewed_files=(make_reviewed_file(local_file),),
        revision=revision,
    )


def make_no_candidate_lookup(group_id: str = "group-0001") -> CandidateLookupResult:
    lookup = LookupSearchResult(
        group_id=group_id,
        queries=(make_query(),),
        candidates=(),
        failures=(),
    )

    return CandidateLookupResult(
        lookup_result=lookup,
        release_ranking=ReleaseRanking(entries=(), ambiguous=False),
        hydration_notices=(),
        matching_issues=(
            Issue(
                code=MatchingErrorCode.NO_CANDIDATE,
                message="No metadata provider returned a release candidate.",
            ),
        ),
    )


def make_candidate_lookup(group_id: str = "group-0001") -> CandidateLookupResult:
    coordinated = make_candidate()
    lookup = LookupSearchResult(
        group_id=group_id,
        queries=(make_query(),),
        candidates=(coordinated,),
        failures=(),
    )
    score = ReleaseScore(
        score=100.0,
        classification=MatchClassification.HIGH,
        evidence=(MatchEvidence("ALBUM_TITLE_EXACT", 1.0, "The titles agree."),),
    )
    ranking = ReleaseRanking(
        entries=(
            RankedReleaseMedium(
                release=coordinated.candidate,
                medium=coordinated.candidate.media[0],
                medium_index=0,
                result=score,
            ),
        ),
        ambiguous=False,
    )

    return CandidateLookupResult(
        lookup_result=lookup,
        release_ranking=ranking,
        hydration_notices=(),
        matching_issues=(),
    )


def make_selected_metadata(
    source: LocalMediaFile,
    candidate_lookup: CandidateLookupResult,
    *,
    mapping_resolved: bool,
) -> SelectedMetadataResult:
    selected = candidate_lookup.lookup_result.candidates[0]
    medium = selected.candidate.media[0]

    if mapping_resolved:
        mapping = TrackMappingResult(
            mappings=(
                TrackMapping(
                    local_file_id=source.file_id,
                    provider_track_index=0,
                    track_position=Position(number=1, total=1),
                    disc_position=Position(number=1, total=1),
                    score=100.0,
                    classification=MatchClassification.HIGH,
                    evidence=(
                        MatchEvidence("TRACK_TITLE_EXACT", 1.0, "The titles agree."),
                    ),
                ),
            ),
            unmatched_local_file_ids=(),
            unmatched_provider_indexes=(),
            selected_medium_index=0,
            selected_medium_number=medium.medium_number,
            classification=MatchClassification.HIGH,
            evidence=(),
        )
    else:
        mapping = TrackMappingResult(
            mappings=(),
            unmatched_local_file_ids=(source.file_id,),
            unmatched_provider_indexes=(0,),
            selected_medium_index=0,
            selected_medium_number=medium.medium_number,
            classification=MatchClassification.LOW,
            evidence=(),
        )

    reviewed = build_selected_file_results(
        (source,),
        selected,
        medium_index=0,
        mapping_result=mapping,
        release_classification=MatchClassification.HIGH,
        preferred_language="auto",
    )

    return SelectedMetadataResult(
        candidate_lookup=candidate_lookup,
        selected_identity=("engine", "catalogue", "release-1", 0),
        selected_candidate=selected,
        track_mapping=mapping,
        reviewed_files=reviewed,
        failures=(),
    )


def test_session_state_copies_collections_but_retains_one_non_semantic_cache_reference() -> None:
    album_group = make_group()
    warnings: list[GroupingWarning] = []
    group_state = GroupState(group=album_group, warnings=warnings)  # type: ignore[arg-type]
    groups = [group_state]
    unsupported = [UnsupportedMediaFile(Path("library/unknown.ape"))]
    issues = [
        Issue(
            code=MediaErrorCode.TAG_READ_FAILED,
            message="One file could not be read.",
        )
    ]
    cache = make_cache()

    state = SessionState(
        root=Path("library"),
        groups=groups,  # type: ignore[arg-type]
        unsupported_files=unsupported,  # type: ignore[arg-type]
        scan_issues=issues,  # type: ignore[arg-type]
        selection=GroupSelection(album_group.group_id),
        provider_cache=cache,
    )
    equal_except_for_cache = replace(state, provider_cache=make_cache())
    groups.clear()
    unsupported.clear()
    issues.clear()
    warnings.append(
        GroupingWarning(
            code=GroupingWarningCode.POSSIBLE_MULTIPLE_ALBUMS,
            reason=GroupingWarningReason.TRACK_NUMBER_RESTART_WITHOUT_STRONG_TAG_EVIDENCE,
            group_id=album_group.group_id,
            affected_file_ids=(album_group.files[0].file_id,),
            message="Review this grouping.",
        )
    )

    assert state.groups == (group_state,)
    assert state.groups[0].warnings == ()
    assert state.unsupported_files == (UnsupportedMediaFile(Path("library/unknown.ape")),)
    assert len(state.scan_issues) == 1
    assert state.provider_cache is cache
    assert state == equal_except_for_cache
    assert "provider_cache" not in repr(state)

    with pytest.raises(FrozenInstanceError):
        state.revision = 3  # type: ignore[misc]


def test_session_rejects_duplicate_group_or_global_file_ownership() -> None:
    first = make_file("01.flac", file_id="same-file")
    second = make_file("02.flac", file_id="same-file")
    first_group = GroupState(group=make_group("group-a", files=(first,)))
    duplicate_group_id = GroupState(group=make_group("group-a", files=(make_file("03.flac"),)))
    second_group = GroupState(group=make_group("group-b", files=(second,)))

    with pytest.raises(ValueError, match="group IDs must be unique"):
        SessionState(root=Path("library"), groups=(first_group, duplicate_group_id))

    with pytest.raises(ValueError, match="exactly one group"):
        SessionState(root=Path("library"), groups=(first_group, second_group))


def test_session_rejects_duplicate_supported_paths_even_with_different_file_ids() -> None:
    first = make_file(file_id="first")
    second = make_file(file_id="second")

    with pytest.raises(ValueError, match="supported file paths must be unique"):
        SessionState(
            root=Path("library"),
            groups=(
                GroupState(group=make_group("group-a", files=(first,))),
                GroupState(group=make_group("group-b", files=(second,))),
            ),
        )


def test_group_warning_and_review_state_must_belong_to_the_group_files() -> None:
    source = make_file()
    album_group = make_group(files=(source,))
    foreign_warning = GroupingWarning(
        code=GroupingWarningCode.POSSIBLE_MULTIPLE_ALBUMS,
        reason=GroupingWarningReason.TRACK_NUMBER_RESTART_WITHOUT_STRONG_TAG_EVIDENCE,
        group_id=album_group.group_id,
        affected_file_ids=("not-in-group",),
        message="Review this grouping.",
    )

    with pytest.raises(ValueError, match="warning affected file IDs"):
        GroupState(group=album_group, warnings=(foreign_warning,))

    wrong_group_warning = replace(foreign_warning, group_id="another-group")

    with pytest.raises(ValueError, match="warning must refer to this group"):
        GroupState(group=album_group, warnings=(wrong_group_warning,))

    with pytest.raises(ValueError, match="reviewed file ID"):
        GroupState(
            group=album_group,
            reviewed_files=(
                replace(
                    make_reviewed_file(source),
                    file_id="not-in-group",
                    change_set=None,
                ),
            ),
        )


def test_review_and_change_state_must_match_the_current_source_snapshot() -> None:
    current = make_file(title="Current title", file_id="stable-file")
    stale = make_file(title="Stale title", file_id="stable-file")

    with pytest.raises(ValueError, match="existing value"):
        GroupState(
            group=make_group(files=(current,)),
            reviewed_files=(make_reviewed_file(stale),),
        )


def test_change_set_must_match_the_review_decisions_stored_beside_it() -> None:
    source = make_file()
    original_reviews = make_reviews(source)
    first_reviews = (
        set_manual_decision(original_reviews[0], "First choice"),
        *original_reviews[1:],
    )
    second_reviews = (
        set_manual_decision(original_reviews[0], "Second choice"),
        *original_reviews[1:],
    )
    mismatched = ReviewedFileState(
        file_id=source.file_id,
        reviews=second_reviews,
        change_set=build_change_set(
            source,
            first_reviews,
            RenameDecision.KEEP_FILENAME,
        ),
    )
    derived = make_derived_group_state(source=source)

    with pytest.raises(ValueError, match="review decisions"):
        replace(derived, reviewed_files=(mismatched,))


def test_review_collections_are_complete_unique_and_canonical() -> None:
    source = make_file()
    reviews = make_reviews(source)
    backwards = ReviewedFileState(
        file_id=source.file_id,
        reviews=tuple(reversed(reviews)),
    )

    assert tuple(review.field for review in backwards.reviews) == tuple(MetadataField)

    with pytest.raises(ValueError, match="every MetadataField"):
        ReviewedFileState(file_id=source.file_id, reviews=reviews[:-1])

    with pytest.raises(ValueError, match="at most once"):
        ReviewedFileState(file_id=source.file_id, reviews=reviews + (reviews[0],))

    second = make_file("02.flac", file_id="second")
    first = make_file("01.flac", file_id="first")
    group = GroupState(
        group=make_group(files=(first, second)),
        reviewed_files=(make_reviewed_file(second), make_reviewed_file(first)),
    )
    partial = GroupState(
        group=make_group(files=(first, second)),
        reviewed_files=(make_reviewed_file(second),),
    )

    assert tuple(item.file_id for item in group.reviewed_files) == ("first", "second")
    assert tuple(item.file_id for item in partial.reviewed_files) == ("second",)


def test_manual_only_reviews_are_valid_but_provider_proposals_require_a_selected_release() -> None:
    source = make_file()
    manual_only = GroupState(
        group=make_group(files=(source,)),
        reviewed_files=(make_reviewed_file(source),),
    )
    proposal = FieldProposal(
        field=MetadataField.TITLE,
        value="Provider title",
        confidence=FieldConfidence.HIGH,
        provenance=make_candidate().provenance[0],
    )
    reviews = list(make_reviews(source))
    reviews[0] = build_field_review_state(
        field=MetadataField.TITLE,
        read_state=FieldReadState.PRESENT,
        existing_value=source.read_result.metadata.title,
        proposals=(proposal,),
    )
    provider_review = ReviewedFileState(
        file_id=source.file_id,
        proposals=(proposal,),
        reviews=tuple(reviews),
    )

    assert manual_only.reviewed_files[0].file_id == source.file_id

    with pytest.raises(ValueError, match="provider proposals require a selected release"):
        GroupState(
            group=make_group(files=(source,)),
            reviewed_files=(provider_review,),
        )


def test_selected_release_retains_enriched_candidate_medium_and_provenance() -> None:
    derived = make_derived_group_state()
    selection = derived.selected_release

    assert selection is not None
    assert selection.candidate is derived.lookup_result.candidates[0]  # type: ignore[union-attr]
    assert selection.medium is selection.candidate.candidate.media[0]
    assert selection.identity == ("engine", "catalogue", "release-1", 0)
    assert selection.candidate.provenance[0].operation_id == "LOOKUP-0001"

    sparse_release = replace(selection.candidate.candidate, media=())
    sparse_candidate = replace(selection.candidate, candidate=sparse_release)

    with pytest.raises(ValueError, match="enriched with media"):
        ReleaseSelectionState(sparse_candidate, 0)

    with pytest.raises(ValueError, match="medium_index"):
        ReleaseSelectionState(selection.candidate, 1)

    with pytest.raises(TypeError, match="medium_index"):
        ReleaseSelectionState(selection.candidate, True)  # type: ignore[arg-type]


def test_selected_release_must_be_ranked_and_retain_lookup_provenance() -> None:
    derived = make_derived_group_state()
    selection = derived.selected_release
    assert selection is not None
    foreign_provenance = replace(
        selection.candidate.provenance[0],
        operation_id="FOREIGN-0001",
    )
    foreign_selection = replace(
        selection,
        candidate=replace(selection.candidate, provenance=(foreign_provenance,)),
    )

    with pytest.raises(ValueError, match="lookup provenance"):
        replace(derived, selected_release=foreign_selection)

    with pytest.raises(ValueError, match="release ranking"):
        GroupState(
            group=derived.group,
            lookup_result=derived.lookup_result,
            selected_release=selection,
        )


def test_selection_is_typed_validated_and_does_not_change_semantic_revisions() -> None:
    group_state = GroupState(group=make_group())
    state = SessionState(
        root=Path("library"),
        groups=(group_state,),
        unsupported_files=(UnsupportedMediaFile(Path("library/unknown.ape")),),
        revision=7,
        library_revision=2,
    )

    selected_group = set_selection(state, GroupSelection(group_state.group.group_id))
    selected_unsupported = set_selection(selected_group, UnsupportedSelection())
    cleared = set_selection(selected_unsupported, None)

    assert isinstance(selected_group.selection, GroupSelection)
    assert isinstance(selected_unsupported.selection, UnsupportedSelection)
    assert cleared.selection is None
    assert selected_group.revision == selected_unsupported.revision == cleared.revision == 7
    assert selected_group.library_revision == selected_unsupported.library_revision == 2

    with pytest.raises(ValueError, match="selected group must exist"):
        replace(state, selection=GroupSelection("missing"))

    with pytest.raises(ValueError, match="unsupported files"):
        replace(state, unsupported_files=(), selection=UnsupportedSelection())


def test_active_operation_target_is_independent_of_visible_selection() -> None:
    group_state = GroupState(group=make_group())
    operation = ActiveOperation(
        operation_id="LOOKUP-0001",
        kind=OperationKind.LOOKUP,
        base_session_revision=0,
        base_library_revision=0,
        target_groups=(GroupVersion(group_state.group.group_id, 0),),
    )
    state = SessionState(
        root=Path("library"),
        groups=(group_state,),
        active_operation=operation,
    )

    changed_selection = set_selection(state, None)

    assert changed_selection.active_operation is operation
    assert changed_selection.revision == state.revision

    with pytest.raises(ValueError, match="operation target group must exist"):
        replace(
            state,
            active_operation=replace(
                operation,
                target_groups=(GroupVersion("missing", 0),),
            ),
        )

    with pytest.raises(ValueError, match="operation target group IDs must be unique"):
        replace(
            operation,
            target_groups=(
                GroupVersion(group_state.group.group_id, 0),
                GroupVersion(group_state.group.group_id, 0),
            ),
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        ({"language_override": ""}, "language_override"),
        ({"disc_number_override": True}, "disc_number_override"),
        ({"disc_number_override": 0}, "greater than zero"),
        ({"search_query_override": "Album"}, "ReleaseSearchQuery"),
        ({"requires_rescan": 1}, "requires_rescan"),
    ),
)
def test_group_session_only_overrides_are_validated(
    changes: dict[str, object],
    message: str,
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        GroupState(group=make_group(), **changes)  # type: ignore[arg-type]


def test_group_language_override_defaults_to_inheriting_global_preference() -> None:
    assert GroupState(group=make_group()).language_override is None


def test_marking_selected_groups_for_rescan_invalidates_only_their_derived_state() -> None:
    first_file = make_file("01.flac", file_id="first")
    second_file = make_file("02.flac", file_id="second")
    first = replace(
        make_derived_group_state("group-a", source=first_file, revision=3),
        language_override="ja",
        disc_number_override=2,
        search_query_override=make_query("Custom album query"),
    )
    second = make_derived_group_state("group-b", source=second_file, revision=4)
    state = SessionState(
        root=Path("library"),
        groups=(first, second),
        revision=8,
        library_revision=2,
    )

    updated = mark_groups_requires_rescan(state, (first.group.group_id,))

    invalidated = updated.groups[0]
    assert invalidated.requires_rescan is True
    assert invalidated.reviewed_files == ()
    assert invalidated.lookup_result is None
    assert invalidated.release_ranking is None
    assert invalidated.selected_release is None
    assert invalidated.automatic_track_mapping is None
    assert invalidated.language_override == "ja"
    assert invalidated.disc_number_override == 2
    assert invalidated.search_query_override == make_query("Custom album query")
    assert invalidated.revision == 4
    assert updated.groups[1] is second
    assert updated.revision == 9
    assert updated.library_revision == 2
    assert state.groups[0] is first

    with pytest.raises(ValueError, match="unknown group"):
        mark_groups_requires_rescan(state, ("missing",))


def test_a_group_requiring_rescan_cannot_retain_provider_or_review_state() -> None:
    source = make_file()

    with pytest.raises(ValueError, match="cannot retain derived state"):
        GroupState(
            group=make_group(files=(source,)),
            reviewed_files=(make_reviewed_file(source),),
            requires_rescan=True,
        )


def test_empty_session_cannot_claim_library_state_without_a_root() -> None:
    assert SessionState(root=None, provider_cache=make_cache()).groups == ()

    with pytest.raises(ValueError, match="root is required"):
        SessionState(root=None, groups=(GroupState(group=make_group()),))

    with pytest.raises(ValueError, match="inside the scan root"):
        SessionState(
            root=Path("elsewhere"),
            groups=(GroupState(group=make_group()),),
        )

    with pytest.raises(ValueError, match="inside the scan root"):
        SessionState(
            root=Path("library"),
            unsupported_files=(UnsupportedMediaFile(Path("library/../outside.ape")),),
        )

    with pytest.raises(TypeError, match="media paths must be Path"):
        SessionState(
            root=Path("library"),
            unsupported_files=(UnsupportedMediaFile("library/unknown.ape"),),  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        ({"revision": True}, "revision"),
        ({"revision": -1}, "revision"),
        ({"library_revision": True}, "library_revision"),
        ({"library_revision": -1}, "library_revision"),
    ),
)
def test_session_revisions_reject_bool_and_negative_values(
    changes: dict[str, object],
    message: str,
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        SessionState(root=None, **changes)  # type: ignore[arg-type]


@pytest.mark.parametrize("revision", (True, -1))
def test_group_revision_rejects_bool_and_negative_values(revision: object) -> None:
    with pytest.raises((TypeError, ValueError), match="revision"):
        GroupState(group=make_group(), revision=revision)  # type: ignore[arg-type]


def test_begin_operation_captures_multi_group_versions_and_validates_scope() -> None:
    first = GroupState(group=make_group("group-a", files=(make_file("01.flac"),)), revision=2)
    second = GroupState(group=make_group("group-b", files=(make_file("02.flac"),)), revision=4)
    state = SessionState(
        root=Path("library"),
        groups=(first, second),
        revision=7,
        library_revision=3,
    )

    started = begin_operation(
        state,
        operation_id="LOOKUP-0001",
        kind=OperationKind.LOOKUP,
        target_group_ids=("group-b", "group-a"),
    )

    assert started.active_operation == ActiveOperation(
        operation_id="LOOKUP-0001",
        kind=OperationKind.LOOKUP,
        base_session_revision=7,
        base_library_revision=3,
        target_groups=(GroupVersion("group-b", 4), GroupVersion("group-a", 2)),
    )
    assert started.revision == 7
    assert started.library_revision == 3

    with pytest.raises(ValueError, match="must target at least one group"):
        begin_operation(state, "LOOKUP-0002", OperationKind.LOOKUP, ())

    with pytest.raises(ValueError, match="must not target groups"):
        begin_operation(state, "SCAN-0001", OperationKind.SCAN, ("group-a",))

    with pytest.raises(ValueError, match="target group IDs must be unique"):
        begin_operation(
            state,
            "APPLY-0001",
            OperationKind.APPLY,
            ("group-a", "group-a"),
        )

    with pytest.raises(ValueError, match="unknown operation target group"):
        begin_operation(state, "LOOKUP-0003", OperationKind.LOOKUP, ("missing",))

    with pytest.raises(ValueError, match="already active"):
        begin_operation(
            started,
            "APPLY-0002",
            OperationKind.APPLY,
            ("group-a",),
        )


def test_finish_operation_never_clears_newer_work_with_an_old_completion() -> None:
    state = SessionState(
        root=Path("library"),
        groups=(GroupState(group=make_group()),),
    )
    newer = begin_operation(
        state,
        "LOOKUP-0002",
        OperationKind.LOOKUP,
        ("group-0001",),
    )

    unchanged = finish_operation(newer, "LOOKUP-0001")
    finished = finish_operation(newer, "LOOKUP-0002")

    assert unchanged is newer
    assert finished.active_operation is None
    assert finished.revision == newer.revision


def test_scan_result_atomically_replaces_the_library_and_preserves_the_cache() -> None:
    old_group = GroupState(group=make_group("old-group"))
    cache = make_cache()
    initial = SessionState(
        root=Path("library"),
        groups=(old_group,),
        unsupported_files=(UnsupportedMediaFile(Path("library/old.ape")),),
        scan_issues=(Issue(MediaErrorCode.TAG_READ_FAILED, "Old issue"),),
        selection=GroupSelection("old-group"),
        revision=5,
        library_revision=2,
        provider_cache=cache,
    )
    started = begin_operation(initial, "SCAN-0001", OperationKind.SCAN, ())
    new_group = GroupState(
        group=make_group("new-group", files=(make_file("02.flac"),)),
    )
    new_unsupported = UnsupportedMediaFile(Path("library/new.ape"))
    new_issue = Issue(MediaErrorCode.CORRUPT_FILE, "New issue")
    envelope = ScanResultEnvelope(
        operation_id="SCAN-0001",
        base_session_revision=5,
        base_library_revision=2,
        root=Path("library"),
        groups=(new_group,),
        unsupported_files=(new_unsupported,),
        scan_issues=(new_issue,),
    )

    applied = apply_scan_result(started, envelope)

    assert applied.status is ResultApplicationStatus.APPLIED
    assert applied.reason is None
    assert applied.state is not started
    assert applied.state.groups == (new_group,)
    assert applied.state.unsupported_files == (new_unsupported,)
    assert applied.state.scan_issues == (new_issue,)
    assert applied.state.selection is None
    assert applied.state.provider_cache is cache
    assert applied.state.revision == 6
    assert applied.state.library_revision == 3
    assert applied.state.active_operation is started.active_operation
    assert started.groups == (old_group,)


def test_scan_result_cannot_inject_session_only_group_overrides() -> None:
    with pytest.raises(ValueError, match="session-only overrides"):
        ScanResultEnvelope(
            operation_id="SCAN-0001",
            base_session_revision=0,
            base_library_revision=0,
            root=Path("library"),
            groups=(
                GroupState(
                    group=make_group(),
                    language_override="ja",
                ),
            ),
        )


@pytest.mark.parametrize(
    ("state_change", "expected_reason"),
    (
        ("operation", StaleResultReason.OPERATION_MISMATCH),
        ("session", StaleResultReason.SESSION_REVISION_CHANGED),
        ("library", StaleResultReason.LIBRARY_REVISION_CHANGED),
    ),
)
def test_stale_scan_results_are_rejected_without_allocating_replacement_state(
    state_change: str,
    expected_reason: StaleResultReason,
) -> None:
    group = GroupState(group=make_group())
    base = SessionState(root=Path("library"), groups=(group,))
    started = begin_operation(base, "SCAN-0001", OperationKind.SCAN, ())
    envelope = ScanResultEnvelope(
        operation_id="SCAN-0001",
        base_session_revision=0,
        base_library_revision=0,
        root=Path("library"),
        groups=(GroupState(group=make_group("fresh-group")),),
    )

    if state_change == "operation":
        current = replace(
            started,
            active_operation=replace(
                started.active_operation,  # type: ignore[arg-type]
                operation_id="SCAN-0002",
            ),
        )
    elif state_change == "session":
        current = replace(started, revision=1)
    else:
        current = replace(started, revision=1, library_revision=1)

    rejected = apply_scan_result(current, envelope)

    assert rejected.status is ResultApplicationStatus.STALE
    assert rejected.reason is expected_reason
    assert rejected.state is current


def test_scan_result_rejects_matching_id_owned_by_a_non_scan_operation() -> None:
    group = GroupState(group=make_group())
    base = SessionState(root=Path("library"), groups=(group,))
    started = begin_operation(
        base,
        "WORK-0001",
        OperationKind.LOOKUP,
        (group.group.group_id,),
    )
    envelope = ScanResultEnvelope(
        operation_id="WORK-0001",
        base_session_revision=0,
        base_library_revision=0,
        root=Path("library"),
        groups=(GroupState(group=make_group("fresh-group")),),
    )

    rejected = apply_scan_result(started, envelope)

    assert rejected.status is ResultApplicationStatus.STALE
    assert rejected.reason is StaleResultReason.OPERATION_MISMATCH
    assert rejected.state is started


def make_group_envelope(
    state: SessionState,
    replacement: GroupState,
    *,
    operation_id: str = "LOOKUP-0001",
) -> GroupResultEnvelope:
    active = state.active_operation
    assert active is not None
    target = next(
        target
        for target in active.target_groups
        if target.group_id == replacement.group.group_id
    )

    return GroupResultEnvelope(
        operation_id=operation_id,
        base_session_revision=active.base_session_revision,
        base_library_revision=active.base_library_revision,
        target=target,
        group_state=replacement,
    )


def test_group_lookup_reducer_retains_zero_candidate_diagnostics_and_lineage() -> None:
    group = GroupState(group=make_group(), revision=2)
    base = SessionState(
        root=Path("library"),
        groups=(group,),
        revision=7,
        library_revision=3,
    )
    started = begin_operation(
        base,
        "LOOKUP-0001",
        OperationKind.LOOKUP,
        (group.group.group_id,),
    )
    candidate_lookup = make_no_candidate_lookup()
    result = GroupLookupResult(
        operation_id="LOOKUP-0001",
        base_session_revision=7,
        base_library_revision=3,
        group_id=group.group.group_id,
        base_group_revision=2,
        candidate_lookup=candidate_lookup,
    )

    applied = apply_group_lookup_result(started, result)

    assert applied.status is ResultApplicationStatus.APPLIED
    reduced = applied.state.groups[0]
    assert reduced.candidate_lookup is candidate_lookup
    assert reduced.lookup_result is candidate_lookup.lookup_result
    assert reduced.release_ranking is candidate_lookup.release_ranking
    assert reduced.candidate_lookup.matching_issues[0].code is MatchingErrorCode.NO_CANDIDATE
    assert reduced.selected_release is None
    assert reduced.automatic_track_mapping is None
    assert reduced.reviewed_files == ()
    assert reduced.revision == 3
    assert applied.state.revision == 8
    assert applied.state.library_revision == 3
    assert applied.state.active_operation is started.active_operation


def test_group_lookup_reducer_retains_partial_provider_and_hydration_diagnostics() -> None:
    group = GroupState(group=make_group())
    base = SessionState(root=Path("library"), groups=(group,))
    started = begin_operation(
        base,
        "LOOKUP-0001",
        OperationKind.LOOKUP,
        (group.group.group_id,),
    )
    scoreable = make_candidate_lookup()
    sparse = make_candidate(release_id="release-2")
    sparse = replace(sparse, candidate=replace(sparse.candidate, media=()))
    lookup = replace(
        scoreable.lookup_result,
        candidates=(*scoreable.lookup_result.candidates, sparse),
        failures=(
            ProviderFailure(
                engine_id="offline-engine",
                issue=Issue(
                    code=ProviderErrorCode.SERVICE_UNAVAILABLE,
                    message="The provider is temporarily unavailable.",
                ),
            ),
        ),
    )
    notice = CandidateHydrationNotice(
        candidate_identity=("engine", "catalogue", "release-2"),
        reason_code=CandidateHydrationReasonCode.BASIC_MEDIA_INVALID,
        issue=Issue(
            code=ProviderErrorCode.INVALID_RESPONSE,
            message="The provider returned incomplete basic media.",
        ),
    )
    candidate_lookup = CandidateLookupResult(
        lookup_result=lookup,
        release_ranking=scoreable.release_ranking,
        hydration_notices=(notice,),
        matching_issues=(),
    )
    result = GroupLookupResult(
        operation_id="LOOKUP-0001",
        base_session_revision=0,
        base_library_revision=0,
        group_id=group.group.group_id,
        base_group_revision=0,
        candidate_lookup=candidate_lookup,
    )

    applied = apply_group_lookup_result(started, result)

    assert applied.status is ResultApplicationStatus.APPLIED
    reduced = applied.state.groups[0]
    assert reduced.candidate_lookup is candidate_lookup
    assert reduced.candidate_lookup.lookup_result.failures == lookup.failures
    assert reduced.candidate_lookup.hydration_notices == (notice,)
    assert reduced.candidate_lookup.matching_issues == ()


def test_group_lookup_reducer_projects_one_successful_selection_exactly() -> None:
    source = make_file()
    group = GroupState(group=make_group(files=(source,)), revision=1)
    base = SessionState(
        root=Path("library"),
        groups=(group,),
        revision=4,
        library_revision=2,
    )
    started = begin_operation(
        base,
        "SELECT-0001",
        OperationKind.LOOKUP,
        (group.group.group_id,),
    )
    candidate_lookup = make_candidate_lookup()
    selected = make_selected_metadata(
        source,
        candidate_lookup,
        mapping_resolved=False,
    )
    result = GroupLookupResult(
        operation_id="SELECT-0001",
        base_session_revision=4,
        base_library_revision=2,
        group_id=group.group.group_id,
        base_group_revision=1,
        candidate_lookup=candidate_lookup,
        selected_metadata=selected,
    )

    applied = apply_group_lookup_result(started, result)

    assert applied.status is ResultApplicationStatus.APPLIED
    reduced = applied.state.groups[0]
    assert reduced.candidate_lookup is candidate_lookup
    assert reduced.selected_metadata is selected
    assert reduced.selection_failure is None
    assert reduced.selected_release == ReleaseSelectionState(
        selected.candidate_lookup.lookup_result.candidates[0],
        0,
    )
    assert reduced.automatic_track_mapping is selected.track_mapping
    assert tuple(item.file_id for item in reduced.reviewed_files) == (source.file_id,)
    assert reduced.reviewed_files[0].track_mapping_resolved is False
    assert reduced.reviewed_files[0].proposals == selected.reviewed_files[0].proposals
    assert reduced.reviewed_files[0].reviews == selected.reviewed_files[0].reviews
    assert reduced.reviewed_files[0].change_set is selected.reviewed_files[0].change_set


def test_failed_selection_retains_prior_valid_selection_and_candidate_diagnostics() -> None:
    source = make_file()
    candidate_lookup = make_candidate_lookup()
    prior_success = make_selected_metadata(
        source,
        candidate_lookup,
        mapping_resolved=True,
    )
    prior = GroupState(
        group=make_group(files=(source,)),
        lookup_result=candidate_lookup.lookup_result,
        release_ranking=candidate_lookup.release_ranking,
        candidate_lookup=candidate_lookup,
        selected_release=ReleaseSelectionState(prior_success.selected_candidate, 0),  # type: ignore[arg-type]
        automatic_track_mapping=prior_success.track_mapping,
        reviewed_files=tuple(
            ReviewedFileState(
                file_id=item.file_id,
                proposals=item.proposals,
                reviews=item.reviews,
                track_mapping_resolved=item.track_mapping_resolved,
                change_set=item.change_set,
            )
            for item in prior_success.reviewed_files
        ),
        selected_metadata=prior_success,
        revision=2,
    )
    base = SessionState(
        root=Path("library"),
        groups=(prior,),
        revision=5,
        library_revision=1,
    )
    started = begin_operation(
        base,
        "SELECT-0002",
        OperationKind.LOOKUP,
        (prior.group.group_id,),
    )
    failed = SelectedMetadataResult(
        candidate_lookup=candidate_lookup,
        selected_identity=("engine", "catalogue", "release-1", 0),
        selected_candidate=None,
        track_mapping=None,
        reviewed_files=(),
        failures=(
            ProviderFailure(
                engine_id="engine",
                issue=Issue(
                    code=ProviderErrorCode.SERVICE_UNAVAILABLE,
                    message="The provider is temporarily unavailable.",
                ),
            ),
        ),
    )
    result = GroupLookupResult(
        operation_id="SELECT-0002",
        base_session_revision=5,
        base_library_revision=1,
        group_id=prior.group.group_id,
        base_group_revision=2,
        candidate_lookup=candidate_lookup,
        selected_metadata=failed,
    )

    applied = apply_group_lookup_result(started, result)

    assert applied.status is ResultApplicationStatus.APPLIED
    reduced = applied.state.groups[0]
    assert reduced.candidate_lookup is candidate_lookup
    assert reduced.lookup_result is prior.lookup_result
    assert reduced.release_ranking is prior.release_ranking
    assert reduced.selected_release is prior.selected_release
    assert reduced.automatic_track_mapping is prior.automatic_track_mapping
    assert reduced.reviewed_files == prior.reviewed_files
    assert reduced.selected_metadata is prior_success
    assert reduced.selection_failure is failed
    assert reduced.selection_failure.selected_identity == (
        "engine",
        "catalogue",
        "release-1",
        0,
    )


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    (
        ("operation", StaleResultReason.OPERATION_MISMATCH),
        ("library", StaleResultReason.LIBRARY_REVISION_CHANGED),
        ("group", StaleResultReason.GROUP_REVISION_CHANGED),
    ),
)
def test_group_lookup_reducer_rejects_stale_lineage(
    case: str,
    expected_reason: StaleResultReason,
) -> None:
    group = GroupState(group=make_group())
    base = SessionState(root=Path("library"), groups=(group,))
    started = begin_operation(
        base,
        "LOOKUP-0001",
        OperationKind.LOOKUP,
        (group.group.group_id,),
    )
    result = GroupLookupResult(
        operation_id="LOOKUP-0001",
        base_session_revision=0,
        base_library_revision=0,
        group_id=group.group.group_id,
        base_group_revision=0,
        candidate_lookup=make_no_candidate_lookup(),
    )

    if case == "operation":
        result = replace(result, operation_id="LOOKUP-9999")
        current = started
    elif case == "library":
        current = replace(started, revision=1, library_revision=1)
    else:
        current = replace(
            started,
            groups=(replace(group, revision=1),),
            revision=1,
        )

    rejected = apply_group_lookup_result(current, result)

    assert rejected.status is ResultApplicationStatus.STALE
    assert rejected.reason is expected_reason
    assert rejected.state is current


def test_group_lookup_results_for_two_targets_apply_sequentially() -> None:
    first = GroupState(group=make_group("group-a", files=(make_file("01.flac"),)))
    second = GroupState(group=make_group("group-b", files=(make_file("02.flac"),)))
    base = SessionState(root=Path("library"), groups=(first, second))
    started = begin_operation(
        base,
        "LOOKUP-0001",
        OperationKind.LOOKUP,
        ("group-a", "group-b"),
    )
    first_result = GroupLookupResult(
        operation_id="LOOKUP-0001",
        base_session_revision=0,
        base_library_revision=0,
        group_id="group-a",
        base_group_revision=0,
        candidate_lookup=make_no_candidate_lookup("group-a"),
    )
    second_result = GroupLookupResult(
        operation_id="LOOKUP-0001",
        base_session_revision=0,
        base_library_revision=0,
        group_id="group-b",
        base_group_revision=0,
        candidate_lookup=make_no_candidate_lookup("group-b"),
    )

    first_applied = apply_group_lookup_result(started, first_result)
    second_applied = apply_group_lookup_result(first_applied.state, second_result)

    assert first_applied.status is ResultApplicationStatus.APPLIED
    assert second_applied.status is ResultApplicationStatus.APPLIED
    assert tuple(group.revision for group in second_applied.state.groups) == (1, 1)
    assert second_applied.state.revision == 2
    assert second_applied.state.active_operation is started.active_operation


def test_fresh_group_search_clears_prior_success_and_failed_attempt_state() -> None:
    source = make_file()
    candidate_lookup = make_candidate_lookup()
    selected = make_selected_metadata(
        source,
        candidate_lookup,
        mapping_resolved=True,
    )
    reviewed_files = tuple(
        ReviewedFileState(
            file_id=item.file_id,
            proposals=item.proposals,
            reviews=item.reviews,
            track_mapping_resolved=item.track_mapping_resolved,
            change_set=item.change_set,
        )
        for item in selected.reviewed_files
    )
    failed = SelectedMetadataResult(
        candidate_lookup=candidate_lookup,
        selected_identity=selected.selected_identity,
        selected_candidate=None,
        track_mapping=None,
        reviewed_files=(),
        failures=(
            ProviderFailure(
                engine_id="engine",
                issue=Issue(
                    code=ProviderErrorCode.SERVICE_UNAVAILABLE,
                    message="The provider is temporarily unavailable.",
                ),
            ),
        ),
    )
    group = GroupState(
        group=make_group(files=(source,)),
        lookup_result=candidate_lookup.lookup_result,
        release_ranking=candidate_lookup.release_ranking,
        candidate_lookup=candidate_lookup,
        selected_release=ReleaseSelectionState(selected.selected_candidate, 0),  # type: ignore[arg-type]
        automatic_track_mapping=selected.track_mapping,
        reviewed_files=reviewed_files,
        selected_metadata=selected,
        selection_failure=failed,
        revision=2,
    )
    base = SessionState(
        root=Path("library"),
        groups=(group,),
        revision=5,
        library_revision=1,
    )
    started = begin_operation(
        base,
        "SEARCH-0002",
        OperationKind.LOOKUP,
        (group.group.group_id,),
    )
    fresh_lookup = make_no_candidate_lookup()
    result = GroupLookupResult(
        operation_id="SEARCH-0002",
        base_session_revision=5,
        base_library_revision=1,
        group_id=group.group.group_id,
        base_group_revision=2,
        candidate_lookup=fresh_lookup,
    )

    applied = apply_group_lookup_result(started, result)

    assert applied.status is ResultApplicationStatus.APPLIED
    reduced = applied.state.groups[0]
    assert reduced.candidate_lookup is fresh_lookup
    assert reduced.selected_release is None
    assert reduced.automatic_track_mapping is None
    assert reduced.reviewed_files == ()
    assert reduced.selected_metadata is None
    assert reduced.selection_failure is None


def test_exact_selected_metadata_projection_rejects_drifted_split_state() -> None:
    source = make_file()
    candidate_lookup = make_candidate_lookup()
    selected = make_selected_metadata(
        source,
        candidate_lookup,
        mapping_resolved=False,
    )
    reviewed_files = tuple(
        ReviewedFileState(
            file_id=item.file_id,
            proposals=item.proposals,
            reviews=item.reviews,
            track_mapping_resolved=item.track_mapping_resolved,
            change_set=item.change_set,
        )
        for item in selected.reviewed_files
    )
    state = GroupState(
        group=make_group(files=(source,)),
        lookup_result=candidate_lookup.lookup_result,
        release_ranking=candidate_lookup.release_ranking,
        candidate_lookup=candidate_lookup,
        selected_release=ReleaseSelectionState(selected.selected_candidate, 0),  # type: ignore[arg-type]
        automatic_track_mapping=selected.track_mapping,
        reviewed_files=reviewed_files,
        selected_metadata=selected,
    )
    assert state.automatic_track_mapping is not None
    drifted_mapping = replace(
        state.automatic_track_mapping,
        evidence=(MatchEvidence("DRIFTED", 0.0, "This mapping is not the retained result."),),
    )

    with pytest.raises(ValueError, match="track mapping must exactly project"):
        replace(state, automatic_track_mapping=drifted_mapping)

    with pytest.raises(ValueError, match="reviewed files must exactly project"):
        replace(
            state,
            reviewed_files=(replace(reviewed_files[0], track_mapping_resolved=True),),
        )

    with pytest.raises(ValueError, match="reviewed files must exactly project"):
        replace(state, reviewed_files=(make_reviewed_file(source),))

    selected_candidate = selected.selected_candidate
    assert selected_candidate is not None
    changed_track = replace(
        selected_candidate.candidate.media[0].tracks[0],
        composers=("Different composer",),
    )
    changed_candidate = replace(
        selected_candidate,
        candidate=replace(
            selected_candidate.candidate,
            media=(
                replace(
                    selected_candidate.candidate.media[0],
                    tracks=(changed_track,),
                ),
            ),
        ),
    )

    with pytest.raises(ValueError, match="selected release must exactly project"):
        replace(
            state,
            selected_release=ReleaseSelectionState(changed_candidate, 0),
        )

    entry = candidate_lookup.release_ranking.entries[0]
    changed_ranking = replace(
        candidate_lookup.release_ranking,
        entries=(replace(entry, result=replace(entry.result, score=99.0)),),
    )
    changed_lookup = replace(candidate_lookup, release_ranking=changed_ranking)

    with pytest.raises(ValueError, match="selected_metadata must retain candidate_lookup"):
        replace(
            state,
            candidate_lookup=changed_lookup,
            release_ranking=changed_ranking,
        )


def test_group_result_replaces_only_its_target_as_one_atomic_state() -> None:
    first_file = make_file("01.flac", file_id="first")
    second_file = make_file("02.flac", file_id="second")
    first = GroupState(group=make_group("group-a", files=(first_file,)))
    second = GroupState(group=make_group("group-b", files=(second_file,)))
    base = SessionState(
        root=Path("library"),
        groups=(first, second),
        selection=GroupSelection("group-a"),
    )
    started = begin_operation(
        base,
        "LOOKUP-0001",
        OperationKind.LOOKUP,
        ("group-a", "group-b"),
    )
    replacement = make_derived_group_state("group-a", source=first_file)

    applied = apply_group_result(started, make_group_envelope(started, replacement))

    assert applied.status is ResultApplicationStatus.APPLIED
    assert applied.reason is None
    assert applied.state.groups[0] == replace(replacement, revision=1)
    assert applied.state.groups[1] is second
    assert applied.state.revision == 1
    assert applied.state.library_revision == 0
    assert applied.state.active_operation is started.active_operation
    assert started.groups[0] is first


def test_visible_selection_and_unrelated_group_updates_do_not_stale_group_result() -> None:
    first_file = make_file("01.flac", file_id="first")
    second_file = make_file("02.flac", file_id="second")
    first = GroupState(group=make_group("group-a", files=(first_file,)))
    second = GroupState(group=make_group("group-b", files=(second_file,)))
    base = SessionState(root=Path("library"), groups=(first, second))
    started = begin_operation(
        base,
        "LOOKUP-0001",
        OperationKind.LOOKUP,
        ("group-a",),
    )
    envelope = make_group_envelope(
        started,
        make_derived_group_state("group-a", source=first_file),
    )
    changed_second = replace(second, language_override="ja", revision=1)
    current = replace(
        set_selection(started, GroupSelection("group-b")),
        groups=(first, changed_second),
        revision=1,
    )

    applied = apply_group_result(current, envelope)

    assert applied.status is ResultApplicationStatus.APPLIED
    assert applied.state.selection == GroupSelection("group-b")
    assert applied.state.groups[1] is changed_second
    assert applied.state.groups[0].selected_release is not None


def test_group_result_rejects_old_group_revision_after_candidate_state_changes() -> None:
    source = make_file()
    group = GroupState(group=make_group(files=(source,)))
    base = SessionState(root=Path("library"), groups=(group,))
    started = begin_operation(
        base,
        "LOOKUP-0001",
        OperationKind.LOOKUP,
        (group.group.group_id,),
    )
    envelope = make_group_envelope(
        started,
        make_derived_group_state(source=source),
    )
    changed_group = replace(group, language_override="ja", revision=1)
    current = replace(started, groups=(changed_group,), revision=1)

    rejected = apply_group_result(current, envelope)

    assert rejected.status is ResultApplicationStatus.STALE
    assert rejected.reason is StaleResultReason.GROUP_REVISION_CHANGED
    assert rejected.state is current


def test_group_result_rejects_rescan_with_reused_group_id_by_library_revision() -> None:
    original_file = make_file("01.flac", file_id="original")
    original = GroupState(group=make_group(files=(original_file,)))
    base = SessionState(root=Path("library"), groups=(original,))
    started = begin_operation(
        base,
        "LOOKUP-0001",
        OperationKind.LOOKUP,
        (original.group.group_id,),
    )
    envelope = make_group_envelope(
        started,
        make_derived_group_state(source=original_file),
    )
    rescanned = GroupState(
        group=make_group(files=(make_file("02.flac", file_id="rescanned"),)),
    )
    current = replace(
        started,
        groups=(rescanned,),
        revision=1,
        library_revision=1,
    )

    rejected = apply_group_result(current, envelope)

    assert rejected.status is ResultApplicationStatus.STALE
    assert rejected.reason is StaleResultReason.LIBRARY_REVISION_CHANGED
    assert rejected.state is current


def test_group_result_reports_missing_target_after_library_replacement() -> None:
    first = GroupState(group=make_group("group-a", files=(make_file("01.flac"),)))
    second = GroupState(group=make_group("group-b", files=(make_file("02.flac"),)))
    base = SessionState(root=Path("library"), groups=(first, second))
    started = begin_operation(
        base,
        "LOOKUP-0001",
        OperationKind.LOOKUP,
        ("group-a",),
    )
    envelope = make_group_envelope(started, make_derived_group_state("group-a"))
    current = replace(
        started,
        groups=(second,),
        revision=1,
        library_revision=1,
    )

    rejected = apply_group_result(current, envelope)

    assert rejected.status is ResultApplicationStatus.STALE
    assert rejected.reason is StaleResultReason.GROUP_MISSING
    assert rejected.state is current


def test_group_result_rejects_a_target_not_captured_by_the_active_operation() -> None:
    first_file = make_file("01.flac", file_id="first")
    second_file = make_file("02.flac", file_id="second")
    first = GroupState(group=make_group("group-a", files=(first_file,)))
    second = GroupState(group=make_group("group-b", files=(second_file,)))
    base = SessionState(root=Path("library"), groups=(first, second))
    started = begin_operation(
        base,
        "LOOKUP-0001",
        OperationKind.LOOKUP,
        ("group-a",),
    )
    envelope = GroupResultEnvelope(
        operation_id="LOOKUP-0001",
        base_session_revision=0,
        base_library_revision=0,
        target=GroupVersion("group-b", 0),
        group_state=make_derived_group_state("group-b", source=second_file),
    )

    rejected = apply_group_result(started, envelope)

    assert rejected.status is ResultApplicationStatus.STALE
    assert rejected.reason is StaleResultReason.OPERATION_MISMATCH
    assert rejected.state is started


@pytest.mark.parametrize("smuggled_field", ("group", "warnings", "language_override"))
def test_group_lookup_result_cannot_smuggle_library_or_session_owned_state(
    smuggled_field: str,
) -> None:
    source = make_file()
    warning = GroupingWarning(
        code=GroupingWarningCode.POSSIBLE_MULTIPLE_ALBUMS,
        reason=GroupingWarningReason.TRACK_NUMBER_RESTART_WITHOUT_STRONG_TAG_EVIDENCE,
        group_id="group-0001",
        affected_file_ids=(source.file_id,),
        message="Review this grouping.",
    )
    current_group = GroupState(
        group=make_group(files=(source,)),
        warnings=(warning,),
        language_override="ja",
    )
    base = SessionState(root=Path("library"), groups=(current_group,))
    started = begin_operation(
        base,
        "LOOKUP-0001",
        OperationKind.LOOKUP,
        (current_group.group.group_id,),
    )
    replacement = replace(
        make_derived_group_state(source=source),
        warnings=(warning,),
        language_override="ja",
    )

    if smuggled_field == "group":
        replacement = GroupState(
            group=make_group(files=(make_file("02.flac", file_id="other"),)),
            warnings=(),
        )
    elif smuggled_field == "warnings":
        replacement = replace(replacement, warnings=())
    else:
        replacement = replace(replacement, language_override="eng")

    envelope = GroupResultEnvelope(
        operation_id="LOOKUP-0001",
        base_session_revision=0,
        base_library_revision=0,
        target=GroupVersion("group-0001", 0),
        group_state=replacement,
    )

    with pytest.raises(ValueError, match="cannot change"):
        apply_group_result(started, envelope)


def test_old_high_group_revision_is_representable_after_rescan_resets_reused_id() -> None:
    source = make_file()
    original = GroupState(group=make_group(files=(source,)), revision=3)
    base = SessionState(
        root=Path("library"),
        groups=(original,),
        revision=3,
    )
    started = begin_operation(
        base,
        "LOOKUP-0001",
        OperationKind.LOOKUP,
        (original.group.group_id,),
    )
    envelope = make_group_envelope(
        started,
        make_derived_group_state(source=source, revision=3),
    )
    rescanned = GroupState(
        group=make_group(files=(make_file("02.flac", file_id="new"),)),
    )
    current = replace(
        started,
        groups=(rescanned,),
        revision=4,
        library_revision=1,
    )

    rejected = apply_group_result(current, envelope)

    assert rejected.status is ResultApplicationStatus.STALE
    assert rejected.reason is StaleResultReason.LIBRARY_REVISION_CHANGED
    assert rejected.state is current


def test_old_group_completion_cannot_overwrite_or_clear_a_newer_operation() -> None:
    source = make_file()
    group = GroupState(group=make_group(files=(source,)))
    base = SessionState(root=Path("library"), groups=(group,))
    old = begin_operation(
        base,
        "LOOKUP-0001",
        OperationKind.LOOKUP,
        (group.group.group_id,),
    )
    envelope = make_group_envelope(old, make_derived_group_state(source=source))
    idle = finish_operation(old, "LOOKUP-0001")
    newer = begin_operation(
        idle,
        "LOOKUP-0002",
        OperationKind.LOOKUP,
        (group.group.group_id,),
    )

    rejected = apply_group_result(newer, envelope)

    assert rejected.status is ResultApplicationStatus.STALE
    assert rejected.reason is StaleResultReason.OPERATION_MISMATCH
    assert rejected.state is newer
    assert rejected.state.active_operation is newer.active_operation
