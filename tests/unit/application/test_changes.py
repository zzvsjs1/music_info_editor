# Build plans from complete review states so each case can vary one decision.
# The assertions distinguish preserved values, explicit clears and actual writes.

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from metadata_polisher.application.changes import (
    ChangeIssueCode,
    ChangeIssueSeverity,
    ChangeSetStatus,
    ChangeValidationFacts,
    FileChangeSet,
    RenameChange,
    RenameDecision,
    build_change_set,
)
from metadata_polisher.application.review import (
    build_field_review_state,
    set_clear_decision,
    set_keep_existing_decision,
    set_manual_decision,
    set_proposal_decision,
)
from metadata_polisher.domain.matching import MetadataProvenance
from metadata_polisher.domain.media import FilenameHints, LocalMediaFile, MediaReadResult, StreamInfo
from metadata_polisher.domain.metadata import FieldReadState, MetadataField, MetadataSnapshot, Position
from metadata_polisher.domain.review import FieldConfidence, FieldProposal, FieldReviewState, ReviewReasonCode
from metadata_polisher.rename.template import FilenameRenderPolicy
from metadata_polisher.rename.windows import FilenameIssueCode


def metadata_value(metadata: MetadataSnapshot, field: MetadataField) -> object:
    return {
        MetadataField.TITLE: metadata.title,
        MetadataField.ARTISTS: metadata.artists,
        MetadataField.ALBUM: metadata.album,
        MetadataField.ALBUM_ARTISTS: metadata.album_artists,
        MetadataField.COMPOSERS: metadata.composers,
        MetadataField.TRACK: metadata.track,
        MetadataField.DISC: metadata.disc,
        MetadataField.DATE: metadata.date,
        MetadataField.GENRES: metadata.genres,
    }[field]


def has_value(value: object) -> bool:
    if value is None:
        return False

    if isinstance(value, tuple):
        return bool(value)

    if isinstance(value, Position):
        return value.number is not None or value.total is not None

    return True


def make_source(
    *,
    name: str = "old-name.flac",
    metadata: MetadataSnapshot | None = None,
    state_overrides: dict[MetadataField, FieldReadState] | None = None,
) -> LocalMediaFile:
    snapshot = metadata or MetadataSnapshot(
        title="Old title",
        artists=("Old artist",),
        album="Old album",
        album_artists=("Old album artist",),
        composers=("Old composer",),
        track=Position(number=2, total=12),
        disc=Position(number=1, total=1),
        date="2020",
        genres=("Old genre",),
    )
    states = {
        field: (
            FieldReadState.PRESENT
            if has_value(metadata_value(snapshot, field))
            else FieldReadState.MISSING
        )
        for field in MetadataField
    }
    states.update(state_overrides or {})

    return LocalMediaFile(
        path=Path("library") / "Album" / name,
        format_id="flac",
        read_result=MediaReadResult(
            metadata=snapshot,
            field_states=states,
            stream_info=StreamInfo(
                duration_seconds=180.0,
                sample_rate=48_000,
                channels=2,
                bit_depth=24,
                codec="FLAC",
            ),
        ),
        filename_hints=FilenameHints(),
        file_id="file-0001",
    )


def base_reviews(source: LocalMediaFile) -> tuple[FieldReviewState, ...]:
    # Read-state flags control whether an existing value is usable; a stored value
    # beside an unreadable flag must not accidentally become a normal present field.
    reviews: list[FieldReviewState] = []

    for field in MetadataField:
        read_state = source.read_result.field_states[field]
        source_value = metadata_value(source.read_result.metadata, field)
        existing_value = (
            source_value
            if read_state is not FieldReadState.MISSING and has_value(source_value)
            else None
        )
        reviews.append(
            build_field_review_state(
                field=field,
                read_state=read_state,
                existing_value=existing_value,  # type: ignore[arg-type]
                proposals=(),
            )
        )

    return tuple(reviews)


