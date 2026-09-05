"""Opaque RAM-only proxy credentials, excluded from serialisable settings."""


class CredentialSnapshot:
    """Immutable operation credentials with a deliberately opaque representation."""

    __slots__ = ("_generation", "_proxy_auth")
    _generation: int
    _proxy_auth: tuple[str, str] | None

    def __init__(self, generation: int, proxy_auth: tuple[str, str] | None) -> None:
        object.__setattr__(self, "_generation", generation)
        object.__setattr__(self, "_proxy_auth", proxy_auth)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Credential snapshots are immutable")

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def proxy_auth(self) -> tuple[str, str] | None:
        return self._proxy_auth

    def __repr__(self) -> str:
        return f"CredentialSnapshot(generation={self.generation}, credentials=[REDACTED])"


class SessionCredentials:
    """One process's credentials; construction and forgetting never access disk."""

    __slots__ = ("_generation", "_proxy_auth")

    def __init__(self) -> None:
        self._generation = 0
        self._proxy_auth: tuple[str, str] | None = None

    def set_proxy(self, username: str, password: str) -> None:
        if not isinstance(username, str) or not isinstance(password, str):
            raise TypeError("Proxy credentials must be strings")

        self._proxy_auth = (username, password) if username or password else None
        # A new generation lets captured operations distinguish an account
        # change even when no credential value is exposed in diagnostics.
        self._generation += 1

    def forget(self) -> None:
        self._proxy_auth = None
        self._generation += 1

    # Each submitted operation receives an immutable snapshot. Forgetting the
    # store requires client retirement elsewhere to stop reuse of old snapshots.
    def snapshot(self) -> CredentialSnapshot:
        return CredentialSnapshot(self._generation, self._proxy_auth)

    def __repr__(self) -> str:
        return "SessionCredentials(credentials=[REDACTED])"
