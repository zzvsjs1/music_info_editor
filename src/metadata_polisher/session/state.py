"""Validated immutable session state for UI-thread ownership."""

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import cast

from metadata_polisher.application.apply import ApplyBatchResult
from metadata_polisher.application.changes import FileChangeSet, RenameDecision, build_change_set
from metadata_polisher.application.lookup import (
    CandidateLookupResult,
    GroupLookupResult,
    LookupSearchResult,
    SelectedMetadataResult,
)
from metadata_polisher.application.scanning import ScanLibraryResult
from metadata_polisher.domain.errors import Issue
from metadata_polisher.domain.matching import ReleaseCandidate, ReleaseMedium, ReleaseSearchQuery
from metadata_polisher.domain.media import LocalMediaFile, UnsupportedMediaFile
from metadata_polisher.domain.metadata import (
    FieldReadState,
    MetadataField,
    MetadataSnapshot,
)
from metadata_polisher.domain.review import (
    FieldProposal,
    FieldReviewState,
    FieldValue,
)
from metadata_polisher.infrastructure.settings import RenameSettings
from metadata_polisher.matching.release_scoring import ReleaseRanking
from metadata_polisher.matching.track_mapping import TrackMappingResult
from metadata_polisher.providers.cache import MemoryCache
from metadata_polisher.providers.coordinator import CoordinatedCandidate, is_valid_basic_media
from metadata_polisher.scanner.grouping import AlbumGroup, GroupingWarning, derive_album_title

type ProviderCacheKey = tuple[str, ...]
type ProviderCacheValue = ReleaseCandidate | tuple[ReleaseCandidate, ...]
type ReleaseMediumIdentity = tuple[str, str, str, int]

_DEFAULT_RENAME_SETTINGS = RenameSettings()


class OperationKind(StrEnum):
    """Major background operation categories visible to session coordination."""

    SCAN = "scan"
    LOOKUP = "lookup"
    APPLY = "apply"
    PROVIDER_TEST = "provider_test"


def _validate_non_blank(name: str, value: object) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")

    if not value.strip():
        raise ValueError(f"{name} must be non-blank")


def _validate_revision(name: str, value: object) -> None:
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer")

    if value < 0:
        raise ValueError(f"{name} cannot be negative")


def _copy_typed_sequence[T](
    name: str,
    values: object,
    item_type: type[T],
) -> tuple[T, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError(f"{name} must be an ordered sequence")

    copied = tuple(values)

    if any(not isinstance(value, item_type) for value in copied):
        raise TypeError(f"{name} must contain only {item_type.__name__} values")

    return cast(tuple[T, ...], copied)


def _default_provider_cache() -> MemoryCache[ProviderCacheKey, ProviderCacheValue]:
    return MemoryCache(capacity=256)


@dataclass(frozen=True)
class GroupSelection:
    """Visible selection of one supported album group."""

    group_id: str

    def __post_init__(self) -> None:
        _validate_non_blank("group_id", self.group_id)


@dataclass(frozen=True)
class UnsupportedSelection:
    """Visible selection of the unsupported-files collection."""


type SessionSelection = GroupSelection | UnsupportedSelection | None


@dataclass(frozen=True)
class GroupVersion:
    """One group identity paired with its captured semantic revision."""

    group_id: str
    revision: int

    def __post_init__(self) -> None:
        _validate_non_blank("group_id", self.group_id)
        _validate_revision("revision", self.revision)


@dataclass(frozen=True)
class ActiveOperation:
    """Identity and captured scope of the one active background operation."""

    operation_id: str
    kind: OperationKind
    base_session_revision: int
    base_library_revision: int
    target_groups: tuple[GroupVersion, ...] = ()

    def __post_init__(self) -> None:
        _validate_non_blank("operation_id", self.operation_id)

        if not isinstance(self.kind, OperationKind):
            raise TypeError("kind must be an OperationKind")

        _validate_revision("base_session_revision", self.base_session_revision)
        _validate_revision("base_library_revision", self.base_library_revision)

        if self.base_library_revision > self.base_session_revision:
            raise ValueError("base_library_revision cannot exceed base_session_revision")

        targets = _copy_typed_sequence("target_groups", self.target_groups, GroupVersion)
        target_ids = tuple(target.group_id for target in targets)

        if len(target_ids) != len(set(target_ids)):
            raise ValueError("operation target group IDs must be unique")

        if self.kind is OperationKind.SCAN and targets:
            raise ValueError("a scan operation must not target groups")

        if self.kind in {OperationKind.LOOKUP, OperationKind.APPLY} and not targets:
            raise ValueError(f"a {self.kind.value} operation must target at least one group")

        object.__setattr__(self, "target_groups", targets)


@dataclass(frozen=True)
class ReleaseSelectionState:
    """The enriched selected release and its exact medium."""

    candidate: CoordinatedCandidate
    medium_index: int

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, CoordinatedCandidate):
            raise TypeError("candidate must be a CoordinatedCandidate")

        if type(self.medium_index) is not int:
            raise TypeError("medium_index must be an integer")

        media = self.candidate.candidate.media

        if not media:
            raise ValueError("selected candidate must be enriched with media")

        if not 0 <= self.medium_index < len(media):
            raise ValueError("medium_index must identify one selected candidate medium")

    @property
    def identity(self) -> ReleaseMediumIdentity:
        """Return the stable catalogue and medium identity used by ranking."""
        release = self.candidate.candidate

        return (
            release.engine_id,
            release.source_id,
            release.release_id,
            self.medium_index,
        )

    @property
    def medium(self) -> ReleaseMedium:
        """Return the selected enriched medium without another lookup."""
        return self.candidate.candidate.media[self.medium_index]


def _metadata_value(metadata: MetadataSnapshot, field_name: MetadataField) -> FieldValue | None:
    values: dict[MetadataField, FieldValue | None] = {
        MetadataField.TITLE: metadata.title,
        MetadataField.ARTISTS: metadata.artists,
        MetadataField.ALBUM: metadata.album,
        MetadataField.ALBUM_ARTISTS: metadata.album_artists,
        MetadataField.COMPOSERS: metadata.composers,
        MetadataField.TRACK: metadata.track,
        MetadataField.DISC: metadata.disc,
        MetadataField.DATE: metadata.date,
        MetadataField.GENRES: metadata.genres,
    }

    return values[field_name]


def _has_semantic_value(value: FieldValue | None) -> bool:
    if value is None:
        return False

    if isinstance(value, str):
        return bool(value.strip())

    if isinstance(value, tuple):
        return bool(value)

    return value.number is not None or value.total is not None


