"""Pure session lookup controls over retained provider evidence and decisions."""

from dataclasses import replace

from metadata_polisher.application.lookup import build_release_search_query
from metadata_polisher.application.review import build_local_review_texts, rerank_field_review_state
from metadata_polisher.domain.matching import ReleaseSearchQuery
from metadata_polisher.domain.metadata import FieldReadState, MetadataField
from metadata_polisher.infrastructure.settings import RenameSettings
from metadata_polisher.session.review_editing import local_reviews_after_lookup_reset, rebuild_reviewed_files
from metadata_polisher.session.state import GroupState, ReviewedFileState, SessionState

_DEFAULT_RENAME_SETTINGS = RenameSettings()


def _group_for_edit(state: SessionState, group_id: str) -> GroupState:
    if state.active_operation is not None:
        raise ValueError("Wait for the current operation before editing lookup options.")

    current = next((item for item in state.groups if item.group.group_id == group_id), None)

    if current is None:
        raise ValueError("The selected group no longer exists.")

    return current


def _replace_group(state: SessionState, current: GroupState, replacement: GroupState) -> SessionState:
    return replace(
        state,
        groups=tuple(replacement if item is current else item for item in state.groups),
        revision=state.revision + 1,
    )


def change_language(
    state: SessionState,
    group_id: str,
    language: str,
    rename_settings: RenameSettings,
) -> SessionState:
    """Rerank loaded variants while preserving explicit decisions and provenance."""
    current = _group_for_edit(state, group_id)

    if not isinstance(language, str) or not language.strip():
        raise ValueError("Choose a non-blank language preference.")

    if not isinstance(rename_settings, RenameSettings):
        raise TypeError("rename_settings must be RenameSettings")

    requested = language.strip().casefold()

    if current.language_override == requested:
        return state

    # Language changes rerank already loaded variants locally. They do not fetch
    # new provider text or translate values absent from the retained evidence.
    local_texts = build_local_review_texts(current.group.files)
    language_profile_texts = tuple(
        text
        for field in (MetadataField.TITLE, MetadataField.ARTISTS, MetadataField.ALBUM, MetadataField.ALBUM_ARTISTS)
        for text in local_texts.get(field, ())
    )
    reviewed_files: list[ReviewedFileState] = []

    for reviewed in current.reviewed_files:
        if not reviewed.reviews:
            reviewed_files.append(reviewed)
            continue

        reviews = tuple(
            rerank_field_review_state(
                review,
                preferred_language=requested,
                local_texts=local_texts.get(review.field, ()),
                language_profile_texts=language_profile_texts,
            )
            for review in reviewed.reviews
        )
        # A different default value can change a filename or create a collision;
        # discard the old derived plan and rebuild it from the new decisions.
        reviewed_files.append(replace(reviewed, reviews=reviews, change_set=None))

    rename_decisions = {
        reviewed.file_id: reviewed.change_set.rename_decision
        for reviewed in current.reviewed_files
        if reviewed.change_set is not None
    }
    rebuilt_by_id = {
        reviewed.file_id: reviewed
        for reviewed in rebuild_reviewed_files(
            state,
            current,
            tuple(reviewed for reviewed in reviewed_files if reviewed.reviews),
            rename_settings,
            rename_decisions=rename_decisions,
        )
    }

    # The automatic receipt must stay an exact projection of its original
    # worker result. Human review creates a new projection over the same source
    # evidence, so retain that evidence and discard only the automatic receipt.
    replacement = replace(
        current,
        language_override=requested,
        reviewed_files=tuple(rebuilt_by_id.get(reviewed.file_id, reviewed) for reviewed in reviewed_files),
        selected_metadata=None,
        revision=current.revision + 1,
    )

    return _replace_group(state, current, replacement)


def set_search_query_override(
    state: SessionState,
    group_id: str,
    query: ReleaseSearchQuery | None,
    rename_settings: RenameSettings = _DEFAULT_RENAME_SETTINGS,
) -> SessionState:
    """Store session-only search terms and invalidate results of the old query."""
    current = _group_for_edit(state, group_id)

    if query is not None and not isinstance(query, ReleaseSearchQuery):
        raise TypeError("query must be ReleaseSearchQuery or None")

    if current.search_query_override == query:
        return state

    # New search terms invalidate the selected release and its track evidence.
    # Independent local decisions survive through the dedicated review transform.
    replacement = replace(
        current,
        search_query_override=query,
        lookup_result=None,
        release_ranking=None,
        candidate_lookup=None,
        selected_release=None,
        automatic_track_mapping=None,
        manual_track_mapping=None,
        reviewed_files=local_reviews_after_lookup_reset(state, current, rename_settings),
        selected_metadata=None,
        selection_failure=None,
        revision=current.revision + 1,
    )

    return _replace_group(state, current, replacement)


def is_group_searchable(group: GroupState) -> bool:
    """Require usable local or edited evidence before offering a provider lookup."""
    if group.requires_rescan:
        return False

    if not any(
        state in (FieldReadState.PRESENT, FieldReadState.MISSING)
        for file in group.group.files
        for state in file.read_result.field_states.values()
    ):
        return False

    query = group.search_query_override or build_release_search_query(group.group)

    # Track count alone is too broad for a useful user-triggered search. Keep
    # explicit edited album terms usable even when all local tags are missing.
    return bool(query.album or query.artists or query.distinctive_titles)


def incomplete_searchable_group_ids(state: SessionState) -> tuple[str, ...]:
    """Select supported incomplete groups in stable visible order."""
    return tuple(
        group.group.group_id
        for group in state.groups
        if is_group_searchable(group)
        and any(
            read_state is FieldReadState.MISSING
            for file in group.group.files
            for read_state in file.read_result.field_states.values()
        )
    )


def available_language_choices(group: GroupState | None) -> tuple[tuple[str, str], ...]:
    """Expose supplied variants, including declared romanisation, without inventing text."""
    languages: set[str] = set()

    if group is not None and group.lookup_result is not None:
        for item in group.lookup_result.candidates:
            variants = (*item.candidate.titles, *(
                title for medium in item.candidate.media for track in medium.tracks for title in track.titles
            ))
            languages.update(title.language for title in variants if title.language)

            # VGMdb declares romanised Japanese as ja + Latn, retaining its
            # language. Offer the existing script preference for those supplied
            # variants instead of requiring a fictitious romanisation language.
            if any(
                title.language in {"ja", "jpn", "ko", "kor", "zh", "zho"}
                and title.script == "Latn"
                for title in variants
            ):
                languages.add("romanised")

    if group is not None and group.language_override:
        languages.add(group.language_override)

    labels = {
        "ja": "Japanese", "jpn": "Japanese", "en": "English", "eng": "English",
        "rom": "Romanised", "romanised": "Romanised",
    }
    return (("auto", "Auto"), *((value, labels.get(value, value)) for value in sorted(languages - {"auto"})))
