"""Explicit outbound HTTP route construction shared by every provider path."""

import ipaddress
import re

import httpx

from metadata_polisher.infrastructure.session_credentials import CredentialSnapshot
from metadata_polisher.infrastructure.settings import NetworkSettings


def validate_network_settings(network: NetworkSettings) -> None:
    if network.mode == "direct":
        return

    if network.mode != "manual_proxy":
        raise ValueError("Choose Direct or Manual HTTP proxy in Settings.")

    host = network.proxy_host.strip()

    if not host or any(character in host for character in "/@?#\\") or any(char.isspace() for char in host):
        raise ValueError("Enter a proxy hostname or IP address without a URL, path or credentials.")

    try:
        ipaddress.ip_address(host)
    except ValueError:
        if re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host) is None:
            raise ValueError("Enter a valid proxy hostname or IP address.") from None

    if not 1 <= network.proxy_port <= 65535:
        raise ValueError("The proxy port must be between 1 and 65535.")


def create_http_client(
    network: NetworkSettings,
    credentials: CredentialSnapshot | None = None,
) -> httpx.Client:
    """HTTP proxies carry HTTPS via CONNECT; TLS verification remains enabled."""
    validate_network_settings(network)
    proxy = None

    if network.mode == "manual_proxy":
        # Keep credentials out of URLs, settings and ordinary request headers.
        # HTTPX sends proxy authentication at its external proxy boundary.
        url = httpx.URL(scheme="http", host=network.proxy_host.strip(), port=network.proxy_port)
        proxy = httpx.Proxy(url, auth=credentials.proxy_auth if credentials is not None else None)

    # Both routes ignore ambient proxy and NO_PROXY variables. A configured
    # proxy therefore cannot be silently bypassed by the process environment.
    return httpx.Client(proxy=proxy, trust_env=False, verify=True, follow_redirects=True)


def describe_network_route(network: NetworkSettings) -> str:
    """Describe a validated non-secret route without echoing malformed input."""
    try:
        validate_network_settings(network)
    except ValueError:
        return "Effective route: invalid — correct the proxy host, port or mode."

    if network.mode == "direct":
        return "Effective route: Direct; environment proxy settings are ignored."

    address = httpx.URL(scheme="http", host=network.proxy_host.strip(), port=network.proxy_port)
    return f"Effective route: {address}; HTTPS uses a verified tunnel through this HTTP proxy."