def _validate_reviews_against_source(
    source: LocalMediaFile,
    reviews: tuple[FieldReviewState, ...],
) -> None:
    for review in reviews:
        source_read_state = source.read_result.field_states[review.field]

        if review.read_state is not source_read_state:
            raise ValueError(
                f"review read state for {review.field.value} does not match the current source"
            )

        source_value = _metadata_value(source.read_result.metadata, review.field)

        if source_read_state is FieldReadState.MISSING:
            expected_existing: FieldValue | None = None
        elif _has_semantic_value(source_value):
            expected_existing = source_value
        elif source_read_state is FieldReadState.PRESENT:
            raise ValueError(
                f"present source field {review.field.value} has no semantic value"
            )
        else:
            expected_existing = None

        if review.existing_value != expected_existing:
            raise ValueError(
                f"review existing value for {review.field.value} does not match the current source"
            )


def _validate_change_set_against_source(
    source: LocalMediaFile,
    reviews: tuple[FieldReviewState, ...],
    change_set: FileChangeSet,
) -> None:
    if change_set.file_id != source.file_id:
        raise ValueError("change set file ID does not match the reviewed file ID")

    rebuilt = build_change_set(
        source,
        reviews,
        RenameDecision.KEEP_FILENAME,
    )

    if change_set.final_metadata != rebuilt.final_metadata:
        raise ValueError("change set final metadata does not derive from the stored review decisions")

    if change_set.metadata_changes != rebuilt.metadata_changes:
        raise ValueError("change set metadata does not align with the current source snapshot")

    for rename in (change_set.rename_preview, change_set.rename_change):
        if rename is not None and rename.old_path != source.path:
            raise ValueError("change set rename does not align with the current source path")


@dataclass(frozen=True)
class ReviewedFileState:
    """Provider proposals, decisions, and derived changes for one group file."""

    file_id: str
    proposals: tuple[FieldProposal, ...] = ()
    reviews: tuple[FieldReviewState, ...] = ()
    track_mapping_resolved: bool = True
    change_set: FileChangeSet | None = None

    def __post_init__(self) -> None:
        _validate_non_blank("file_id", self.file_id)
        proposals = _copy_typed_sequence("proposals", self.proposals, FieldProposal)
        reviews = _copy_typed_sequence("reviews", self.reviews, FieldReviewState)
        review_fields = tuple(review.field for review in reviews)

        if len(review_fields) != len(set(review_fields)):
            raise ValueError("reviews must contain each MetadataField at most once")

        if reviews and frozenset(review_fields) != frozenset(MetadataField):
            raise ValueError("reviews must contain every MetadataField when present")

        field_order = {field_name: index for index, field_name in enumerate(MetadataField)}
        reviews = tuple(sorted(reviews, key=lambda review: field_order[review.field]))
        retained_proposals = set(proposals)
        review_proposals = tuple(
            member
            for review in reviews
            for consolidated in review.proposals
            for member in consolidated.members
        )

        if len(proposals) != len(retained_proposals):
            raise ValueError("proposals must be unique")

        if len(review_proposals) != len(set(review_proposals)):
            raise ValueError("a proposal cannot belong to more than one reviewed choice")

        if retained_proposals != set(review_proposals):
            raise ValueError("file proposals and reviewed proposals must match exactly")

        if type(self.track_mapping_resolved) is not bool:
            raise TypeError("track_mapping_resolved must be a bool")

        if self.change_set is not None:
            if not isinstance(self.change_set, FileChangeSet):
                raise TypeError("change_set must be a FileChangeSet or None")

            if not reviews:
                raise ValueError("a change set requires complete review state")

            if self.change_set.file_id != self.file_id:
                raise ValueError("change set file ID must match file_id")

        object.__setattr__(self, "proposals", proposals)
        object.__setattr__(self, "reviews", reviews)


def _validate_optional_non_blank(name: str, value: object) -> None:
    if value is not None:
        _validate_non_blank(name, value)


