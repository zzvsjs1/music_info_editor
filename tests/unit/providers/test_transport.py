from collections.abc import Callable
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from metadata_polisher.domain.errors import ProviderErrorCode
from metadata_polisher.providers.transport import (
    ProviderTransport,
    ProviderTransportError,
    TransportPolicy,
)


# Record both elapsed virtual time and each delay; retries and rate limits
# can then be checked exactly without making the suite sleep in real time.
class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.mark.parametrize("status, expected_code", [(401, "AUTHENTICATION_REQUIRED"), (403, "ACCESS_DENIED")])
def test_forbidden_access_does_not_claim_sign_in_is_required(status: int, expected_code: str) -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, text="private response body")

    transport, client, _ = make_transport(handler)

    with client, pytest.raises(ProviderTransportError) as caught:
        transport.get_text("https://provider.invalid/search")

    assert caught.value.code.value == expected_code
    assert len(requests) == 1
    assert caught.value.issue.technical_detail is not None
    assert f"status={status}" in caught.value.issue.technical_detail
    assert "private response body" not in str(caught.value.issue)

    if status == 403:
        assert "blocked" in caught.value.issue.message.lower()
        assert "authentication" not in caught.value.issue.message.lower()


def make_transport(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    clock: FakeClock | None = None,
    policy: TransportPolicy | None = None,
) -> tuple[ProviderTransport, httpx.Client, FakeClock]:
    fake_clock = clock or FakeClock()
    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider_transport = ProviderTransport(
        client=client,
        policy=policy
        or TransportPolicy(
            timeout_seconds=2.0,
            minimum_interval_seconds=0.0,
            max_attempts=3,
            retry_delay_seconds=0.0,
            max_retry_after_seconds=10.0,
        ),
        monotonic=fake_clock.monotonic,
        sleeper=fake_clock.sleep,
    )

    return provider_transport, client, fake_clock


def test_requests_start_at_least_the_minimum_interval_apart() -> None:
    clock = FakeClock()
    request_starts: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        request_starts.append(clock.monotonic())
        return httpx.Response(200, json={"ok": True})

    transport, client, _ = make_transport(
        handler,
        clock=clock,
        policy=TransportPolicy(
            timeout_seconds=2,
            minimum_interval_seconds=1.5,
            max_attempts=1,
        ),
    )

    try:
        assert transport.get_json("https://provider.invalid/one") == {"ok": True}
        assert transport.get_json("https://provider.invalid/two") == {"ok": True}
    finally:
        client.close()

    assert request_starts == [0.0, 1.5]
    assert clock.sleeps == [1.5]


def test_timeout_is_retried_within_the_attempt_budget() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1

        if calls == 1:
            raise httpx.ReadTimeout("socket detail must stay private", request=request)

        return httpx.Response(200, json={"release": "found"})

    transport, client, clock = make_transport(
        handler,
        policy=TransportPolicy(
            timeout_seconds=2,
            minimum_interval_seconds=0,
            max_attempts=2,
            retry_delay_seconds=0.25,
        ),
    )

    try:
        result = transport.get_json("https://provider.invalid/releases")
    finally:
        client.close()

    assert result == {"release": "found"}
    assert calls == 2
    assert clock.sleeps == [0.25]


def test_service_unavailable_is_retried() -> None:
    statuses = [503, 200]

    def handler(_request: httpx.Request) -> httpx.Response:
        status = statuses.pop(0)
        return httpx.Response(status, json={"status": status})

    transport, client, _ = make_transport(
        handler,
        policy=TransportPolicy(
            timeout_seconds=2,
            minimum_interval_seconds=0,
            max_attempts=2,
        ),
    )

    try:
        result = transport.get_json("https://provider.invalid/releases")
    finally:
        client.close()

    assert result == {"status": 200}
    assert statuses == []


