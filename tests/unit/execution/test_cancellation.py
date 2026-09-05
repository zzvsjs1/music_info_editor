import pytest

from metadata_polisher.execution.cancellation import (
    MutableCancellationToken,
    NeverCancelledToken,
    OperationCancelledError,
)


def test_never_cancelled_token_never_raises() -> None:
    token = NeverCancelledToken()

    assert token.is_cancelled() is False
    token.raise_if_cancelled()


def test_mutable_token_raises_the_typed_error_after_cancellation() -> None:
    token = MutableCancellationToken()

    assert token.is_cancelled() is False

    # Requesting a stop does not itself raise; only an explicit worker-side
    # checkpoint turns the shared flag into the typed cancellation exception.
    token.cancel()

    assert token.is_cancelled() is True

    with pytest.raises(OperationCancelledError, match="operation was cancelled"):
        token.raise_if_cancelled()