def replace_review(
    reviews: tuple[FieldReviewState, ...],
    replacement: FieldReviewState,
) -> tuple[FieldReviewState, ...]:
    return tuple(replacement if review.field is replacement.field else review for review in reviews)


def field_proposal(field: MetadataField, value: object) -> FieldProposal:
    return FieldProposal(
        field=field,
        value=value,  # type: ignore[arg-type]
        confidence=FieldConfidence.HIGH,
        provenance=MetadataProvenance(
            engine_id="direct",
            source_id="catalogue",
            record_id="release-1",
            source_url="https://catalogue.invalid/release-1",
            language="eng",
            operation_id="LOOKUP-0001",
        ),
        language="eng",
        script="Latn",
        reason_codes=(ReviewReasonCode.FIELD_MATCH_HIGH,),
    )


def proposed_review(
    source: LocalMediaFile,
    field: MetadataField,
    value: object,
) -> FieldReviewState:
    read_state = source.read_result.field_states[field]
    existing = metadata_value(source.read_result.metadata, field)
    existing_value = (
        existing
        if read_state is not FieldReadState.MISSING and has_value(existing)
        else None
    )

    return build_field_review_state(
        field=field,
        read_state=read_state,
        existing_value=existing_value,  # type: ignore[arg-type]
        proposals=(field_proposal(field, value),),
        preferred_language="eng",
    )


def selected_proposed_review(
    source: LocalMediaFile,
    field: MetadataField,
    value: object,
) -> FieldReviewState:
    review = proposed_review(source, field, value)

    return set_proposal_decision(review, review.proposals[0])


def test_final_metadata_is_freshly_derived_from_every_review_decision() -> None:
    source = make_source()
    reviews = base_reviews(source)
    title = set_manual_decision(reviews[0], "Reviewed title")
    artists = set_clear_decision(reviews[1])
    composers = selected_proposed_review(
        source,
        MetadataField.COMPOSERS,
        ("New composer",),
    )
    track = selected_proposed_review(source, MetadataField.TRACK, Position(number=3, total=None))
    disc = set_manual_decision(reviews[6], Position(number=None, total=2))
    date = set_clear_decision(reviews[7])

    for replacement in (title, artists, composers, track, disc, date):
        reviews = replace_review(reviews, replacement)

    change_set = build_change_set(
        source,
        reviews,
        RenameDecision.KEEP_FILENAME,
        validation=ChangeValidationFacts(),
    )

    assert change_set.final_metadata == MetadataSnapshot(
        title="Reviewed title",
        artists=(),
        album="Old album",
        album_artists=("Old album artist",),
        composers=("New composer",),
        track=Position(number=3, total=12),
        disc=Position(number=1, total=2),
        date=None,
        genres=("Old genre",),
    )
    assert tuple(change.field for change in change_set.metadata_changes) == (
        MetadataField.TITLE,
        MetadataField.ARTISTS,
        MetadataField.COMPOSERS,
        MetadataField.TRACK,
        MetadataField.DISC,
        MetadataField.DATE,
    )
    assert change_set.metadata_changes[3].old_value == Position(number=2, total=12)
    assert change_set.metadata_changes[3].new_value == Position(number=3, total=12)
    assert change_set.rename_change is None
    assert change_set.status is ChangeSetStatus.VALID

    # All inputs remain usable as the immutable source of a later rebuild.
    assert source.read_result.metadata.track == Position(number=2, total=12)
    assert track.selected_proposal is not None
    assert track.selected_proposal.value == Position(number=3, total=None)


def test_partial_position_overlay_preserves_each_existing_component_independently() -> None:
    source = make_source()
    reviews = base_reviews(source)
    track_number_only = selected_proposed_review(
        source,
        MetadataField.TRACK,
        Position(number=4, total=None),
    )
    disc_total_only = set_manual_decision(
        reviews[6],
        Position(number=None, total=3),
    )
    reviews = replace_review(reviews, track_number_only)
    reviews = replace_review(reviews, disc_total_only)

    change_set = build_change_set(source, reviews, RenameDecision.KEEP_FILENAME)

    assert change_set.final_metadata.track == Position(number=4, total=12)
    assert change_set.final_metadata.disc == Position(number=1, total=3)