def test_text_responses_share_retry_and_polite_rate_limit_policy() -> None:
    clock = FakeClock()
    request_starts: list[float] = []
    statuses = [503, 200]

    def handler(_request: httpx.Request) -> httpx.Response:
        request_starts.append(clock.monotonic())
        status = statuses.pop(0)

        if status == 503:
            return httpx.Response(status, text="temporarily unavailable")

        return httpx.Response(
            status,
            content=b"Soundtrack caf\xe9",
            headers={"Content-Type": "text/html; charset=iso-8859-1"},
        )

    transport, client, _ = make_transport(
        handler,
        clock=clock,
        policy=TransportPolicy(
            timeout_seconds=2,
            minimum_interval_seconds=1,
            max_attempts=2,
            retry_delay_seconds=0.25,
        ),
    )

    try:
        result = transport.get_text("https://provider.invalid/search")
    finally:
        client.close()

    assert result == "Soundtrack café"
    assert request_starts == [0.0, 1.0]
    assert clock.sleeps == [0.25, 0.75]
    assert statuses == []


# Different transient failures spend the same budget. Switching error types
# must never reset the retry count and permit an unbounded request sequence.
def test_timeout_503_and_429_share_one_bounded_attempt_budget() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1

        if calls == 1:
            raise httpx.ReadTimeout("private timeout detail", request=request)

        if calls == 2:
            return httpx.Response(503, text="private service body")

        return httpx.Response(429, headers={"Retry-After": "4"}, text="private rate body")

    transport, client, clock = make_transport(handler)

    try:
        with pytest.raises(ProviderTransportError) as caught:
            transport.get_json("https://provider.invalid/releases?token=private")
    finally:
        client.close()

    assert calls == 3
    assert clock.sleeps == []
    assert caught.value.code is ProviderErrorCode.RATE_LIMITED
    assert caught.value.context.attempt_count == 3
    assert caught.value.context.status_code == 429
    assert caught.value.context.retry_after_seconds == 4.0


@pytest.mark.parametrize(
    ("retry_after", "maximum", "expected_sleep"),
    [
        ("3", 10.0, 3.0),
        ("not-a-delay", 4.0, 0.5),
        ("-3", 4.0, 0.5),
    ],
)
def test_429_uses_only_a_valid_bounded_retry_after_delay(
    retry_after: str,
    maximum: float,
    expected_sleep: float,
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1

        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": retry_after})

        return httpx.Response(200, json={"ok": True})

    transport, client, clock = make_transport(
        handler,
        policy=TransportPolicy(
            timeout_seconds=2,
            minimum_interval_seconds=0,
            max_attempts=2,
            retry_delay_seconds=0.5,
            max_retry_after_seconds=maximum,
        ),
    )

    try:
        assert transport.get_json("https://provider.invalid/releases") == {"ok": True}
    finally:
        client.close()

    assert calls == 2
    assert clock.sleeps == [expected_sleep]


def test_excessive_retry_after_is_not_clamped_to_an_earlier_retry() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "999999"})

    transport, client, clock = make_transport(
        handler,
        policy=TransportPolicy(
            timeout_seconds=2,
            minimum_interval_seconds=0,
            max_attempts=2,
            retry_delay_seconds=0.5,
            max_retry_after_seconds=4,
        ),
    )

    try:
        with pytest.raises(ProviderTransportError) as caught:
            transport.get_json("https://provider.invalid/releases")
    finally:
        client.close()

    assert calls == 1
    assert clock.sleeps == []
    assert caught.value.code is ProviderErrorCode.RATE_LIMITED
    assert caught.value.context.retry_after_seconds == 999999.0


