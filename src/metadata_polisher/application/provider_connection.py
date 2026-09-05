"""One explicit selected-adapter test with a serialisable, secret-free result."""

from dataclasses import dataclass, replace

from metadata_polisher.application.lookup import LookupService
from metadata_polisher.domain.errors import Issue
from metadata_polisher.execution.cancellation import CancellationToken
from metadata_polisher.execution.events import (
    OperationEventSink,
    OperationStageChanged,
    ProviderCompleted,
    ProviderFailed,
    ProviderStarted,
)
from metadata_polisher.providers.base import RequestContext


@dataclass(frozen=True)
class ProviderConnectionResult:
    """An empty valid catalogue response is a successful connection test."""

    operation_id: str
    provider_id: str
    issue: Issue | None = None


def test_provider_connection(
    service: LookupService,
    provider_id: str,
    context: RequestContext,
    cancellation: CancellationToken,
    events: OperationEventSink,
) -> ProviderConnectionResult:
    """Use the same adapter/parser boundary as lookup, without claiming a match."""
    cancellation.raise_if_cancelled()
    context = replace(context, cancellation=cancellation, events=events)
    events.emit(OperationStageChanged(context.operation_id, "testing_selected_provider"))
    events.emit(ProviderStarted(context.operation_id, provider_id))
    # Exercise the configured provider's normal request and parser path. Merely
    # reaching an HTTP server would also accept a challenge page as a connection.
    failures = service.test_connection(context)
    cancellation.raise_if_cancelled()
    issue = failures[0].issue if failures else None
    events.emit(
        ProviderCompleted(context.operation_id, provider_id)
        if issue is None else ProviderFailed(context.operation_id, provider_id, issue)
    )
    # Return only labelled outcome data; the request context can contain session
    # credentials and must not become part of the displayed or retained result.
    return ProviderConnectionResult(context.operation_id, provider_id, issue)
