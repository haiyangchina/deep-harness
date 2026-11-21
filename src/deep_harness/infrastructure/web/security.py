"""URL boundaries and prompt-injection containment for external content."""

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

from deep_harness.domain.models import DomainError
from deep_harness.server_config import number

MAX_URL_LENGTH = number("web_fetch.url_max_length")
_PRIVATE_HOSTS = {"localhost", "localhost.localdomain", "metadata.google.internal"}
_PRIVATE_SUFFIXES = (".local", ".internal", ".localhost", ".home.arpa")


def validate_public_url(raw: str, *, allow_private: bool = False) -> str:
    """Reject non-HTTP schemes, embedded credentials and private network targets.

    Configured, operator-owned endpoints (for example a self-hosted SearXNG) never
    go through this check; it only guards model-supplied URLs in web_fetch.
    """
    if not isinstance(raw, str) or not raw.strip() or len(raw) > MAX_URL_LENGTH:
        raise DomainError(
            "web_url", "URL must be a non-empty http(s) address within the configured limit"
        )
    value = raw.strip()
    try:
        parts = urlsplit(value)
        _ = parts.port
    except ValueError as exc:
        raise DomainError("web_url", "Malformed URL") from exc
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise DomainError("web_url", "Only http and https URLs are supported")
    if parts.username or parts.password:
        raise DomainError("web_url", "URLs with embedded credentials are refused")
    host = parts.hostname.lower().rstrip(".")
    if allow_private:
        return value
    if host in _PRIVATE_HOSTS or host.endswith(_PRIVATE_SUFFIXES):
        raise DomainError("web_private_url", "Private or local network URLs are refused")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        require_global_address(address)
    return value


def require_global_address(address):
    if not address.is_global or address.is_multicast or address.is_reserved:
        raise DomainError("web_private_url", "Private or local network URLs are refused")


async def resolve_fetch_addresses(host, port, *, allow_private):
    rows = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = tuple(dict.fromkeys(row[4][0] for row in rows))
    if not addresses:
        raise DomainError("web_backend", "No address available for page fetch")
    if not allow_private:
        for address in addresses:
            require_global_address(ipaddress.ip_address(address))
    return addresses


def untrusted_block(text: str) -> str:
    """Wrap page text so a model cannot mistake it for instructions."""
    safe = text.replace("</untrusted-web-content", "</ untrusted-web-content")
    return f"<untrusted-web-content>\n{safe}\n</untrusted-web-content>"


def normalize_url(url: str) -> str:
    """Loose canonical form used only for de-duplicating search hits."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().rstrip(".")
    path = parts.path.rstrip("/") or "/"
    return f"{host}{path}?{parts.query}" if parts.query else f"{host}{path}"
