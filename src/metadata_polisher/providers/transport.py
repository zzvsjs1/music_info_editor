"""Synchronous HTTP transport with central rate-limit and retry policy."""

import math
import ssl
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime

import httpx

from metadata_polisher.domain.errors import Issue, ProviderErrorCode
from metadata_polisher.execution.events import OperationStageChanged
from metadata_polisher.providers.base import RequestContext

_ERROR_MESSAGES: dict[ProviderErrorCode, str] = {
    ProviderErrorCode.NETWORK_TIMEOUT: "The metadata provider request timed out.",
    ProviderErrorCode.NETWORK_ERROR: "The metadata provider could not be reached.",
    ProviderErrorCode.PROXY_CONNECTION_FAILED: (
        "The configured HTTP proxy could not complete the connection. No direct fallback was attempted."
    ),
    ProviderErrorCode.PROXY_AUTHENTICATION_REQUIRED: "The HTTP proxy requires valid session credentials.",
    ProviderErrorCode.TLS_VERIFICATION_FAILED: "The external service's HTTPS certificate could not be verified.",
    ProviderErrorCode.RATE_LIMITED: "The metadata provider rate limit was reached.",
    ProviderErrorCode.SERVICE_UNAVAILABLE: "The metadata provider is temporarily unavailable.",
    ProviderErrorCode.AUTHENTICATION_REQUIRED: "The metadata provider requires authentication.",
    ProviderErrorCode.ACCESS_DENIED: (
        "The selected metadata provider blocked this app's request. No other provider was contacted."
    ),
    ProviderErrorCode.INVALID_RESPONSE: "The metadata provider returned an invalid response.",
    ProviderErrorCode.NOT_FOUND: "The requested metadata provider record was not found.",
}


def _normalise_seconds(name: str, value: object, *, allow_zero: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")

    normalised = float(value)
    minimum_is_valid = normalised >= 0 if allow_zero else normalised > 0

    if not math.isfinite(normalised) or not minimum_is_valid:
        condition = "non-negative and finite" if allow_zero else "greater than zero and finite"
        raise ValueError(f"{name} must be {condition}")

    return normalised


@dataclass(frozen=True)
class TransportPolicy:
    """Provider-specific timeout, politeness, and bounded retry settings."""

    timeout_seconds: float
    minimum_interval_seconds: float
    max_attempts: int = 3
    retry_delay_seconds: float = 0.5
    max_retry_after_seconds: float = 30.0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "timeout_seconds",
            _normalise_seconds("timeout_seconds", self.timeout_seconds, allow_zero=False),
        )
        object.__setattr__(
            self,
            "minimum_interval_seconds",
            _normalise_seconds(
                "minimum_interval_seconds",
                self.minimum_interval_seconds,
                allow_zero=True,
            ),
        )

        if type(self.max_attempts) is not int:
            raise TypeError("max_attempts must be an integer")

        if self.max_attempts <= 0:
            raise ValueError("max_attempts must be greater than zero")

        object.__setattr__(
            self,
            "retry_delay_seconds",
            _normalise_seconds("retry_delay_seconds", self.retry_delay_seconds, allow_zero=True),
        )
        object.__setattr__(
            self,
            "max_retry_after_seconds",
            _normalise_seconds(
                "max_retry_after_seconds",
                self.max_retry_after_seconds,
                allow_zero=True,
            ),
        )


@dataclass(frozen=True)
class ProviderTransportErrorContext:
    """Allow-listed numeric diagnostics that cannot retain request secrets."""

    attempt_count: int
    status_code: int | None = None
    retry_after_seconds: float | None = None

    def __post_init__(self) -> None:
        if type(self.attempt_count) is not int:
            raise TypeError("attempt_count must be an integer")

        if self.attempt_count <= 0:
            raise ValueError("attempt_count must be greater than zero")

        if self.status_code is not None:
            if type(self.status_code) is not int:
                raise TypeError("status_code must be an integer or None")

            if not 100 <= self.status_code <= 599:
                raise ValueError("status_code must be a valid HTTP status or None")

        if self.retry_after_seconds is not None:
            object.__setattr__(
                self,
                "retry_after_seconds",
                _normalise_seconds(
                    "retry_after_seconds",
                    self.retry_after_seconds,
                    allow_zero=True,
                ),
            )


class ProviderTransportError(RuntimeError):
    """Expected provider transport failure with secret-safe diagnostics."""

    def __init__(
        self,
        *,
        code: ProviderErrorCode,
        context: ProviderTransportErrorContext,
    ) -> None:
        message = _ERROR_MESSAGES[code]
        self.code = code
        self.context = context
        rendered_context = (
            f"attempts={context.attempt_count}, status={context.status_code}, "
            f"retry_after={context.retry_after_seconds}"
        )
        self.issue = Issue(code=code, message=message, technical_detail=rendered_context)
        super().__init__(f"{code.value}: {message} ({rendered_context})")