def test_explicit_position_clear_removes_both_number_and_total() -> None:
    source = make_source()
    reviews = replace_review(
        base_reviews(source),
        set_clear_decision(base_reviews(source)[5]),
    )

    change_set = build_change_set(source, reviews, RenameDecision.KEEP_FILENAME)

    assert change_set.final_metadata.track == Position()
    assert len(change_set.metadata_changes) == 1
    assert change_set.metadata_changes[0].field is MetadataField.TRACK
    assert change_set.metadata_changes[0].new_value == Position()


def test_rebuilding_from_changed_review_state_does_not_patch_a_stale_change_set() -> None:
    source = make_source()
    original_reviews = base_reviews(source)
    first_reviews = replace_review(
        original_reviews,
        set_manual_decision(original_reviews[0], "First choice"),
    )
    second_reviews = replace_review(
        original_reviews,
        set_manual_decision(original_reviews[0], "Second choice"),
    )

    first = build_change_set(source, first_reviews, RenameDecision.KEEP_FILENAME)
    second = build_change_set(source, second_reviews, RenameDecision.KEEP_FILENAME)

    assert first.final_metadata.title == "First choice"
    assert second.final_metadata.title == "Second choice"
    assert first.metadata_changes[0].new_value == "First choice"
    assert source.read_result.metadata.title == "Old title"


def test_rename_preview_uses_final_reviewed_disc_decision() -> None:
    source = make_source(
        name="raw-name.flac",
        metadata=MetadataSnapshot(
            title="Emblem Engage!",
            track=Position(number=1, total=10),
        ),
    )
    original_reviews = base_reviews(source)
    proposed_disc = proposed_review(source, MetadataField.DISC, Position(number=1, total=1))
    accepted_reviews = replace_review(original_reviews, proposed_disc)
    rejected_reviews = replace_review(
        original_reviews,
        set_keep_existing_decision(proposed_disc),
    )
    facts = ChangeValidationFacts(existing_names=(source.path.name,))

    accepted = build_change_set(
        source,
        accepted_reviews,
        RenameDecision.APPLY_RENAME,
        validation=facts,
    )
    rejected = build_change_set(
        source,
        rejected_reviews,
        RenameDecision.APPLY_RENAME,
        validation=facts,
    )

    assert accepted.final_metadata.disc == Position(number=1, total=1)
    assert accepted.rename_change is not None
    assert accepted.rename_change.new_path.name == "1.01. Emblem Engage!.flac"
    assert rejected.final_metadata.disc == Position()
    assert rejected.rename_change is not None
    assert rejected.rename_change.new_path.name == "01. Emblem Engage!.flac"


def test_rename_preserves_source_extension_and_uses_the_injected_render_policy() -> None:
    source = make_source(
        name="source.FLAC",
        metadata=MetadataSnapshot(
            title="Finale",
            track=Position(number=2),
        ),
    )

    change_set = build_change_set(
        source,
        base_reviews(source),
        RenameDecision.APPLY_RENAME,
        rename_policy=FilenameRenderPolicy(minimum_track_digits=3),
        validation=ChangeValidationFacts(existing_names=(source.path.name,)),
    )

    assert change_set.rename_change is not None
    assert change_set.rename_change.new_path.name == "002. Finale.FLAC"


def test_keep_filename_retains_a_pure_preview_but_never_requests_a_rename() -> None:
    source = make_source(
        metadata=MetadataSnapshot(
            title="Finale",
            track=Position(number=2),
        )
    )

    change_set = build_change_set(
        source,
        base_reviews(source),
        RenameDecision.KEEP_FILENAME,
        validation=ChangeValidationFacts(existing_names=(source.path.name,)),
    )

    assert change_set.rename_preview is not None
    assert change_set.rename_preview.new_path.name == "02. Finale.flac"
    assert change_set.rename_change is None