@pytest.mark.parametrize("delay_seconds", [4, 60])
def test_http_date_retry_after_is_honoured_without_retrying_early(
    delay_seconds: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    wall_time = datetime(2026, 9, 5, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(
        "metadata_polisher.providers.transport.time.time",
        lambda: wall_time.timestamp() + clock.now,
    )
    retry_after = format_datetime(wall_time + timedelta(seconds=delay_seconds), usegmt=True)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1

        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": retry_after})

        return httpx.Response(200, json={"ok": True})

    transport, client, _ = make_transport(
        handler,
        clock=clock,
        policy=TransportPolicy(
            timeout_seconds=2,
            minimum_interval_seconds=0,
            max_attempts=2,
            retry_delay_seconds=0.5,
            max_retry_after_seconds=10,
        ),
    )

    try:
        if delay_seconds <= 10:
            assert transport.get_json("https://provider.invalid/releases") == {"ok": True}
            assert calls == 2
            assert clock.sleeps == [float(delay_seconds)]
        else:
            # A server deadline beyond the bounded wait must stop this lookup;
            # replacing it with the ordinary retry delay would violate the limit.
            with pytest.raises(ProviderTransportError) as caught:
                transport.get_json("https://provider.invalid/releases")

            assert calls == 1
            assert clock.sleeps == []
            assert caught.value.code is ProviderErrorCode.RATE_LIMITED
            assert caught.value.context.retry_after_seconds == float(delay_seconds)
    finally:
        client.close()


def test_404_is_not_retried() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404, text="private response body")

    transport, client, clock = make_transport(handler)

    try:
        with pytest.raises(ProviderTransportError) as caught:
            transport.get_json("https://provider.invalid/releases")
    finally:
        client.close()

    assert calls == 1
    assert clock.sleeps == []
    assert caught.value.code is ProviderErrorCode.NOT_FOUND
    assert caught.value.context.status_code == 404


def test_invalid_json_is_not_retried_and_diagnostics_are_allow_listed() -> None:
    calls = 0
    private_values = (
        "private-token",
        "private response body",
        "https://provider.invalid/releases",
        "Authorization",
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="private response body: not JSON")

    transport, client, clock = make_transport(handler)

    try:
        with pytest.raises(ProviderTransportError) as caught:
            transport.get_json(
                "https://provider.invalid/releases?token=private-token",
                headers={"Authorization": "Bearer private-token"},
            )
    finally:
        client.close()

    error = caught.value
    rendered_diagnostics = f"{error!r} {error.args!r} {error.context!r}"

    assert calls == 1
    assert clock.sleeps == []
    assert error.code is ProviderErrorCode.INVALID_RESPONSE
    assert vars(error.context).keys() == {
        "attempt_count",
        "status_code",
        "retry_after_seconds",
    }
    assert all(private_value not in rendered_diagnostics for private_value in private_values)
    assert error.__cause__ is None
    assert error.__context__ is None


def test_network_error_does_not_retain_third_party_exception_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("host lookup included private-token", request=request)

    transport, client, _ = make_transport(handler)

    try:
        with pytest.raises(ProviderTransportError) as caught:
            transport.get_json("https://private-token@provider.invalid/releases")
    finally:
        client.close()

    error = caught.value

    assert error.code is ProviderErrorCode.NETWORK_ERROR
    assert "private-token" not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None


def test_error_context_is_frozen() -> None:
    transport, client, _ = make_transport(lambda _request: httpx.Response(404))

    try:
        with pytest.raises(ProviderTransportError) as caught:
            transport.get_json("https://provider.invalid/missing")
    finally:
        client.close()

    with pytest.raises(FrozenInstanceError):
        caught.value.context.status_code = 200


@pytest.mark.parametrize(
    "policy_values",
    [
        {"timeout_seconds": 0, "minimum_interval_seconds": 0},
        {"timeout_seconds": float("inf"), "minimum_interval_seconds": 0},
        {"timeout_seconds": 1, "minimum_interval_seconds": -1},
        {"timeout_seconds": 1, "minimum_interval_seconds": 0, "max_attempts": 0},
        {"timeout_seconds": 1, "minimum_interval_seconds": 0, "max_attempts": True},
        {"timeout_seconds": 1, "minimum_interval_seconds": 0, "retry_delay_seconds": -1},
        {"timeout_seconds": 1, "minimum_interval_seconds": 0, "max_retry_after_seconds": -1},
    ],
)
def test_transport_policy_rejects_unsafe_values(policy_values: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        TransportPolicy(**policy_values)  # type: ignore[arg-type]