@dataclass(frozen=True)
class GroupState:
    """One album group and every downstream result derived from its files."""

    group: AlbumGroup
    warnings: tuple[GroupingWarning, ...] = ()
    lookup_result: LookupSearchResult | None = None
    release_ranking: ReleaseRanking | None = None
    candidate_lookup: CandidateLookupResult | None = None
    selected_release: ReleaseSelectionState | None = None
    automatic_track_mapping: TrackMappingResult | None = None
    reviewed_files: tuple[ReviewedFileState, ...] = ()
    selected_metadata: SelectedMetadataResult | None = None
    selection_failure: SelectedMetadataResult | None = None
    language_override: str | None = None
    disc_number_override: int | None = None
    search_query_override: ReleaseSearchQuery | None = None
    requires_rescan: bool = False
    revision: int = 0
    manual_track_mapping: TrackMappingResult | None = None
    search_failure: CandidateLookupResult | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.group, AlbumGroup):
            raise TypeError("group must be an AlbumGroup")

        warnings = _copy_typed_sequence("warnings", self.warnings, GroupingWarning)
        reviewed_files = _copy_typed_sequence(
            "reviewed_files",
            self.reviewed_files,
            ReviewedFileState,
        )
        # Validate ownership before accepting any derived data. Matching file
        # IDs alone are insufficient if the review's source values have changed.
        group_file_by_id = {file.file_id: file for file in self.group.files}

        for warning in warnings:
            if warning.group_id != self.group.group_id:
                raise ValueError("every warning must refer to this group")

            if not set(warning.affected_file_ids) <= group_file_by_id.keys():
                raise ValueError("warning affected file IDs must belong to this group")

        reviewed_file_ids = tuple(item.file_id for item in reviewed_files)

        if len(reviewed_file_ids) != len(set(reviewed_file_ids)):
            raise ValueError("reviewed file IDs must be unique")

        for reviewed_file in reviewed_files:
            source = group_file_by_id.get(reviewed_file.file_id)

            if source is None:
                raise ValueError("every reviewed file ID must belong to this group")

            _validate_reviews_against_source(source, reviewed_file.reviews)

            if reviewed_file.change_set is not None:
                _validate_change_set_against_source(
                    source,
                    reviewed_file.reviews,
                    reviewed_file.change_set,
                )

        # Canonicalise reviews to source-file order so widgets and later batch
        # requests see the same order regardless of how callers supplied them.
        if reviewed_files:
            reviewed_by_id = {item.file_id: item for item in reviewed_files}
            reviewed_files = tuple(
                reviewed_by_id[file.file_id]
                for file in self.group.files
                if file.file_id in reviewed_by_id
            )

        if self.lookup_result is not None:
            if not isinstance(self.lookup_result, LookupSearchResult):
                raise TypeError("lookup_result must be a LookupSearchResult or None")

            if self.lookup_result.group_id != self.group.group_id:
                raise ValueError("lookup result must refer to this group")

        if self.candidate_lookup is not None:
            if not isinstance(self.candidate_lookup, CandidateLookupResult):
                raise TypeError("candidate_lookup must be a CandidateLookupResult or None")

            if (
                self.lookup_result != self.candidate_lookup.lookup_result
                or self.release_ranking != self.candidate_lookup.release_ranking
            ):
                raise ValueError(
                    "candidate_lookup must exactly retain the stored lookup result and ranking"
                )

        if self.release_ranking is not None:
            if not isinstance(self.release_ranking, ReleaseRanking):
                raise TypeError("release_ranking must be a ReleaseRanking or None")

            if self.lookup_result is None:
                raise ValueError("release ranking requires a lookup result")

            candidate_identity_sequence = tuple(
                (
                    item.candidate.engine_id,
                    item.candidate.source_id,
                    item.candidate.release_id,
                )
                for item in self.lookup_result.candidates
            )

            if len(candidate_identity_sequence) != len(set(candidate_identity_sequence)):
                raise ValueError("lookup candidate identities must be unique")

            candidates_by_identity = {
                identity: item.candidate
                for identity, item in zip(
                    candidate_identity_sequence,
                    self.lookup_result.candidates,
                    strict=True,
                )
            }
            ranking_identities = tuple(entry.identity for entry in self.release_ranking.entries)

            # Each ranked medium must come from the exact retained candidate,
            # not just reuse its catalogue ID with different tracks or positions.
            for entry in self.release_ranking.entries:
                candidate = candidates_by_identity.get(entry.identity[:3])

                if candidate is None or entry.release != candidate:
                    raise ValueError("release ranking entries must equal their lookup candidates")

                if (
                    entry.medium_index >= len(candidate.media)
                    or entry.medium != candidate.media[entry.medium_index]
                ):
                    raise ValueError("release ranking media must equal their indexed lookup media")

            expected_ranking_identities = {
                (
                    candidate.engine_id,
                    candidate.source_id,
                    candidate.release_id,
                    medium_index,
                )
                for candidate in candidates_by_identity.values()
                if is_valid_basic_media(candidate)
                for medium_index, _medium in enumerate(candidate.media)
            }

            if (
                len(ranking_identities) != len(set(ranking_identities))
                or set(ranking_identities) != expected_ranking_identities
            ):
                raise ValueError(
                    "release ranking must contain every scoreable lookup medium exactly once"
                )

        if self.selected_release is not None:
            if not isinstance(self.selected_release, ReleaseSelectionState):
                raise TypeError("selected_release must be a ReleaseSelectionState or None")

            if self.release_ranking is None:
                raise ValueError("selected release requires a release ranking")

            if self.selected_release.identity not in self.release_ranking.identities:
                raise ValueError("selected release must belong to the release ranking")

            selected_identity = self.selected_release.identity[:3]
            lookup_result = self.lookup_result

            if lookup_result is None:
                raise ValueError("selected release requires a lookup result")

            lookup_candidate = next(
                (
                    item
                    for item in lookup_result.candidates
                    if (
                        item.candidate.engine_id,
                        item.candidate.source_id,
                        item.candidate.release_id,
                    )
                    == selected_identity
                ),
                None,
            )

            if (
                lookup_candidate is None
                or lookup_candidate.provenance != self.selected_release.candidate.provenance
            ):
                raise ValueError("selected release must retain its lookup provenance")

        if self.automatic_track_mapping is not None:
            if not isinstance(self.automatic_track_mapping, TrackMappingResult):
                raise TypeError("automatic_track_mapping must be a TrackMappingResult or None")

            if self.selected_release is None:
                raise ValueError("automatic track mapping requires a selected release")

            self._validate_mapping(self.automatic_track_mapping, self.selected_release)

        if self.manual_track_mapping is not None:
            if not isinstance(self.manual_track_mapping, TrackMappingResult):
                raise TypeError("manual_track_mapping must be a TrackMappingResult or None")

            if self.selected_release is None or self.automatic_track_mapping is None:
                raise ValueError("manual track mapping requires a selected release and original automatic mapping")

            if self.selected_metadata is not None:
                raise ValueError("manual mapping cannot retain the automatic metadata receipt")

            self._validate_mapping(self.manual_track_mapping, self.selected_release)

        if self.selected_metadata is not None:
            if not isinstance(self.selected_metadata, SelectedMetadataResult):
                raise TypeError("selected_metadata must be a SelectedMetadataResult or None")

            selected = self.selected_metadata

            if selected.selected_candidate is None or selected.track_mapping is None:
                raise ValueError("selected_metadata must contain a successful selection result")

            if self.candidate_lookup != selected.candidate_lookup:
                raise ValueError("selected_metadata must retain candidate_lookup exactly")

            expected_selection = ReleaseSelectionState(
                candidate=selected.selected_candidate,
                medium_index=selected.selected_identity[3],
            )
            expected_reviewed_files = tuple(
                ReviewedFileState(
                    file_id=reviewed.file_id,
                    proposals=reviewed.proposals,
                    reviews=reviewed.reviews,
                    track_mapping_resolved=reviewed.track_mapping_resolved,
                    change_set=reviewed.change_set,
                )
                for reviewed in selected.reviewed_files
            )

            if self.selected_release != expected_selection:
                raise ValueError("selected release must exactly project selected_metadata")

            if self.automatic_track_mapping != selected.track_mapping:
                raise ValueError("track mapping must exactly project selected_metadata")

            if reviewed_files != expected_reviewed_files:
                raise ValueError("reviewed files must exactly project selected_metadata")

        if self.selection_failure is not None:
            if not isinstance(self.selection_failure, SelectedMetadataResult):
                raise TypeError("selection_failure must be a SelectedMetadataResult or None")

            if self.selection_failure.selected_candidate is not None:
                raise ValueError("selection_failure must contain a failed selection result")

            if self.candidate_lookup != self.selection_failure.candidate_lookup:
                raise ValueError("selection_failure must retain candidate_lookup exactly")

        if self.search_failure is not None:
            if not isinstance(self.search_failure, CandidateLookupResult):
                raise TypeError("search_failure must be a CandidateLookupResult or None")

            failed_search = self.search_failure.lookup_result

            if failed_search.group_id != self.group.group_id:
                raise ValueError("search_failure must refer to this group")

            if (
                not failed_search.failures
                or failed_search.candidates
                or any(summary.successful_queries for summary in failed_search.summaries)
            ):
                raise ValueError("search_failure must contain an unsuccessful search")

        has_provider_proposals = any(reviewed.proposals for reviewed in reviewed_files)

        if has_provider_proposals and self.selected_release is None:
            raise ValueError("provider proposals require a selected release")

        _validate_optional_non_blank("language_override", self.language_override)

        if self.disc_number_override is not None:
            if type(self.disc_number_override) is not int:
                raise TypeError("disc_number_override must be an integer or None")

            if self.disc_number_override <= 0:
                raise ValueError("disc_number_override must be greater than zero")

        if self.search_query_override is not None and not isinstance(
            self.search_query_override,
            ReleaseSearchQuery,
        ):
            raise TypeError("search_query_override must be a ReleaseSearchQuery or None")

        if type(self.requires_rescan) is not bool:
            raise TypeError("requires_rescan must be a bool")

        _validate_revision("revision", self.revision)

        if self.requires_rescan and any(
            value
            for value in (
                self.lookup_result,
                self.release_ranking,
                self.candidate_lookup,
                self.selected_release,
                self.automatic_track_mapping,
                self.manual_track_mapping,
                reviewed_files,
                self.selected_metadata,
                self.selection_failure,
                self.search_failure,
            )
        ):
            raise ValueError("a group requiring rescan cannot retain derived state")

        object.__setattr__(self, "warnings", warnings)
        object.__setattr__(self, "reviewed_files", reviewed_files)

    @property
    def effective_track_mapping(self) -> TrackMappingResult | None:
        """Project human assignments while retaining the original automatic evidence."""
        return self.manual_track_mapping or self.automatic_track_mapping

    def _validate_mapping(
        self,
        mapping: TrackMappingResult,
        selection: ReleaseSelectionState,
    ) -> None:
        if mapping.selected_medium_index != selection.medium_index:
            raise ValueError("track mapping must refer to the selected medium index")

        if mapping.selected_medium_number != selection.medium.medium_number:
            raise ValueError("track mapping must refer to the selected medium number")

        local_ids = {
            *(item.local_file_id for item in mapping.mappings),
            *mapping.unmatched_local_file_ids,
        }

        if local_ids != {file.file_id for file in self.group.files}:
            raise ValueError("track mapping must partition every local group file")

        provider_indexes = {
            *(item.provider_track_index for item in mapping.mappings),
            *mapping.unmatched_provider_indexes,
        }

        if provider_indexes != set(range(len(selection.medium.tracks))):
            raise ValueError("track mapping must partition every selected provider track")