def issue_codes(change_set: FileChangeSet) -> tuple[ChangeIssueCode, ...]:
    return tuple(issue.code for issue in change_set.validation.issues)


def test_destination_collision_is_a_structured_blocker_without_inventing_a_name() -> None:
    source = make_source(
        metadata=MetadataSnapshot(
            title="Finale",
            track=Position(number=2),
        )
    )
    change_set = build_change_set(
        source,
        base_reviews(source),
        RenameDecision.APPLY_RENAME,
        validation=ChangeValidationFacts(
            existing_names=(source.path.name, "02. Finale.flac"),
        ),
    )

    assert change_set.status is ChangeSetStatus.BLOCKED
    assert change_set.rename_preview is not None
    assert change_set.rename_preview.new_path.name == "02. Finale.flac"
    assert change_set.rename_change is None
    assert issue_codes(change_set) == (ChangeIssueCode.DESTINATION_COLLISION,)


def test_control_character_in_rendered_filename_is_a_structured_blocker() -> None:
    source = make_source(
        metadata=MetadataSnapshot(
            title="Bad\x00title",
            track=Position(number=1),
        )
    )

    change_set = build_change_set(
        source,
        base_reviews(source),
        RenameDecision.APPLY_RENAME,
    )

    assert change_set.status is ChangeSetStatus.BLOCKED
    assert change_set.rename_preview is None
    assert change_set.rename_change is None
    issue = change_set.validation.issues[0]
    assert issue.code is ChangeIssueCode.INVALID_DESTINATION_FILENAME
    assert issue.filename_issue_code is FilenameIssueCode.CONTROL_CHARACTER


def test_invalid_template_is_a_structured_blocker_when_rename_is_requested() -> None:
    source = make_source()

    change_set = build_change_set(
        source,
        base_reviews(source),
        RenameDecision.APPLY_RENAME,
        template="[%unknown%]",
    )

    assert change_set.status is ChangeSetStatus.BLOCKED
    assert issue_codes(change_set) == (ChangeIssueCode.INVALID_RENAME_TEMPLATE,)


def test_template_with_no_usable_stem_is_an_invalid_destination() -> None:
    source = make_source()

    change_set = build_change_set(
        source,
        base_reviews(source),
        RenameDecision.APPLY_RENAME,
        template="   ",
    )

    assert change_set.status is ChangeSetStatus.BLOCKED
    assert change_set.rename_preview is None
    assert issue_codes(change_set) == (ChangeIssueCode.INVALID_DESTINATION_FILENAME,)


def test_keep_filename_is_not_blocked_by_hypothetical_invalid_or_colliding_preview() -> None:
    source = make_source(
        metadata=MetadataSnapshot(
            title="Finale",
            track=Position(number=2),
        )
    )
    invalid = build_change_set(
        source,
        base_reviews(source),
        RenameDecision.KEEP_FILENAME,
        template="[%unknown%]",
    )
    colliding = build_change_set(
        source,
        base_reviews(source),
        RenameDecision.KEEP_FILENAME,
        validation=ChangeValidationFacts(existing_names=("02. Finale.flac",)),
    )

    assert invalid.status is ChangeSetStatus.VALID_WITH_WARNINGS
    assert colliding.status is ChangeSetStatus.VALID_WITH_WARNINGS
    assert all(
        issue.severity is ChangeIssueSeverity.WARNING
        for result in (invalid, colliding)
        for issue in result.validation.issues
    )
    assert invalid.rename_change is None
    assert colliding.rename_change is None


