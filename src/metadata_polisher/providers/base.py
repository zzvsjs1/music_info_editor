"""Metadata provider contract shared by concrete boundary adapters."""

from dataclasses import dataclass, field
from typing import Protocol

from metadata_polisher.domain.matching import ReleaseCandidate, ReleaseSearchQuery
from metadata_polisher.execution.cancellation import CancellationToken
from metadata_polisher.execution.events import OperationEventSink


def _validate_string(name: str, value: object) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")


@dataclass(frozen=True)
class RequestContext:
    """Stable per-lookup values passed to every participating provider."""

    operation_id: str
    preferred_language: str
    cancellation: CancellationToken | None = field(default=None, repr=False, compare=False)
    events: OperationEventSink | None = field(default=None, repr=False, compare=False)
    credential_generation: int = 0

    def __post_init__(self) -> None:
        _validate_string("operation_id", self.operation_id)
        _validate_string("preferred_language", self.preferred_language)


@dataclass(frozen=True)
class ProviderCapabilities:
    """Features honestly supplied by a concrete metadata provider."""

    release_search: bool
    track_listing: bool
    composer_credits: bool
    multilingual_titles: bool

    def __post_init__(self) -> None:
        for name in (
            "release_search",
            "track_listing",
            "composer_credits",
            "multilingual_titles",
        ):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be a bool")


# Search, basic track loading and selected-release enrichment are separate
# calls so expensive credit queries are deferred until a candidate is chosen.
class MetadataProvider(Protocol):
    """Synchronous provider boundary used by background lookup operations."""

    @property
    def engine_id(self) -> str: ...

    def capabilities(self) -> ProviderCapabilities: ...

    def search_releases(
        self,
        query: ReleaseSearchQuery,
        context: RequestContext,
    ) -> tuple[ReleaseCandidate, ...]: ...

    def load_release_media(
        self,
        candidate: ReleaseCandidate,
        context: RequestContext,
    ) -> ReleaseCandidate:
        """Load real track listings without expensive composer relationships."""
        ...

    def enrich_release(
        self,
        candidate: ReleaseCandidate,
        context: RequestContext,
    ) -> ReleaseCandidate: ...
