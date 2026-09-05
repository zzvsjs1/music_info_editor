"""Immutable local-media scan and read results."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType

from metadata_polisher.domain.errors import Issue
from metadata_polisher.domain.metadata import FieldReadState, MetadataField, MetadataSnapshot


class FilenameHintConfidence(Enum):
    """Coarse evidence strength assigned by deterministic filename parser rules."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class FilenameHintReason(Enum):
    """Stable parser rule identifying why filename evidence was produced."""

    LABELLED_DISC_TRACK_PREFIX = "LABELLED_DISC_TRACK_PREFIX"
    CD_TRACK_PREFIX = "CD_TRACK_PREFIX"
    DISC_TRACK_DOTTED_PREFIX = "DISC_TRACK_DOTTED_PREFIX"
    DISC_TRACK_HYPHEN_PREFIX = "DISC_TRACK_HYPHEN_PREFIX"
    TRACK_DASH_PREFIX = "TRACK_DASH_PREFIX"
    TRACK_DOTTED_PREFIX = "TRACK_DOTTED_PREFIX"
    TRACK_UNDERSCORE_PREFIX = "TRACK_UNDERSCORE_PREFIX"
    STEM_ONLY = "STEM_ONLY"


@dataclass(frozen=True)
class FilenameHints:
    """Non-authoritative disc, track, and title evidence parsed from a filename."""

    disc_number: int | None = None
    track_number: int | None = None
    probable_title: str | None = None
    reason: FilenameHintReason | None = None
    confidence: FilenameHintConfidence | None = None


@dataclass(frozen=True)
class StreamInfo:
    """Stable audio properties used for diagnostics and write verification."""

    duration_seconds: float | None
    sample_rate: int | None
    channels: int | None
    bit_depth: int | None
    codec: str | None


@dataclass(frozen=True)
class MediaReadResult:
    """Format-neutral metadata, per-field read states, stream data, and issues."""

    metadata: MetadataSnapshot
    # A value alone cannot distinguish absent, unreadable and unsupported tags.
    # Carry a state for every managed field alongside the semantic snapshot.
    field_states: Mapping[MetadataField, FieldReadState]
    stream_info: StreamInfo
    issues: tuple[Issue, ...] = ()

    def __post_init__(self) -> None:
        copied_states = dict(self.field_states)
        required_fields = frozenset(MetadataField)
        supplied_fields = frozenset(copied_states)

        # Incomplete maps would make downstream matching guess what omitted
        # fields mean. Reject them at the adapter boundary instead.
        if supplied_fields != required_fields:
            missing = ", ".join(sorted(field.value for field in required_fields - supplied_fields))
            unexpected = ", ".join(sorted(str(field) for field in supplied_fields - required_fields))
            raise ValueError(
                "field_states must contain exactly one state for every MetadataField; "
                f"missing=[{missing}], unexpected=[{unexpected}]"
            )

        # A read result crosses an adapter boundary. Defensive copies ensure an
        # adapter cannot later change the domain result through retained containers.
        object.__setattr__(self, "field_states", MappingProxyType(copied_states))
        object.__setattr__(self, "issues", tuple(self.issues))


@dataclass(frozen=True)
class LocalMediaFile:
    """One supported local file and all evidence gathered without provider lookup."""

    path: Path
    format_id: str
    read_result: MediaReadResult
    filename_hints: FilenameHints = field(default_factory=FilenameHints)
    file_id: str = ""

    def __post_init__(self) -> None:
        if not self.file_id:
            # The full scanned path is deterministic within a session and remains
            # stable if a later reviewed ChangeSet proposes a destination rename.
            object.__setattr__(self, "file_id", str(self.path))


class UnsupportedMediaStatus(Enum):
    """Status displayed for recognised audio formats outside V1 support."""

    NOT_SUPPORTED_YET = "not_supported_yet"


# Recognised audio outside current support remains visible in scan results
# so the user can distinguish unsupported files from files never discovered.
@dataclass(frozen=True)
class UnsupportedMediaFile:
    """Known audio file retained in scan results despite unsupported metadata."""

    path: Path
    status: UnsupportedMediaStatus = UnsupportedMediaStatus.NOT_SUPPORTED_YET