def test_unsupported_requested_field_write_blocks_the_change_set() -> None:
    source = make_source()
    reviews = replace_review(
        base_reviews(source),
        set_manual_decision(base_reviews(source)[0], "New title"),
    )

    change_set = build_change_set(
        source,
        reviews,
        RenameDecision.KEEP_FILENAME,
        validation=ChangeValidationFacts(
            unsupported_write_fields=(MetadataField.TITLE,),
        ),
    )

    assert change_set.status is ChangeSetStatus.BLOCKED
    assert issue_codes(change_set) == (ChangeIssueCode.UNSUPPORTED_WRITE,)
    assert change_set.validation.issues[0].field is MetadataField.TITLE


def test_a_field_read_as_unsupported_cannot_be_explicitly_written() -> None:
    source = make_source(state_overrides={MetadataField.TITLE: FieldReadState.UNSUPPORTED})
    reviews = base_reviews(source)
    reviews = replace_review(reviews, set_manual_decision(reviews[0], "New title"))

    change_set = build_change_set(source, reviews, RenameDecision.KEEP_FILENAME)

    assert change_set.status is ChangeSetStatus.BLOCKED
    assert issue_codes(change_set) == (ChangeIssueCode.UNSUPPORTED_WRITE,)


def test_non_writable_directory_blocks_an_actual_metadata_change() -> None:
    source = make_source()
    reviews = replace_review(
        base_reviews(source),
        set_manual_decision(base_reviews(source)[0], "New title"),
    )

    change_set = build_change_set(
        source,
        reviews,
        RenameDecision.KEEP_FILENAME,
        validation=ChangeValidationFacts(directory_writable=False),
    )

    assert change_set.status is ChangeSetStatus.BLOCKED
    assert ChangeIssueCode.DIRECTORY_NOT_WRITABLE in issue_codes(change_set)


def test_unresolved_mapping_blocks_a_selected_track_specific_provider_change() -> None:
    source = make_source()
    reviews = replace_review(
        base_reviews(source),
        selected_proposed_review(source, MetadataField.TITLE, "Provider title"),
    )

    change_set = build_change_set(
        source,
        reviews,
        RenameDecision.KEEP_FILENAME,
        validation=ChangeValidationFacts(track_mapping_resolved=False),
    )

    assert change_set.status is ChangeSetStatus.BLOCKED
    assert issue_codes(change_set) == (ChangeIssueCode.UNRESOLVED_TRACK_MAPPING,)
    assert change_set.validation.issues[0].field is MetadataField.TITLE


def test_unresolved_optional_composer_is_preserved_as_a_non_blocking_warning() -> None:
    source = make_source(
        metadata=MetadataSnapshot(
            title="Old title",
            album="Old album",
            composers=(),
            track=Position(number=1),
        )
    )
    reviews = base_reviews(source)
    composer_review = build_field_review_state(
        field=MetadataField.COMPOSERS,
        read_state=FieldReadState.MISSING,
        existing_value=None,
        proposals=(
            field_proposal(MetadataField.COMPOSERS, ("First composer",)),
            FieldProposal(
                field=MetadataField.COMPOSERS,
                value=("Second composer",),
                confidence=FieldConfidence.HIGH,
                provenance=MetadataProvenance(
                    engine_id="second",
                    source_id="second-catalogue",
                    record_id="release-2",
                    source_url=None,
                    language="eng",
                    operation_id="LOOKUP-0001",
                ),
                language="eng",
                script="Latn",
            ),
        ),
        preferred_language="eng",
    )
    reviews = replace_review(reviews, composer_review)
    reviews = replace_review(reviews, set_manual_decision(reviews[2], "Reviewed album"))

    change_set = build_change_set(
        source,
        reviews,
        RenameDecision.KEEP_FILENAME,
        validation=ChangeValidationFacts(track_mapping_resolved=False),
    )

    assert composer_review.decision.value == "unresolved"
    assert change_set.final_metadata.composers == ()
    assert tuple(change.field for change in change_set.metadata_changes) == (MetadataField.ALBUM,)
    assert change_set.status is ChangeSetStatus.VALID_WITH_WARNINGS
    assert issue_codes(change_set) == (ChangeIssueCode.UNRESOLVED_FIELD_PRESERVED,)
    assert change_set.validation.issues[0].field is MetadataField.COMPOSERS