@dataclass
class ProviderRateLimit:
    """Share provider timing across captured clients without changing their route."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    last_request_started_at: float | None = None


class ProviderTransport:
    """Make GET requests without leaking provider-specific data into policy."""

    def __init__(
        self,
        *,
        client: httpx.Client,
        policy: TransportPolicy,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        rate_limit: ProviderRateLimit | None = None,
        proxy_mode: bool = False,
    ) -> None:
        self._client = client
        self._policy = policy
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._rate_limit = rate_limit if rate_limit is not None else ProviderRateLimit()
        self._proxy_mode = proxy_mode

    def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        context: RequestContext | None = None,
    ) -> object:
        """Return decoded JSON or raise a typed, redacted transport error."""
        response, attempt_count = self._get_response(url, params=params, headers=headers, context=context)
        decoded, value = self._decode_json(response)

        if not decoded:
            raise self._error(
                ProviderErrorCode.INVALID_RESPONSE,
                attempt_count=attempt_count,
                status_code=response.status_code,
            )

        return value

    def get_text(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        context: RequestContext | None = None,
    ) -> str:
        """Return decoded response text through the shared rate and retry policy."""
        response, attempt_count = self._get_response(url, params=params, headers=headers, context=context)

        try:
            return response.text
        except (LookupError, UnicodeError):
            raise self._error(
                ProviderErrorCode.INVALID_RESPONSE,
                attempt_count=attempt_count,
                status_code=response.status_code,
            ) from None

    def _get_response(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None,
        headers: Mapping[str, str] | None,
        context: RequestContext | None,
    ) -> tuple[httpx.Response, int]:
        """Apply the common request policy before format-specific decoding."""
        # The attempt budget includes the first request. Only explicitly handled
        # transient outcomes re-enter this loop; access failures stop immediately.
        for attempt_count in range(1, self._policy.max_attempts + 1):
            self._check_cancelled(context)
            self._wait_for_request_start(context)
            self._stage(context, "requesting_provider")
            response_or_error = self._send(url, params=params, headers=headers)
            # Synchronous HTTP cannot safely be force-killed. Its finite timeout
            # bounds the request; discard the response when cancellation arrived.
            self._check_cancelled(context)

            if isinstance(response_or_error, ProviderErrorCode):
                if response_or_error is ProviderErrorCode.NETWORK_TIMEOUT and self._can_retry(attempt_count):
                    self._sleep_before_retry(self._policy.retry_delay_seconds, context)
                    continue

                raise self._error(
                    response_or_error,
                    attempt_count=attempt_count,
                )

            response = response_or_error

            if response.status_code == 429:
                retry_after, retry_after_is_within_bound = self._bounded_retry_after(response)

                # Do not cap a long server delay and retry too early. Decline
                # the retry when respecting the delay would exceed our wait bound.
                if not retry_after_is_within_bound:
                    raise self._error(
                        ProviderErrorCode.RATE_LIMITED,
                        attempt_count=attempt_count,
                        status_code=response.status_code,
                        retry_after_seconds=retry_after,
                    )

                if self._can_retry(attempt_count):
                    delay = retry_after if retry_after is not None else self._policy.retry_delay_seconds
                    self._sleep_before_retry(delay, context)
                    continue

                raise self._error(
                    ProviderErrorCode.RATE_LIMITED,
                    attempt_count=attempt_count,
                    status_code=response.status_code,
                    retry_after_seconds=retry_after,
                )

            if response.status_code == 503:
                if self._can_retry(attempt_count):
                    self._sleep_before_retry(self._policy.retry_delay_seconds, context)
                    continue

                raise self._error(
                    ProviderErrorCode.SERVICE_UNAVAILABLE,
                    attempt_count=attempt_count,
                    status_code=response.status_code,
                )

            status_error = self._status_error(response.status_code)

            if status_error is not None:
                raise self._error(
                    status_error,
                    attempt_count=attempt_count,
                    status_code=response.status_code,
                )

            return response, attempt_count

        # The validated positive attempt count and exhaustive loop branches make
        # this unreachable, but retaining the guard keeps the control flow explicit.
        raise AssertionError("Transport attempt loop ended unexpectedly")

    def _wait_for_request_start(self, context: RequestContext | None) -> None:
        # Serialising only the start-time calculation makes one shared transport
        # preserve its provider's polite request interval across worker threads.
        while not self._rate_limit.lock.acquire(timeout=0.1):
            self._check_cancelled(context)

        try:
            now = self._monotonic()

            if self._rate_limit.last_request_started_at is not None:
                earliest_start = self._rate_limit.last_request_started_at + self._policy.minimum_interval_seconds
                remaining = earliest_start - now

                if remaining > 0:
                    self._stage(context, "waiting_for_rate_limit")
                    self._wait(remaining, context)

            self._check_cancelled(context)
            self._rate_limit.last_request_started_at = self._monotonic()
        finally:
            self._rate_limit.lock.release()

    def _send(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None,
        headers: Mapping[str, str] | None,
    ) -> httpx.Response | ProviderErrorCode:
        # Third-party exceptions are converted inside this helper and never
        # retained as a cause/context on the public transport error.
        try:
            return self._client.get(
                url,
                params=params,
                headers=headers,
                timeout=self._policy.timeout_seconds,
            )
        except httpx.ConnectTimeout:
            return ProviderErrorCode.PROXY_CONNECTION_FAILED if self._proxy_mode else ProviderErrorCode.NETWORK_TIMEOUT
        except httpx.TimeoutException:
            return ProviderErrorCode.NETWORK_TIMEOUT
        except httpx.ProxyError as error:
            # HTTPX represents a CONNECT rejection as ProxyError rather than a
            # Response. Read only its fixed status prefix, never export the
            # potentially credential-bearing reason text or exception chain.
            if str(error).partition(" ")[0] == "407":
                return ProviderErrorCode.PROXY_AUTHENTICATION_REQUIRED

            return ProviderErrorCode.PROXY_CONNECTION_FAILED
        except httpx.ConnectError as error:
            cause: BaseException | None = error
            visited: set[int] = set()

            while cause is not None and id(cause) not in visited:
                if isinstance(cause, ssl.SSLCertVerificationError):
                    return ProviderErrorCode.TLS_VERIFICATION_FAILED

                visited.add(id(cause))
                cause = cause.__cause__ or cause.__context__

            return ProviderErrorCode.PROXY_CONNECTION_FAILED if self._proxy_mode else ProviderErrorCode.NETWORK_ERROR
        except httpx.RequestError:
            return ProviderErrorCode.NETWORK_ERROR

    def _can_retry(self, attempt_count: int) -> bool:
        return attempt_count < self._policy.max_attempts

    def _sleep_before_retry(self, delay_seconds: float, context: RequestContext | None) -> None:
        if delay_seconds > 0:
            self._stage(context, "waiting_to_retry")
            self._wait(delay_seconds, context)

    def _wait(self, delay_seconds: float, context: RequestContext | None) -> None:
        if context is None or context.cancellation is None:
            self._sleeper(delay_seconds)
            return

        # Short slices make rate/retry waits responsive to cancellation. Use a
        # monotonic deadline so wall-clock adjustments cannot extend the wait.
        deadline = self._monotonic() + delay_seconds

        while (remaining := deadline - self._monotonic()) > 0:
            self._check_cancelled(context)
            self._sleeper(min(0.1, remaining))

        self._check_cancelled(context)

    @staticmethod
    def _check_cancelled(context: RequestContext | None) -> None:
        if context is not None and context.cancellation is not None:
            context.cancellation.raise_if_cancelled()

    @staticmethod
    def _stage(context: RequestContext | None, stage: str) -> None:
        if context is not None and context.events is not None:
            context.events.emit(OperationStageChanged(context.operation_id, stage))

    def _bounded_retry_after(self, response: httpx.Response) -> tuple[float | None, bool]:
        raw_value = response.headers.get("Retry-After")

        if raw_value is None:
            return None, True

        candidate = raw_value.strip()

        if not candidate or not candidate.isascii() or not candidate.isdecimal():
            try:
                deadline = parsedate_to_datetime(candidate)
            except (TypeError, ValueError, OverflowError):
                return None, True

            if deadline.tzinfo is None:
                return None, True

            # HTTP dates are absolute UTC deadlines. Convert once to a relative
            # wait, then keep request spacing on the monotonic clock as before.
            # An excessive wait returns RATE_LIMITED instead of retrying early.
            retry_after = max(0.0, deadline.timestamp() - time.time())

            return retry_after, retry_after <= self._policy.max_retry_after_seconds

        # Bound conversion before int(): an enormous untrusted header is already
        # beyond the supported wait, so parsing every digit brings no benefit.
        significant_digits = candidate.lstrip("0") or "0"

        if len(significant_digits) > 18:
            return self._policy.max_retry_after_seconds + 1.0, False

        retry_after = float(int(significant_digits))

        return retry_after, retry_after <= self._policy.max_retry_after_seconds

    @staticmethod
    def _status_error(status_code: int) -> ProviderErrorCode | None:
        if 200 <= status_code <= 299:
            return None

        if status_code == 404:
            return ProviderErrorCode.NOT_FOUND

        if status_code == 401:
            return ProviderErrorCode.AUTHENTICATION_REQUIRED

        if status_code == 407:
            return ProviderErrorCode.PROXY_AUTHENTICATION_REQUIRED

        if status_code == 403:
            return ProviderErrorCode.ACCESS_DENIED

        if 500 <= status_code <= 599:
            return ProviderErrorCode.SERVICE_UNAVAILABLE

        return ProviderErrorCode.NETWORK_ERROR

    @staticmethod
    def _decode_json(response: httpx.Response) -> tuple[bool, object]:
        try:
            return True, response.json()
        except (UnicodeError, ValueError):
            return False, None

    @staticmethod
    def _error(
        code: ProviderErrorCode,
        *,
        attempt_count: int,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> ProviderTransportError:
        return ProviderTransportError(
            code=code,
            context=ProviderTransportErrorContext(
                attempt_count=attempt_count,
                status_code=status_code,
                retry_after_seconds=retry_after_seconds,
            ),
        )