@dataclass(frozen=True)
class ReviewUndoFile:
    """The source and review values touched by one reversible review action."""

    source: LocalMediaFile
    before: ReviewedFileState
    after: ReviewedFileState

    def __post_init__(self) -> None:
        if not isinstance(self.source, LocalMediaFile):
            raise TypeError("undo source must be a LocalMediaFile")

        for review in (self.before, self.after):
            if not isinstance(review, ReviewedFileState) or review.file_id != self.source.file_id:
                raise ValueError("undo reviews must belong to the captured file")


@dataclass(frozen=True)
class ReviewUndoEntry:
    """One in-memory action; this is never a disk rollback or recovery record."""

    files: tuple[ReviewUndoFile, ...]

    def __post_init__(self) -> None:
        files = _copy_typed_sequence("undo files", self.files, ReviewUndoFile)

        if not files or len({item.source.file_id for item in files}) != len(files):
            raise ValueError("an undo action requires unique affected files")

        object.__setattr__(self, "files", files)


@dataclass(frozen=True)
class VerifiedWriteReceipt:
    """Ephemeral proof of the exact refreshed source and fields actually written."""

    source: LocalMediaFile
    changed_fields: frozenset[MetadataField]
    renamed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.source, LocalMediaFile):
            raise TypeError("a verified write receipt requires a LocalMediaFile")

        fields = frozenset(self.changed_fields)

        if any(not isinstance(field_name, MetadataField) for field_name in fields):
            raise TypeError("written fields must contain MetadataField values")

        if type(self.renamed) is not bool:
            raise TypeError("renamed must be a bool")

        if not fields and not self.renamed:
            raise ValueError("a verified write receipt requires a metadata change or rename")

        object.__setattr__(self, "changed_fields", fields)


@dataclass(frozen=True)
class SessionState:
    """Single memory-only source of truth owned and replaced by the UI thread."""

    root: Path | None
    groups: tuple[GroupState, ...] = ()
    unsupported_files: tuple[UnsupportedMediaFile, ...] = ()
    scan_issues: tuple[Issue, ...] = ()
    selection: SessionSelection = None
    active_operation: ActiveOperation | None = None
    revision: int = 0
    library_revision: int = 0
    review_undo: tuple[ReviewUndoEntry, ...] = ()
    provider_cache: MemoryCache[ProviderCacheKey, ProviderCacheValue] = field(
        default_factory=_default_provider_cache,
        compare=False,
        repr=False,
    )
    written_files: tuple[VerifiedWriteReceipt, ...] = ()

    def __post_init__(self) -> None:
        if self.root is not None and not isinstance(self.root, Path):
            raise TypeError("root must be a Path or None")

        groups = _copy_typed_sequence("groups", self.groups, GroupState)
        unsupported_files = _copy_typed_sequence(
            "unsupported_files",
            self.unsupported_files,
            UnsupportedMediaFile,
        )
        scan_issues = _copy_typed_sequence("scan_issues", self.scan_issues, Issue)
        review_undo = _copy_typed_sequence("review_undo", self.review_undo, ReviewUndoEntry)
        written_files = _copy_typed_sequence("written_files", self.written_files, VerifiedWriteReceipt)
        group_ids = tuple(group.group.group_id for group in groups)

        if len(group_ids) != len(set(group_ids)):
            raise ValueError("group IDs must be unique")

        all_supported = tuple(file for group in groups for file in group.group.files)
        file_ids = tuple(file.file_id for file in all_supported)

        if len(written_files) != len({receipt.source.file_id for receipt in written_files}):
            raise ValueError("verified write receipts must contain unique file IDs")

        current_sources = {
            source.file_id: source
            for group in groups if not group.requires_rescan
            for source in group.group.files
        }
        # Receipts are display evidence, not permanent flags attached to a path.
        # Replacing/removing a source or marking its group stale expires proof.
        written_files = tuple(
            receipt for receipt in written_files
            if current_sources.get(receipt.source.file_id) == receipt.source
        )

        if len(file_ids) != len(set(file_ids)):
            raise ValueError("each supported file ID must belong to exactly one group")

        supported_paths = tuple(file.path for file in all_supported)
        unsupported_paths = tuple(file.path for file in unsupported_files)

        if any(not isinstance(path, Path) for path in (*supported_paths, *unsupported_paths)):
            raise TypeError("media paths must be Path values")

        if len(supported_paths) != len(set(supported_paths)):
            raise ValueError("supported file paths must be unique")

        if len(unsupported_paths) != len(set(unsupported_paths)):
            raise ValueError("unsupported file paths must be unique")

        if set(supported_paths) & set(unsupported_paths):
            raise ValueError("supported and unsupported file paths must be disjoint")

        has_library_state = bool(groups or unsupported_files or scan_issues)

        if self.root is None and has_library_state:
            raise ValueError("a scan root is required when library state is present")

        if self.root is not None:
            for path in (*supported_paths, *unsupported_paths):
                if ".." in path.parts:
                    raise ValueError("every media path must be inside the scan root")

                try:
                    path.resolve(strict=False).relative_to(self.root.resolve(strict=False))
                except ValueError:
                    raise ValueError("every media path must be inside the scan root") from None

        if self.selection is not None and not isinstance(
            self.selection,
            (GroupSelection, UnsupportedSelection),
        ):
            raise TypeError("selection must be GroupSelection, UnsupportedSelection, or None")

        if isinstance(self.selection, GroupSelection) and self.selection.group_id not in set(group_ids):
            raise ValueError("selected group must exist")

        if isinstance(self.selection, UnsupportedSelection) and not unsupported_files:
            raise ValueError("unsupported selection requires unsupported files")

        if self.active_operation is not None:
            if not isinstance(self.active_operation, ActiveOperation):
                raise TypeError("active_operation must be an ActiveOperation or None")

            active = self.active_operation

            if active.base_session_revision > self.revision:
                raise ValueError("active operation session revision cannot be in the future")

            if active.base_library_revision > self.library_revision:
                raise ValueError("active operation library revision cannot be in the future")

            groups_by_id = {group.group.group_id: group for group in groups}

            for target in active.target_groups:
                current_group = groups_by_id.get(target.group_id)

                if current_group is None:
                    if active.base_library_revision == self.library_revision:
                        raise ValueError("operation target group must exist")

                    continue

                if (
                    active.base_library_revision == self.library_revision
                    and target.revision > current_group.revision
                ):
                    raise ValueError("operation target group revision cannot be in the future")

        _validate_revision("revision", self.revision)
        _validate_revision("library_revision", self.library_revision)

        if self.library_revision > self.revision:
            raise ValueError("library_revision cannot exceed revision")

        if any(group.revision > self.revision for group in groups):
            raise ValueError("group revision cannot exceed session revision")

        if not isinstance(self.provider_cache, MemoryCache):
            raise TypeError("provider_cache must be a MemoryCache")

        object.__setattr__(self, "groups", groups)
        object.__setattr__(self, "written_files", written_files)
        object.__setattr__(self, "unsupported_files", unsupported_files)
        object.__setattr__(self, "scan_issues", scan_issues)
        object.__setattr__(self, "review_undo", review_undo)