@pytest.mark.parametrize(
    ("facts", "expected_code"),
    (
        ({"source_readable": False}, ChangeIssueCode.SOURCE_NOT_READABLE),
        ({"adapter_available": False}, ChangeIssueCode.ADAPTER_UNAVAILABLE),
        (
            {"temporary_space_sufficient": False},
            ChangeIssueCode.INSUFFICIENT_TEMPORARY_SPACE,
        ),
    ),
)
def test_other_injected_preflight_failures_block_requested_changes(
    facts: dict[str, bool],
    expected_code: ChangeIssueCode,
) -> None:
    source = make_source()
    reviews = replace_review(
        base_reviews(source),
        set_manual_decision(base_reviews(source)[0], "New title"),
    )

    change_set = build_change_set(
        source,
        reviews,
        RenameDecision.KEEP_FILENAME,
        validation=ChangeValidationFacts(**facts),
    )

    assert change_set.status is ChangeSetStatus.BLOCKED
    assert expected_code in issue_codes(change_set)


def test_deterministic_filename_repair_is_a_warning_and_keeps_the_rename() -> None:
    source = make_source(
        metadata=MetadataSnapshot(
            title="Question?",
            track=Position(number=1),
        )
    )

    change_set = build_change_set(
        source,
        base_reviews(source),
        RenameDecision.APPLY_RENAME,
    )

    assert change_set.status is ChangeSetStatus.VALID_WITH_WARNINGS
    assert change_set.rename_change is not None
    assert change_set.rename_change.new_path.name == "01. Question？.flac"
    assert issue_codes(change_set) == (ChangeIssueCode.FILENAME_REPAIRED,)


def test_semantically_equal_selected_proposal_does_not_emit_a_metadata_change() -> None:
    source = make_source()
    same_title = selected_proposed_review(source, MetadataField.TITLE, "Old title")
    reviews = replace_review(base_reviews(source), same_title)

    change_set = build_change_set(source, reviews, RenameDecision.KEEP_FILENAME)

    assert change_set.metadata_changes == ()


def test_builder_requires_one_unique_review_for_every_managed_field() -> None:
    source = make_source()
    reviews = base_reviews(source)

    with pytest.raises(ValueError, match="cover every MetadataField"):
        build_change_set(source, reviews[:-1], RenameDecision.KEEP_FILENAME)

    with pytest.raises(ValueError, match="each MetadataField exactly once"):
        build_change_set(source, reviews + (reviews[0],), RenameDecision.KEEP_FILENAME)

    with pytest.raises(TypeError, match="ordered sequence"):
        build_change_set(source, set(reviews), RenameDecision.KEEP_FILENAME)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="ordered sequence"):
        build_change_set(
            source,
            (review for review in reviews),  # type: ignore[arg-type]
            RenameDecision.KEEP_FILENAME,
        )


def test_review_input_order_cannot_change_metadata_change_order() -> None:
    source = make_source()
    reviews = base_reviews(source)
    reviews = replace_review(reviews, set_manual_decision(reviews[7], "2024"))
    reviews = replace_review(reviews, set_manual_decision(reviews[0], "New title"))

    forwards = build_change_set(source, reviews, RenameDecision.KEEP_FILENAME)
    backwards = build_change_set(source, tuple(reversed(reviews)), RenameDecision.KEEP_FILENAME)

    assert forwards == backwards
    assert tuple(change.field for change in forwards.metadata_changes) == (
        MetadataField.TITLE,
        MetadataField.DATE,
    )


