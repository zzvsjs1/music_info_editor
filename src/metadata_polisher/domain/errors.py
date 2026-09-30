"""Stable typed issue codes shared across external boundaries."""

from dataclasses import dataclass
from enum import StrEnum


# Stable codes drive presentation and recovery without parsing human messages.
# In particular, access denial is distinct from a valid search returning no result.
class ProviderErrorCode(StrEnum):
    """Expected provider and network failure categories."""

    NETWORK_TIMEOUT = "NETWORK_TIMEOUT"
    NETWORK_ERROR = "NETWORK_ERROR"
    PROXY_CONNECTION_FAILED = "PROXY_CONNECTION_FAILED"
    PROXY_AUTHENTICATION_REQUIRED = "PROXY_AUTHENTICATION_REQUIRED"
    TLS_VERIFICATION_FAILED = "TLS_VERIFICATION_FAILED"
    RATE_LIMITED = "RATE_LIMITED"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    ACCESS_DENIED = "ACCESS_DENIED"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    NOT_FOUND = "NOT_FOUND"


class MediaErrorCode(StrEnum):
    """Expected media, metadata, and filesystem failure categories."""

    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    CORRUPT_FILE = "CORRUPT_FILE"
    TAG_READ_FAILED = "TAG_READ_FAILED"
    ADDITIONAL_METADATA = "ADDITIONAL_METADATA"
    TAG_WRITE_FAILED = "TAG_WRITE_FAILED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    SOURCE_CHANGED = "SOURCE_CHANGED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    DESTINATION_EXISTS = "DESTINATION_EXISTS"
    INSUFFICIENT_SPACE = "INSUFFICIENT_SPACE"
    FILE_COPY_FAILED = "FILE_COPY_FAILED"
    BACKUP_FAILED = "BACKUP_FAILED"
    COMMIT_FAILED = "COMMIT_FAILED"
    RENAME_FAILED = "RENAME_FAILED"
    CLEANUP_FAILED = "CLEANUP_FAILED"


class MatchingErrorCode(StrEnum):
    """Expected deterministic release/track matching outcomes."""

    NO_CANDIDATE = "NO_CANDIDATE"
    AMBIGUOUS_CANDIDATE = "AMBIGUOUS_CANDIDATE"
    PARTIAL_TRACK_MAPPING = "PARTIAL_TRACK_MAPPING"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


type IssueCode = ProviderErrorCode | MediaErrorCode | MatchingErrorCode


@dataclass(frozen=True)
class Issue:
    """Compact user-facing issue with optional developer diagnostic detail."""

    code: IssueCode
    message: str
    # Keep concise user-facing wording separate from diagnostics so boundaries
    # can add useful failure context without changing the stable issue code.
    technical_detail: str | None = None