def set_selection(state: SessionState, selection: SessionSelection) -> SessionState:
    """Change visible navigation without invalidating semantic worker snapshots."""
    if not isinstance(state, SessionState):
        raise TypeError("state must be a SessionState")

    if state.selection == selection:
        return state

    # Navigation changes what is visible, not the evidence a worker consumed;
    # leave semantic revisions alone so browsing does not make results stale.
    return replace(state, selection=selection)


def mark_groups_requires_rescan(
    state: SessionState,
    group_ids: Sequence[str],
) -> SessionState:
    """Invalidate derived data only for groups whose files changed on disk."""
    if not isinstance(state, SessionState):
        raise TypeError("state must be a SessionState")

    requested = _copy_typed_sequence("group_ids", group_ids, str)

    if not requested:
        raise ValueError("at least one group must be marked for rescan")

    if len(requested) != len(set(requested)):
        raise ValueError("group IDs to mark for rescan must be unique")

    known = {group.group.group_id for group in state.groups}
    unknown = set(requested) - known

    if unknown:
        raise ValueError("cannot mark an unknown group for rescan")

    requested_set = set(requested)
    changed = False
    updated_groups: list[GroupState] = []

    for group in state.groups:
        if group.group.group_id not in requested_set or group.requires_rescan:
            updated_groups.append(group)
            continue

        changed = True
        updated_groups.append(
            replace(
                group,
                lookup_result=None,
                release_ranking=None,
                candidate_lookup=None,
                selected_release=None,
                automatic_track_mapping=None,
                manual_track_mapping=None,
                reviewed_files=(),
                selected_metadata=None,
                selection_failure=None,
                search_failure=None,
                requires_rescan=True,
                revision=group.revision + 1,
            )
        )

    if not changed:
        return state

    return replace(
        state,
        groups=tuple(updated_groups),
        revision=state.revision + 1,
    )


def begin_operation(
    state: SessionState,
    operation_id: str,
    kind: OperationKind,
    target_group_ids: Sequence[str],
) -> SessionState:
    """Capture exact semantic versions before dispatching background work."""
    if not isinstance(state, SessionState):
        raise TypeError("state must be a SessionState")

    if state.active_operation is not None:
        raise ValueError("another major operation is already active")

    _validate_non_blank("operation_id", operation_id)

    if not isinstance(kind, OperationKind):
        raise TypeError("kind must be an OperationKind")

    target_ids = _copy_typed_sequence(
        "target_group_ids",
        target_group_ids,
        str,
    )

    if len(target_ids) != len(set(target_ids)):
        raise ValueError("operation target group IDs must be unique")

    groups_by_id = {group.group.group_id: group for group in state.groups}
    unknown = tuple(group_id for group_id in target_ids if group_id not in groups_by_id)

    if unknown:
        raise ValueError("operation cannot target an unknown operation target group")

    if kind in {OperationKind.LOOKUP, OperationKind.APPLY} and any(
        groups_by_id[group_id].requires_rescan for group_id in target_ids
    ):
        raise ValueError("a group requiring rescan cannot start lookup or apply")

    # Capture three scopes: whole-session decisions, library membership and each
    # target group's evidence. Reducers use the scope relevant to their result.
    active = ActiveOperation(
        operation_id=operation_id,
        kind=kind,
        base_session_revision=state.revision,
        base_library_revision=state.library_revision,
        target_groups=tuple(
            GroupVersion(group_id, groups_by_id[group_id].revision)
            for group_id in target_ids
        ),
    )

    return replace(state, active_operation=active)


def finish_operation(state: SessionState, operation_id: str) -> SessionState:
    """Clear only the operation whose terminal event is being handled."""
    if not isinstance(state, SessionState):
        raise TypeError("state must be a SessionState")

    _validate_non_blank("operation_id", operation_id)
    active = state.active_operation

    # A queued terminal callback can arrive after another operation has started.
    # Only its own operation ID is allowed to clear the active-operation slot.
    if active is None or active.operation_id != operation_id:
        return state

    return replace(state, active_operation=None)