def test_validation_facts_reject_bool_integer_confusion_and_unordered_collections() -> None:
    with pytest.raises(TypeError, match="directory_writable must be a bool"):
        ChangeValidationFacts(directory_writable=1)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="ordered sequence"):
        ChangeValidationFacts(
            unsupported_write_fields={MetadataField.TITLE},  # type: ignore[arg-type]
        )

    with pytest.raises(ValueError, match="must be unique"):
        ChangeValidationFacts(
            unsupported_write_fields=(MetadataField.TITLE, MetadataField.TITLE),
        )

    with pytest.raises(TypeError, match="ordered sequence"):
        ChangeValidationFacts(existing_names="song.flac")  # type: ignore[arg-type]


def test_validation_facts_copy_mutable_sequences_and_change_contracts_are_frozen() -> None:
    existing_names = ["old-name.flac"]
    facts = ChangeValidationFacts(existing_names=existing_names)  # type: ignore[arg-type]
    existing_names.append("later.flac")
    source = make_source()
    change_set = build_change_set(
        source,
        base_reviews(source),
        RenameDecision.KEEP_FILENAME,
        validation=facts,
    )

    assert facts.existing_names == ("old-name.flac",)

    with pytest.raises(FrozenInstanceError):
        change_set.file_id = "changed"  # type: ignore[misc]

    with pytest.raises(ValueError, match="source directory"):
        RenameChange(
            old_path=Path("library/old.flac"),
            new_path=Path("elsewhere/new.flac"),
        )

    with pytest.raises(TypeError, match="RenameDecision"):
        build_change_set(
            source,
            base_reviews(source),
            "keep_filename",  # type: ignore[arg-type]
        )


def test_selected_disc_proposal_does_not_depend_on_per_file_track_mapping() -> None:
    source = make_source()
    reviews = replace_review(
        base_reviews(source),
        selected_proposed_review(
            source,
            MetadataField.DISC,
            Position(number=2, total=2),
        ),
    )

    change_set = build_change_set(
        source,
        reviews,
        RenameDecision.KEEP_FILENAME,
        validation=ChangeValidationFacts(track_mapping_resolved=False),
    )

    assert change_set.final_metadata.disc == Position(number=2, total=2)
    assert ChangeIssueCode.UNRESOLVED_TRACK_MAPPING not in issue_codes(change_set)
    assert change_set.status is ChangeSetStatus.VALID


def test_apply_rename_to_current_path_is_a_no_op_not_a_preflight_operation() -> None:
    source = make_source(name="1.02. Old title.flac")

    change_set = build_change_set(
        source,
        base_reviews(source),
        RenameDecision.APPLY_RENAME,
        validation=ChangeValidationFacts(
            source_readable=False,
            directory_writable=False,
            adapter_available=False,
            temporary_space_sufficient=False,
            existing_names=(source.path.name,),
        ),
    )

    assert change_set.metadata_changes == ()
    assert change_set.rename_preview is None
    assert change_set.rename_change is None
    assert change_set.validation.issues == ()
    assert change_set.status is ChangeSetStatus.VALID


def test_builder_rejects_review_state_from_another_source_snapshot() -> None:
    reviewed_source = make_source(metadata=MetadataSnapshot(title="Reviewed source"))
    supplied_source = make_source(metadata=MetadataSnapshot(title="Different source"))

    with pytest.raises(ValueError, match="existing_value"):
        build_change_set(
            supplied_source,
            base_reviews(reviewed_source),
            RenameDecision.KEEP_FILENAME,
        )


def test_builder_rejects_review_read_state_that_does_not_match_source_read_state() -> None:
    source = make_source()
    reviews = base_reviews(source)
    foreign_title = build_field_review_state(
        field=MetadataField.TITLE,
        read_state=FieldReadState.MISSING,
        existing_value=None,
        proposals=(),
    )

    with pytest.raises(ValueError, match="read_state"):
        build_change_set(
            source,
            replace_review(reviews, foreign_title),
            RenameDecision.KEEP_FILENAME,
        )
