"""Ordered, failure-isolating orchestration for enabled metadata providers."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from metadata_polisher.domain.errors import Issue, ProviderErrorCode
from metadata_polisher.domain.matching import MetadataProvenance, ReleaseCandidate, ReleaseSearchQuery
from metadata_polisher.execution.cancellation import OperationCancelledError
from metadata_polisher.providers.base import MetadataProvider, RequestContext
from metadata_polisher.providers.transport import ProviderTransportError


def _typed_tuple[T](name: str, values: object, item_type: type[T]) -> tuple[T, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError(f"{name} must be an ordered sequence")

    copied = tuple(values)

    if any(not isinstance(value, item_type) for value in copied):
        raise TypeError(f"{name} must contain only {item_type.__name__} values")

    return copied


@dataclass(frozen=True)
class CoordinatedCandidate:
    """One display candidate with every equivalent retrieval provenance retained."""

    candidate: ReleaseCandidate
    provenance: tuple[MetadataProvenance, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, ReleaseCandidate):
            raise TypeError("candidate must be a ReleaseCandidate")

        provenance = _typed_tuple("provenance", self.provenance, MetadataProvenance)

        if not provenance:
            raise ValueError("a coordinated candidate must retain at least one provenance")

        if any(
            item.source_id != self.candidate.source_id
            or item.record_id != self.candidate.release_id
            for item in provenance
        ):
            raise ValueError("candidate provenance must refer to the same opaque source identity")

        object.__setattr__(self, "provenance", provenance)


@dataclass(frozen=True)
class ProviderFailure:
    """One expected provider failure labelled without retaining request data."""

    engine_id: str
    issue: Issue

    def __post_init__(self) -> None:
        if not isinstance(self.engine_id, str):
            raise TypeError("engine_id must be a string")

        if not self.engine_id:
            raise ValueError("engine_id must not be empty")

        if not isinstance(self.issue, Issue):
            raise TypeError("issue must be an Issue")


@dataclass(frozen=True)
class ProviderSearchSummary:
    """Per-engine search totals, including successful searches with no hits."""

    engine_id: str
    attempted_queries: int
    successful_queries: int
    candidate_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.engine_id, str) or not self.engine_id:
            raise ValueError("engine_id must be a non-empty string")

        for value in (self.attempted_queries, self.successful_queries, self.candidate_count):
            if type(value) is not int or value < 0:
                raise ValueError("search totals must be non-negative integers")

        if self.successful_queries > self.attempted_queries:
            raise ValueError("successful queries cannot exceed attempted queries")


@dataclass(frozen=True)
class ProviderSearchResult:
    """Ordered candidate successes and isolated failures from one lookup."""

    candidates: tuple[CoordinatedCandidate, ...]
    failures: tuple[ProviderFailure, ...]
    summaries: tuple[ProviderSearchSummary, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "candidates",
            _typed_tuple("candidates", self.candidates, CoordinatedCandidate),
        )
        object.__setattr__(
            self,
            "failures",
            _typed_tuple("failures", self.failures, ProviderFailure),
        )
        object.__setattr__(self, "summaries", _typed_tuple("summaries", self.summaries, ProviderSearchSummary))


@dataclass(frozen=True)
class ProviderEnrichmentResult:
    """One selected-release enrichment or its isolated provider failure."""

    candidate: CoordinatedCandidate | None
    failures: tuple[ProviderFailure, ...]

    def __post_init__(self) -> None:
        if self.candidate is not None and not isinstance(self.candidate, CoordinatedCandidate):
            raise TypeError("candidate must be a CoordinatedCandidate or None")

        failures = _typed_tuple("failures", self.failures, ProviderFailure)

        if (self.candidate is None) == (not failures):
            raise ValueError("enrichment must contain either one candidate or at least one failure")

        object.__setattr__(self, "failures", failures)


type CandidateIdentity = tuple[str, str, str]


class CandidateHydrationReasonCode(StrEnum):
    """Stable explanations for a candidate that could not be media-hydrated."""

    BASIC_MEDIA_LOAD_FAILED = "BASIC_MEDIA_LOAD_FAILED"
    BASIC_MEDIA_INVALID = "BASIC_MEDIA_INVALID"
    PER_ENGINE_LIMIT_REACHED = "PER_ENGINE_LIMIT_REACHED"


@dataclass(frozen=True)
class CandidateHydrationNotice:
    """Candidate-specific basic-media failure or deliberate hydration limit."""

    candidate_identity: CandidateIdentity
    reason_code: CandidateHydrationReasonCode
    issue: Issue | None

    def __post_init__(self) -> None:
        identity = self.candidate_identity

        if (
            not isinstance(identity, tuple)
            or len(identity) != 3
            or any(not isinstance(value, str) or not value for value in identity)
        ):
            raise TypeError("candidate_identity must contain three non-empty strings")

        if not isinstance(self.reason_code, CandidateHydrationReasonCode):
            raise TypeError("reason_code must be a CandidateHydrationReasonCode")

        if self.issue is not None and not isinstance(self.issue, Issue):
            raise TypeError("issue must be an Issue or None")

        if self.reason_code is CandidateHydrationReasonCode.PER_ENGINE_LIMIT_REACHED:
            if self.issue is not None:
                raise ValueError("a hydration limit notice cannot contain a provider issue")
        elif self.issue is None:
            raise ValueError("a failed hydration notice must contain a provider issue")


@dataclass(frozen=True)
class ProviderMediaResult:
    """Search candidates after bounded basic-media hydration."""

    candidates: tuple[CoordinatedCandidate, ...]
    notices: tuple[CandidateHydrationNotice, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "candidates",
            _typed_tuple("candidates", self.candidates, CoordinatedCandidate),
        )
        object.__setattr__(
            self,
            "notices",
            _typed_tuple("notices", self.notices, CandidateHydrationNotice),
        )


def _invalid_response_issue(error: Exception | None = None) -> Issue:
    technical_detail = None

    if error is not None:
        # Exception messages can contain URLs, query strings, or response
        # fragments. The type is sufficient for diagnostics at this boundary.
        technical_detail = f"provider_exception={type(error).__name__}"

    return Issue(
        code=ProviderErrorCode.INVALID_RESPONSE,
        message="The metadata provider returned an invalid response.",
        technical_detail=technical_detail,
    )


def _provenance(candidate: ReleaseCandidate, context: RequestContext) -> MetadataProvenance:
    return MetadataProvenance(
        engine_id=candidate.engine_id,
        source_id=candidate.source_id,
        record_id=candidate.release_id,
        source_url=candidate.source_url,
        # A release can contain several title languages, so no single language
        # can honestly describe the whole candidate at this boundary.
        language=None,
        operation_id=context.operation_id,
    )


def _candidate_identity(candidate: ReleaseCandidate) -> CandidateIdentity:
    return candidate.engine_id, candidate.source_id, candidate.release_id


def _contains_composers(candidate: ReleaseCandidate) -> bool:
    return any(
        track.composers
        for medium in candidate.media
        for track in medium.tracks
    )


def is_valid_basic_media(candidate: ReleaseCandidate) -> bool:
    """Require a genuine scoreable listing rather than a placeholder medium."""
    if not isinstance(candidate, ReleaseCandidate):
        raise TypeError("candidate must be a ReleaseCandidate")

    return (
        bool(candidate.media)
        and all(medium.tracks for medium in candidate.media)
        and not _contains_composers(candidate)
    )


class ProviderCoordinator:
    """Call configured providers synchronously in explicit priority order."""

    def __init__(self, providers: Sequence[MetadataProvider]) -> None:
        untrusted_providers: object = providers

        if isinstance(untrusted_providers, (str, bytes)) or not isinstance(untrusted_providers, Sequence):
            raise TypeError("providers must be an ordered sequence")

        ordered_providers = cast(Sequence[MetadataProvider], untrusted_providers)
        configured = tuple((provider.engine_id, provider) for provider in ordered_providers)
        engine_ids = tuple(engine_id for engine_id, _provider in configured)

        if any(not engine_id for engine_id in engine_ids):
            raise ValueError("provider engine ID must not be empty")

        if len(engine_ids) != len(set(engine_ids)):
            raise ValueError("duplicate provider engine ID")

        self._providers = configured
        self._provider_by_engine_id = dict(configured)

    @property
    def provider_count(self) -> int:
        """Return the configured provider count for bounded progress reporting."""
        return len(self._providers)

    def search_releases(
        self,
        query: ReleaseSearchQuery,
        context: RequestContext,
    ) -> ProviderSearchResult:
        """Search one strategy while retaining the ordered coordinator contract."""
        return self.search_release_queries((query,), context)

    def search_release_queries(
        self,
        queries: Sequence[ReleaseSearchQuery],
        context: RequestContext,
        *,
        check_cancelled: Callable[[], None] | None = None,
        on_call_started: Callable[[str], None] | None = None,
        on_call_finished: Callable[[str, Issue | None], None] | None = None,
    ) -> ProviderSearchResult:
        """Search strict-to-broad strategies in provider-priority order."""
        validated_queries = _typed_tuple("queries", queries, ReleaseSearchQuery)

        if not validated_queries:
            raise ValueError("queries must contain at least one search strategy")

        candidates: list[CoordinatedCandidate] = []
        failures: list[ProviderFailure] = []
        # De-duplicate by catalogue identity, not title similarity. Two printings
        # with equal titles may have different tracks and must remain selectable.
        candidate_indexes: dict[tuple[str, str], int] = {}
        summaries: list[ProviderSearchSummary] = []

        for engine_id, provider in self._providers:
            attempted_queries = 0
            successful_queries = 0
            found_identities: set[tuple[str, str]] = set()

            for query in validated_queries:
                if check_cancelled is not None:
                    check_cancelled()

                if on_call_started is not None:
                    on_call_started(engine_id)

                attempted_queries += 1

                try:
                    provider_candidates = _typed_tuple(
                        "provider candidates",
                        provider.search_releases(query, context),
                        ReleaseCandidate,
                    )
                except OperationCancelledError:
                    raise
                except ProviderTransportError as error:
                    failures.append(ProviderFailure(engine_id=engine_id, issue=error.issue))

                    if on_call_finished is not None:
                        on_call_finished(engine_id, error.issue)

                    # Broader title terms cannot resolve access controls or an
                    # exhausted rate limit. Other enabled engines still run.
                    if error.code in (
                        ProviderErrorCode.ACCESS_DENIED,
                        ProviderErrorCode.AUTHENTICATION_REQUIRED,
                        ProviderErrorCode.RATE_LIMITED,
                        ProviderErrorCode.PROXY_AUTHENTICATION_REQUIRED,
                        ProviderErrorCode.PROXY_CONNECTION_FAILED,
                        ProviderErrorCode.TLS_VERIFICATION_FAILED,
                    ):
                        break

                    continue
                except Exception as error:
                    issue = _invalid_response_issue(error)
                    failures.append(
                        ProviderFailure(
                            engine_id=engine_id,
                            issue=issue,
                        )
                    )

                    if on_call_finished is not None:
                        on_call_finished(engine_id, issue)

                    continue

                if any(candidate.engine_id != engine_id for candidate in provider_candidates):
                    issue = _invalid_response_issue()
                    failures.append(
                        ProviderFailure(
                            engine_id=engine_id,
                            issue=issue,
                        )
                    )

                    if on_call_finished is not None:
                        on_call_finished(engine_id, issue)

                    continue

                if on_call_finished is not None:
                    on_call_finished(engine_id, None)

                successful_queries += 1

                for candidate in provider_candidates:
                    identity = candidate.source_id, candidate.release_id
                    found_identities.add(identity)
                    provenance = _provenance(candidate, context)
                    existing_index = candidate_indexes.get(identity)

                    if existing_index is None:
                        candidate_indexes[identity] = len(candidates)
                        candidates.append(
                            CoordinatedCandidate(
                                candidate=candidate,
                                provenance=(provenance,),
                            )
                        )
                        continue

                    existing = candidates[existing_index]

                    if provenance in existing.provenance:
                        continue

                    # The first provider remains the display representative. Later
                    # equivalent hits contribute provenance rather than overwriting
                    # differently rich titles, media, or credits without scoring.
                    candidates[existing_index] = CoordinatedCandidate(
                        candidate=existing.candidate,
                        provenance=(*existing.provenance, provenance),
                    )

            summaries.append(ProviderSearchSummary(
                engine_id, attempted_queries, successful_queries, len(found_identities),
            ))

        return ProviderSearchResult(
            candidates=tuple(candidates),
            failures=tuple(failures),
            summaries=tuple(summaries),
        )

    def enrich_release(
        self,
        selected: CoordinatedCandidate,
        context: RequestContext,
        *,
        check_cancelled: Callable[[], None] | None = None,
        on_call_started: Callable[[str], None] | None = None,
        on_call_finished: Callable[[str, Issue | None], None] | None = None,
    ) -> ProviderEnrichmentResult:
        """Ask only the representative candidate's provider for expensive detail."""
        engine_id = selected.candidate.engine_id

        if check_cancelled is not None:
            check_cancelled()

        provider = self._provider_by_engine_id.get(engine_id)

        if provider is None:
            result = ProviderEnrichmentResult(
                candidate=None,
                failures=(
                    ProviderFailure(
                        engine_id=engine_id,
                        issue=Issue(
                            code=ProviderErrorCode.NOT_FOUND,
                            message="Switch Settings back to this candidate's provider, or start a new lookup.",
                        ),
                    ),
                ),
            )

            if on_call_finished is not None:
                on_call_finished(engine_id, result.failures[0].issue)

            return result

        if on_call_started is not None:
            on_call_started(engine_id)

        try:
            enriched_value: object = provider.enrich_release(selected.candidate, context)
        except OperationCancelledError:
            raise
        except ProviderTransportError as error:
            if check_cancelled is not None:
                check_cancelled()

            if on_call_finished is not None:
                on_call_finished(engine_id, error.issue)

            return ProviderEnrichmentResult(
                candidate=None,
                failures=(ProviderFailure(engine_id=engine_id, issue=error.issue),),
            )
        except Exception as error:
            if check_cancelled is not None:
                check_cancelled()

            result = ProviderEnrichmentResult(
                candidate=None,
                failures=(
                    ProviderFailure(
                        engine_id=engine_id,
                        issue=_invalid_response_issue(error),
                    ),
                ),
            )

            if on_call_finished is not None:
                on_call_finished(engine_id, result.failures[0].issue)

            return result

        if check_cancelled is not None:
            check_cancelled()

        if not isinstance(enriched_value, ReleaseCandidate):
            result = ProviderEnrichmentResult(
                candidate=None,
                failures=(
                    ProviderFailure(
                        engine_id=engine_id,
                        issue=_invalid_response_issue(TypeError("invalid enrichment result")),
                    ),
                ),
            )

            if on_call_finished is not None:
                on_call_finished(engine_id, result.failures[0].issue)

            return result

        enriched = enriched_value

        expected_identity = (
            selected.candidate.engine_id,
            selected.candidate.source_id,
            selected.candidate.release_id,
        )
        enriched_identity = (
            enriched.engine_id,
            enriched.source_id,
            enriched.release_id,
        )

        # Enrichment may add evidence but cannot switch the selected record or
        # its provider; review decisions depend on that identity staying stable.
        if enriched_identity != expected_identity:
            result = ProviderEnrichmentResult(
                candidate=None,
                failures=(
                    ProviderFailure(
                        engine_id=engine_id,
                        issue=_invalid_response_issue(),
                    ),
                ),
            )

            if on_call_finished is not None:
                on_call_finished(engine_id, result.failures[0].issue)

            return result

        if on_call_finished is not None:
            on_call_finished(engine_id, None)

        return ProviderEnrichmentResult(
            candidate=CoordinatedCandidate(
                candidate=enriched,
                provenance=selected.provenance,
            ),
            failures=(),
        )

    def load_release_media(
        self,
        candidates: Sequence[CoordinatedCandidate],
        context: RequestContext,
        *,
        per_engine_limit: int = 5,
        check_cancelled: Callable[[], None] | None = None,
        on_call_started: Callable[[str], None] | None = None,
        on_call_finished: Callable[[str, Issue | None], None] | None = None,
    ) -> ProviderMediaResult:
        """Hydrate a bounded number of candidates per representative engine."""
        selected_candidates = _typed_tuple(
            "candidates",
            candidates,
            CoordinatedCandidate,
        )

        if type(per_engine_limit) is not int:
            raise TypeError("per_engine_limit must be an integer")

        if per_engine_limit <= 0:
            raise ValueError("per_engine_limit must be greater than zero")

        attempts_by_engine: dict[str, int] = {}
        hydrated_candidates: list[CoordinatedCandidate] = []
        notices: list[CandidateHydrationNotice] = []

        for selected in selected_candidates:
            if check_cancelled is not None:
                check_cancelled()

            candidate = selected.candidate
            identity = _candidate_identity(candidate)

            # Reusing an already-basic candidate is harmless, but composer-bearing
            # data must never cross back into the search-result boundary.
            if is_valid_basic_media(candidate):
                hydrated_candidates.append(selected)
                continue

            attempted = attempts_by_engine.get(candidate.engine_id, 0)

            if attempted >= per_engine_limit:
                hydrated_candidates.append(selected)
                notices.append(
                    CandidateHydrationNotice(
                        candidate_identity=identity,
                        reason_code=CandidateHydrationReasonCode.PER_ENGINE_LIMIT_REACHED,
                        issue=None,
                    )
                )
                continue

            # Count attempted loads, including failures, to keep a broken
            # provider from expanding a bounded hydration pass into many calls.
            attempts_by_engine[candidate.engine_id] = attempted + 1
            provider = self._provider_by_engine_id.get(candidate.engine_id)

            if provider is None:
                issue = Issue(
                    code=ProviderErrorCode.NOT_FOUND,
                    message="No enabled provider can load media for this candidate.",
                )
                hydrated_candidates.append(selected)
                notices.append(
                    CandidateHydrationNotice(
                        candidate_identity=identity,
                        reason_code=CandidateHydrationReasonCode.BASIC_MEDIA_LOAD_FAILED,
                        issue=issue,
                    )
                )

                if on_call_finished is not None:
                    on_call_finished(candidate.engine_id, issue)

                continue

            if on_call_started is not None:
                on_call_started(candidate.engine_id)

            try:
                loaded_value: object = provider.load_release_media(candidate, context)
            except OperationCancelledError:
                raise
            except ProviderTransportError as error:
                hydrated_candidates.append(selected)
                notices.append(
                    CandidateHydrationNotice(
                        candidate_identity=identity,
                        reason_code=CandidateHydrationReasonCode.BASIC_MEDIA_LOAD_FAILED,
                        issue=error.issue,
                    )
                )

                if on_call_finished is not None:
                    on_call_finished(candidate.engine_id, error.issue)

                continue
            except Exception as error:
                issue = _invalid_response_issue(error)
                hydrated_candidates.append(selected)
                notices.append(
                    CandidateHydrationNotice(
                        candidate_identity=identity,
                        reason_code=CandidateHydrationReasonCode.BASIC_MEDIA_LOAD_FAILED,
                        issue=issue,
                    )
                )

                if on_call_finished is not None:
                    on_call_finished(candidate.engine_id, issue)

                continue

            if (
                not isinstance(loaded_value, ReleaseCandidate)
                or _candidate_identity(loaded_value) != identity
                or not is_valid_basic_media(loaded_value)
            ):
                issue = _invalid_response_issue()
                hydrated_candidates.append(selected)
                notices.append(
                    CandidateHydrationNotice(
                        candidate_identity=identity,
                        reason_code=CandidateHydrationReasonCode.BASIC_MEDIA_INVALID,
                        issue=issue,
                    )
                )

                if on_call_finished is not None:
                    on_call_finished(candidate.engine_id, issue)

                continue

            hydrated_candidates.append(
                CoordinatedCandidate(
                    candidate=loaded_value,
                    provenance=selected.provenance,
                )
            )

            if on_call_finished is not None:
                on_call_finished(candidate.engine_id, None)

        return ProviderMediaResult(
            candidates=tuple(hydrated_candidates),
            notices=tuple(notices),
        )