@dataclass(frozen=True)
class ScanResultEnvelope:
    """A scanned library labelled with the versions captured before dispatch."""

    operation_id: str
    base_session_revision: int
    base_library_revision: int
    root: Path
    groups: tuple[GroupState, ...]
    unsupported_files: tuple[UnsupportedMediaFile, ...] = ()
    scan_issues: tuple[Issue, ...] = ()

    @classmethod
    def from_scan_library_result(cls, result: ScanLibraryResult) -> ScanResultEnvelope:
        """Convert state-independent scan output into fresh reducer-owned state."""
        if not isinstance(result, ScanLibraryResult):
            raise TypeError("result must be a ScanLibraryResult")

        warnings_by_group: dict[str, list[GroupingWarning]] = {
            group.group_id: [] for group in result.grouping_result.groups
        }

        for warning in result.grouping_result.warnings:
            warnings_by_group[warning.group_id].append(warning)

        return cls(
            operation_id=result.operation_id,
            base_session_revision=result.base_session_revision,
            base_library_revision=result.base_library_revision,
            root=result.root,
            groups=tuple(
                GroupState(
                    group=group,
                    warnings=tuple(warnings_by_group[group.group_id]),
                )
                for group in result.grouping_result.groups
            ),
            unsupported_files=result.scan_result.unsupported_files,
            scan_issues=result.scan_result.issues,
        )

    def __post_init__(self) -> None:
        _validate_non_blank("operation_id", self.operation_id)
        _validate_revision("base_session_revision", self.base_session_revision)
        _validate_revision("base_library_revision", self.base_library_revision)

        if self.base_library_revision > self.base_session_revision:
            raise ValueError("base_library_revision cannot exceed base_session_revision")

        if not isinstance(self.root, Path):
            raise TypeError("root must be a Path")

        groups = _copy_typed_sequence("groups", self.groups, GroupState)
        unsupported_files = _copy_typed_sequence(
            "unsupported_files",
            self.unsupported_files,
            UnsupportedMediaFile,
        )
        scan_issues = _copy_typed_sequence("scan_issues", self.scan_issues, Issue)

        if any(
            group.revision != 0
            or group.lookup_result is not None
            or group.release_ranking is not None
            or group.candidate_lookup is not None
            or group.selected_release is not None
            or group.automatic_track_mapping is not None
            or group.reviewed_files
            or group.selected_metadata is not None
            or group.selection_failure is not None
            or group.search_failure is not None
            or group.requires_rescan
            or group.language_override is not None
            or group.disc_number_override is not None
            or group.search_query_override is not None
            for group in groups
        ):
            raise ValueError(
                "a scan result must contain fresh state without session-only overrides"
            )

        # Reuse the same root, ownership, warning, and path validation as a live
        # session. The temporary cache is discarded and never crosses this seam.
        SessionState(
            root=self.root,
            groups=groups,
            unsupported_files=unsupported_files,
            scan_issues=scan_issues,
        )
        object.__setattr__(self, "groups", groups)
        object.__setattr__(self, "unsupported_files", unsupported_files)
        object.__setattr__(self, "scan_issues", scan_issues)


@dataclass(frozen=True)
class GroupResultEnvelope:
    """One complete group result labelled with its captured version lineage."""

    operation_id: str
    base_session_revision: int
    base_library_revision: int
    target: GroupVersion
    group_state: GroupState

    def __post_init__(self) -> None:
        _validate_non_blank("operation_id", self.operation_id)
        _validate_revision("base_session_revision", self.base_session_revision)
        _validate_revision("base_library_revision", self.base_library_revision)

        if self.base_library_revision > self.base_session_revision:
            raise ValueError("base_library_revision cannot exceed base_session_revision")

        if not isinstance(self.target, GroupVersion):
            raise TypeError("target must be a GroupVersion")

        if not isinstance(self.group_state, GroupState):
            raise TypeError("group_state must be a GroupState")

        if self.group_state.group.group_id != self.target.group_id:
            raise ValueError("group result target must match its group state")

        if self.group_state.revision != self.target.revision:
            raise ValueError("group result must retain its captured base revision")


class ResultApplicationStatus(StrEnum):
    """Whether a worker result was current enough to enter session state."""

    APPLIED = "applied"
    STALE = "stale"


class StaleResultReason(StrEnum):
    """Stable reason that a worker result can no longer be applied safely."""

    OPERATION_MISMATCH = "OPERATION_MISMATCH"
    SESSION_REVISION_CHANGED = "SESSION_REVISION_CHANGED"
    LIBRARY_REVISION_CHANGED = "LIBRARY_REVISION_CHANGED"
    GROUP_REVISION_CHANGED = "GROUP_REVISION_CHANGED"
    GROUP_MISSING = "GROUP_MISSING"


@dataclass(frozen=True)
class StateApplicationResult:
    """The resulting state and explicit disposition of one worker result."""

    state: SessionState
    status: ResultApplicationStatus
    reason: StaleResultReason | None

    def __post_init__(self) -> None:
        if not isinstance(self.state, SessionState):
            raise TypeError("state must be a SessionState")

        if not isinstance(self.status, ResultApplicationStatus):
            raise TypeError("status must be a ResultApplicationStatus")

        if self.status is ResultApplicationStatus.APPLIED and self.reason is not None:
            raise ValueError("an applied result cannot have a stale reason")

        if self.status is ResultApplicationStatus.STALE and not isinstance(
            self.reason,
            StaleResultReason,
        ):
            raise ValueError("a stale result requires a StaleResultReason")


def _stale(
    state: SessionState,
    reason: StaleResultReason,
) -> StateApplicationResult:
    return StateApplicationResult(
        state=state,
        status=ResultApplicationStatus.STALE,
        reason=reason,
    )


def _operation_matches(
    state: SessionState,
    *,
    operation_id: str,
    kind: OperationKind,
    base_session_revision: int,
    base_library_revision: int,
) -> bool:
    active = state.active_operation

    return (
        active is not None
        and active.operation_id == operation_id
        and active.kind is kind
        and active.base_session_revision == base_session_revision
        and active.base_library_revision == base_library_revision
    )


def apply_scan_result(
    state: SessionState,
    envelope: ScanResultEnvelope,
) -> StateApplicationResult:
    """Atomically replace a library only when the scan snapshot remains current."""
    if not isinstance(state, SessionState):
        raise TypeError("state must be a SessionState")

    if not isinstance(envelope, ScanResultEnvelope):
        raise TypeError("envelope must be a ScanResultEnvelope")

    if not _operation_matches(
        state,
        operation_id=envelope.operation_id,
        kind=OperationKind.SCAN,
        base_session_revision=envelope.base_session_revision,
        base_library_revision=envelope.base_library_revision,
    ):
        return _stale(state, StaleResultReason.OPERATION_MISMATCH)

    if state.library_revision != envelope.base_library_revision:
        return _stale(state, StaleResultReason.LIBRARY_REVISION_CHANGED)

    if state.revision != envelope.base_session_revision:
        return _stale(state, StaleResultReason.SESSION_REVISION_CHANGED)

    # A scan replaces the entire library, so unlike a single-group lookup it
    # requires the complete session snapshot to remain current.
    updated = SessionState(
        root=envelope.root,
        groups=envelope.groups,
        unsupported_files=envelope.unsupported_files,
        scan_issues=envelope.scan_issues,
        selection=None,
        active_operation=state.active_operation,
        revision=state.revision + 1,
        library_revision=state.library_revision + 1,
        provider_cache=state.provider_cache,
    )

    return StateApplicationResult(
        state=updated,
        status=ResultApplicationStatus.APPLIED,
        reason=None,
    )


