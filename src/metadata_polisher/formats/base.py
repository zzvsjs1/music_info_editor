"""Format adapter contract separating semantic metadata from physical tags."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from metadata_polisher.domain.errors import Issue, MediaErrorCode
from metadata_polisher.domain.media import MediaReadResult, StreamInfo
from metadata_polisher.domain.metadata import FieldReadState, MetadataChange, MetadataField, MetadataSnapshot


class MediaFormatError(RuntimeError):
    """Expected media-boundary failure with stable UI and diagnostic context."""

    def __init__(self, *, path: Path, issue: Issue) -> None:
        self.path = path
        self.issue = issue
        super().__init__(f"{issue.code}: {issue.message} [{path}]")

    @classmethod
    def from_cause(
        cls,
        *,
        path: Path,
        code: MediaErrorCode,
        message: str,
        cause: Exception,
    ) -> MediaFormatError:
        """Retain third-party details while callers preserve the exception chain."""
        return cls(
            path=path,
            issue=Issue(
                code=code,
                message=message,
                technical_detail=f"{type(cause).__name__}: {cause}",
            ),
        )


@dataclass(frozen=True)
class TagReadResult[T]:
    """A decoded field value with its physical read state and diagnostic."""

    value: T
    # Empty values arise from both absent tags and unreadable content. Keep the
    # state beside the value so matching and verification retain that distinction.
    read_state: FieldReadState
    detail: str | None = None


@dataclass(frozen=True)
class VerificationResult:
    """Outcome of reopening and checking a written temporary media file."""

    ok: bool
    issues: tuple[Issue, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "issues", tuple(self.issues))


# Adapters edit the path supplied by their caller. The transaction layer owns
# the temporary copy and is responsible for publishing a verified result.
class MediaFormatAdapter(Protocol):
    """Boundary implemented by each supported physical media/tag format."""

    format_id: str
    extensions: frozenset[str]

    def can_handle(self, path: Path) -> bool: ...

    def read(self, path: Path) -> MediaReadResult: ...

    def write_changes(self, path: Path, changes: tuple[MetadataChange, ...]) -> None: ...

    # Check only the reviewed fields plus stable stream properties. This contract
    # does not claim that the encoded audio payload has been checksummed.
    def verify(
        self,
        path: Path,
        expected: MetadataSnapshot,
        changed_fields: frozenset[MetadataField],
        baseline_stream: StreamInfo,
    ) -> VerificationResult: ...
