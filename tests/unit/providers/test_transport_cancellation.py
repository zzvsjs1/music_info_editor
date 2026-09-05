"""Cancellation reaches retry/rate waits and the bounded request boundary."""

import httpx
import pytest

from metadata_polisher.execution.cancellation import MutableCancellationToken, OperationCancelledError
from metadata_polisher.providers.base import RequestContext
from metadata_polisher.providers.transport import ProviderTransport, ProviderTransportError, TransportPolicy
from tests.unit.application.test_lookup_service import RecordingEventSink


@pytest.mark.parametrize("waiting", ["rate_limit", "retry"])
def test_transport_wait_cancellation_stops_before_the_next_request(waiting):
    token = MutableCancellationToken()
    sink = RecordingEventSink()
    calls = []
    sleeps = []
    now = [0.0]

    def respond(request):
        calls.append(request)
        return httpx.Response(503 if waiting == "retry" else 200, json={})

    # Request cancellation in the first virtual sleep slice. This proves a
    # long policy delay is interruptible before another HTTP request starts.
    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
        token.cancel()

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        transport = ProviderTransport(
            client=client,
            policy=TransportPolicy(2, 30, max_attempts=2, retry_delay_seconds=30),
            monotonic=lambda: now[0], sleeper=sleep,
        )

        if waiting == "rate_limit":
            transport.get_json("https://provider.invalid/first")

        context = RequestContext("LOOKUP-0301", "auto", cancellation=token, events=sink)

        with pytest.raises(OperationCancelledError):
            transport.get_json("https://provider.invalid/next", context=context)

    assert len(calls) == 1
    assert sleeps and max(sleeps) <= 0.1
    assert any(getattr(event, "stage", "") in {"waiting_for_rate_limit", "waiting_to_retry"} for event in sink.events)


def test_in_flight_response_is_discarded_after_cancellation_without_another_request():
    token = MutableCancellationToken()
    calls = []

    def respond(request):
        calls.append(request)
        # Model a stop request arriving during blocking HTTP: the response
        # returns normally, but its now-stale payload must be discarded.
        token.cancel()
        return httpx.Response(200, json={"private": "stale result"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        transport = ProviderTransport(client=client, policy=TransportPolicy(2, 0, max_attempts=2))

        with pytest.raises(OperationCancelledError):
            transport.get_json(
                "https://provider.invalid/record",
                context=RequestContext("LOOKUP-0302", "auto", cancellation=token),
            )

    assert len(calls) == 1
    assert calls[0].extensions["timeout"]["read"] == 2


def test_proxy_authentication_is_distinct_from_provider_authentication():
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(407))) as client:
        transport = ProviderTransport(client=client, policy=TransportPolicy(2, 0, max_attempts=1))

        with pytest.raises(ProviderTransportError) as caught:
            transport.get_json("https://provider.invalid/record")

    assert caught.value.code.value == "PROXY_AUTHENTICATION_REQUIRED"
    assert "proxy" in caught.value.issue.message.casefold()