def apply_scan_library_result(
    state: SessionState,
    result: ScanLibraryResult,
) -> StateApplicationResult:
    """Convert and apply one scan service result through the canonical reducer."""
    return apply_scan_result(
        state,
        ScanResultEnvelope.from_scan_library_result(result),
    )


def _apply_batch_stale_reason(
    state: SessionState,
    result: ApplyBatchResult,
) -> StaleResultReason | None:
    """Classify lineage before rescan invalidation changes semantic revisions."""
    active = state.active_operation
    result_targets = tuple(
        GroupVersion(group.group_id, group.base_group_revision)
        for group in result.groups
    )

    if (
        not _operation_matches(
            state,
            operation_id=result.operation_id,
            kind=OperationKind.APPLY,
            base_session_revision=result.base_session_revision,
            base_library_revision=result.base_library_revision,
        )
        or active is None
        or result_targets != active.target_groups
    ):
        return StaleResultReason.OPERATION_MISMATCH

    groups_by_id = {group.group.group_id: group for group in state.groups}

    if any(target.group_id not in groups_by_id for target in result_targets):
        return StaleResultReason.GROUP_MISSING

    if state.library_revision != result.base_library_revision:
        return StaleResultReason.LIBRARY_REVISION_CHANGED

    if any(
        groups_by_id[target.group_id].revision != target.revision
        for target in result_targets
    ):
        return StaleResultReason.GROUP_REVISION_CHANGED

    return None


def _current_apply_membership_matches(
    state: SessionState,
    result: ApplyBatchResult,
) -> bool:
    groups_by_id = {group.group.group_id: group for group in state.groups}

    for outcome_group in result.groups:
        current = groups_by_id[outcome_group.group_id]
        current_pairs = {
            (source.file_id, source.path)
            for source in current.group.files
        }

        if any(
            (outcome.file_id, outcome.source_path) not in current_pairs
            for outcome in outcome_group.files
        ):
            return False

    return True


def _reconcile_apply_filesystem_effects(
    state: SessionState,
    result: ApplyBatchResult,
    *,
    accept_refresh: bool,
) -> SessionState:
    # Match both stable file ID and original path: an old result must not be
    # attached to a different source merely because an identifier was reused.
    changed = {
        (outcome.file_id, outcome.source_path): outcome
        for group in result.groups
        for outcome in group.files
        if outcome.filesystem_changed
    }

    if not changed:
        return state

    updated_groups: list[GroupState] = []
    uncertain_groups: list[str] = []
    touched_ids: set[str] = set()
    refreshed_any = False
    new_receipts: list[VerifiedWriteReceipt] = []

    for group in state.groups:
        effects = {
            source.file_id: changed[(source.file_id, source.path)]
            for source in group.group.files
            if (source.file_id, source.path) in changed
        }

        if not effects:
            updated_groups.append(group)
            continue

        touched_ids.update(effects)

        if not accept_refresh or any(outcome.refreshed_source is None for outcome in effects.values()):
            # A stale worker or unresolved post-commit/cleanup result must never
            # claim a fresh local snapshot merely because it contains new values.
            uncertain_groups.append(group.group.group_id)
            updated_groups.append(group)
            continue

        refreshed_any = True
        new_receipts.extend(
            VerifiedWriteReceipt(
                cast(LocalMediaFile, outcome.refreshed_source),
                frozenset(change.field for change in outcome.change_set.metadata_changes),
                outcome.change_set.rename_change is not None,
            )
            for outcome in effects.values()
        )
        files = tuple(
            cast(LocalMediaFile, effects[source.file_id].refreshed_source)
            if source.file_id in effects else source
            for source in group.group.files
        )
        updated_groups.append(replace(
            group,
            # Refresh the cached album label from the verified snapshots and
            # untouched siblings. Deriving only the title preserves stable group
            # membership, including a user's manual split or merge.
            group=replace(group.group, files=files, album_title=derive_album_title(files)),
            # The selected receipt projects the old full review exactly. Keep
            # its candidate/mapping evidence and unaffected reviews separately.
            selected_metadata=None,
            selection_failure=None,
            search_failure=None,
            reviewed_files=tuple(review for review in group.reviewed_files if review.file_id not in effects),
            revision=group.revision + 1,
        ))

    if not touched_ids:
        return state

    # Review Undo is an in-memory convenience, not a disk rollback. Remove only
    # files touched on disk, retaining untouched members of earlier batch actions.
    undo = tuple(
        ReviewUndoEntry(remaining)
        for entry in state.review_undo
        if (remaining := tuple(item for item in entry.files if item.source.file_id not in touched_ids))
    )
    updated = state
    receipts = (
        *(receipt for receipt in state.written_files if receipt.source.file_id not in touched_ids),
        *new_receipts,
    )

    if refreshed_any or undo != state.review_undo or receipts != state.written_files:
        updated = replace(
            state,
            groups=tuple(updated_groups),
            review_undo=undo,
            written_files=receipts,
            revision=state.revision + int(refreshed_any),
        )

    return mark_groups_requires_rescan(updated, uncertain_groups) if uncertain_groups else updated


def apply_batch_result(
    state: SessionState,
    result: ApplyBatchResult,
) -> StateApplicationResult:
    """Reconcile irreversible Apply effects while preserving stale diagnostics."""
    if not isinstance(state, SessionState):
        raise TypeError("state must be a SessionState")

    if not isinstance(result, ApplyBatchResult):
        raise TypeError("result must be an ApplyBatchResult")

    # Judge staleness before reconciliation increments any revision. Even stale
    # Apply results must invalidate affected snapshots because disk writes cannot
    # be discarded in the same way as an outdated provider search result.
    stale_reason = _apply_batch_stale_reason(state, result)

    if stale_reason is None and not _current_apply_membership_matches(state, result):
        stale_reason = StaleResultReason.OPERATION_MISMATCH

    updated = _reconcile_apply_filesystem_effects(state, result, accept_refresh=stale_reason is None)

    if stale_reason is not None:
        return _stale(updated, stale_reason)

    return StateApplicationResult(
        state=updated,
        status=ResultApplicationStatus.APPLIED,
        reason=None,
    )


def _validate_group_worker_result(
    current: GroupState,
    replacement: GroupState,
) -> None:
    if replacement.group != current.group:
        raise ValueError("a group worker result cannot change file membership")

    if replacement.warnings != current.warnings:
        raise ValueError("a group worker result cannot change grouping warnings")

    if replacement.language_override != current.language_override:
        raise ValueError("a group worker result cannot change language_override")

    if replacement.disc_number_override != current.disc_number_override:
        raise ValueError("a group worker result cannot change disc_number_override")

    if replacement.search_query_override != current.search_query_override:
        raise ValueError("a group worker result cannot change search_query_override")

    if replacement.requires_rescan != current.requires_rescan:
        raise ValueError("a group worker result cannot change requires_rescan")


def apply_group_result(
    state: SessionState,
    envelope: GroupResultEnvelope,
) -> StateApplicationResult:
    """Replace one whole group while tolerating changes to unrelated groups."""
    if not isinstance(state, SessionState):
        raise TypeError("state must be a SessionState")

    if not isinstance(envelope, GroupResultEnvelope):
        raise TypeError("envelope must be a GroupResultEnvelope")

    active = state.active_operation

    if not _operation_matches(
        state,
        operation_id=envelope.operation_id,
        kind=OperationKind.LOOKUP,
        base_session_revision=envelope.base_session_revision,
        base_library_revision=envelope.base_library_revision,
    ) or active is None or envelope.target not in active.target_groups:
        return _stale(state, StaleResultReason.OPERATION_MISMATCH)

    group_index = next(
        (
            index
            for index, group in enumerate(state.groups)
            if group.group.group_id == envelope.target.group_id
        ),
        None,
    )

    if group_index is None:
        return _stale(state, StaleResultReason.GROUP_MISSING)

    if state.library_revision != envelope.base_library_revision:
        return _stale(state, StaleResultReason.LIBRARY_REVISION_CHANGED)

    current = state.groups[group_index]

    # Unrelated groups may have changed since dispatch. This result is usable
    # while its own group revision and library membership still match.
    if current.revision != envelope.target.revision:
        return _stale(state, StaleResultReason.GROUP_REVISION_CHANGED)

    _validate_group_worker_result(current, envelope.group_state)
    replacement = replace(envelope.group_state, revision=current.revision + 1)
    groups = state.groups[:group_index] + (replacement,) + state.groups[group_index + 1 :]
    updated = replace(
        state,
        groups=groups,
        revision=state.revision + 1,
    )

    return StateApplicationResult(
        state=updated,
        status=ResultApplicationStatus.APPLIED,
        reason=None,
    )


def _group_state_from_lookup_result(
    state: SessionState,
    current: GroupState,
    result: GroupLookupResult,
    rename_settings: RenameSettings,
) -> GroupState:
    # Session transforms already consume the state dataclasses. Importing this
    # narrow reconciliation seam when reducing avoids a module initialisation cycle.
    from metadata_polisher.session.review_editing import local_reviews_after_lookup_reset, reconcile_lookup_reviews

    selected = result.selected_metadata

    if selected is None:
        search = result.candidate_lookup.lookup_result

        if (
            search.failures
            and not search.candidates
            and not any(summary.successful_queries for summary in search.summaries)
        ):
            # No completed search supplied replacement evidence. Keep the whole
            # usable review and its provenance, recording this attempt separately.
            # A first failed search still remains reachable from the chooser.
            retained = current.candidate_lookup or result.candidate_lookup

            return replace(
                current,
                lookup_result=retained.lookup_result,
                release_ranking=retained.release_ranking,
                candidate_lookup=retained,
                search_failure=result.candidate_lookup,
                revision=result.base_group_revision,
            )

        # A completed empty search is a valid replacement outcome, just like a
        # new candidate list. In both cases the next release is chosen explicitly;
        # independent local decisions survive the evidence reset below.
        return replace(
            current,
            lookup_result=result.candidate_lookup.lookup_result,
            release_ranking=result.candidate_lookup.release_ranking,
            candidate_lookup=result.candidate_lookup,
            selected_release=None,
            automatic_track_mapping=None,
            manual_track_mapping=None,
            reviewed_files=local_reviews_after_lookup_reset(state, current, rename_settings),
            selected_metadata=None,
            selection_failure=None,
            search_failure=None,
            revision=result.base_group_revision,
        )

    if selected.selected_candidate is None:
        # A failed replacement attempt must not destroy a prior valid review.
        return replace(
            current,
            lookup_result=result.candidate_lookup.lookup_result,
            release_ranking=result.candidate_lookup.release_ranking,
            candidate_lookup=result.candidate_lookup,
            selection_failure=selected,
            search_failure=None,
            revision=result.base_group_revision,
        )

    if selected.track_mapping is None:
        raise ValueError("a successful selected result must retain its track mapping")

    reviewed_files = tuple(
        ReviewedFileState(
            file_id=reviewed.file_id,
            proposals=reviewed.proposals,
            reviews=reviewed.reviews,
            track_mapping_resolved=reviewed.track_mapping_resolved,
            change_set=reviewed.change_set,
        )
        for reviewed in selected.reviewed_files
    )

    fresh = replace(
        current,
        lookup_result=result.candidate_lookup.lookup_result,
        release_ranking=result.candidate_lookup.release_ranking,
        candidate_lookup=result.candidate_lookup,
        selected_release=ReleaseSelectionState(
            candidate=selected.selected_candidate,
            medium_index=selected.selected_identity[3],
        ),
        automatic_track_mapping=selected.track_mapping,
        manual_track_mapping=None,
        reviewed_files=reviewed_files,
        selected_metadata=selected,
        selection_failure=None,
        search_failure=None,
        revision=result.base_group_revision,
    )

    return reconcile_lookup_reviews(state, current, fresh, rename_settings)


def apply_group_lookup_result(
    state: SessionState,
    result: GroupLookupResult,
    rename_settings: RenameSettings = _DEFAULT_RENAME_SETTINGS,
) -> StateApplicationResult:
    """Convert and atomically reduce one state-free lookup worker result."""
    if not isinstance(state, SessionState):
        raise TypeError("state must be a SessionState")

    if not isinstance(result, GroupLookupResult):
        raise TypeError("result must be a GroupLookupResult")

    active = state.active_operation
    target = GroupVersion(result.group_id, result.base_group_revision)

    if not _operation_matches(
        state,
        operation_id=result.operation_id,
        kind=OperationKind.LOOKUP,
        base_session_revision=result.base_session_revision,
        base_library_revision=result.base_library_revision,
    ) or active is None or target not in active.target_groups:
        return _stale(state, StaleResultReason.OPERATION_MISMATCH)

    group_index = next(
        (
            index
            for index, group in enumerate(state.groups)
            if group.group.group_id == result.group_id
        ),
        None,
    )

    if group_index is None:
        return _stale(state, StaleResultReason.GROUP_MISSING)

    if state.library_revision != result.base_library_revision:
        return _stale(state, StaleResultReason.LIBRARY_REVISION_CHANGED)

    current = state.groups[group_index]

    if current.revision != result.base_group_revision:
        return _stale(state, StaleResultReason.GROUP_REVISION_CHANGED)

    replacement = _group_state_from_lookup_result(state, current, result, rename_settings)

    return apply_group_result(
        state,
        GroupResultEnvelope(
            operation_id=result.operation_id,
            base_session_revision=result.base_session_revision,
            base_library_revision=result.base_library_revision,
            target=target,
            group_state=replacement,
        ),
    )
