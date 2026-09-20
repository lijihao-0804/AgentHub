"""Secret protection and outbound target policy for remote MCP connections.

Two concerns live here because they guard the same boundary: what AgentHub
stores on behalf of a remote server (its bearer token) and what AgentHub is
willing to connect to on a user's instruction (its address).

Nothing in this module raises an exception carrying a URL, a host, or a
secret. Failures are reported as stable codes so they can be surfaced to a
caller, logged, and asserted in tests without any of them leaking.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken

from packages.core.config.settings import DEVELOPMENT_ENVIRONMENTS, Settings, get_settings

MCP_SECRET_CIPHERTEXT_VERSION = 1

MAX_ENDPOINT_URL_LENGTH = 2048
ALLOWED_SCHEMES = frozenset({"http", "https"})
DEFAULT_PORTS = {"http": 80, "https": 443}

ENDPOINT_INVALID = "MCP_ENDPOINT_INVALID"
TARGET_FORBIDDEN = "MCP_TARGET_FORBIDDEN"
DNS_FAILED = "MCP_DNS_FAILED"

Resolver = Callable[[str, int], Awaitable[tuple[str, ...]]]


class McpSecretError(Exception):
    """Safe local error; never includes the plaintext token or its ciphertext."""


class McpTargetError(Exception):
    """An outbound target was refused. Carries only a stable failure code."""

    def __init__(self, failure_code: str) -> None:
        super().__init__(failure_code)
        self.failure_code = failure_code


@dataclass(frozen=True)
class McpEndpoint:
    """A syntactically valid MCP endpoint, not yet checked against DNS."""

    url: str
    scheme: str
    host: str
    port: int


class McpSecretCipher:
    """Fernet encryption for MCP bearer tokens.

    This is deliberately not the provider-credential cipher. The two share a
    master key, which is the part that genuinely must be shared, but they own
    independent ciphertext versions so that a future change to one secret
    format cannot silently reinterpret the other's rows. The provider
    credential path is frozen; reaching into it to save thirty lines here
    would put that guarantee at risk for no functional gain.
    """

    def __init__(self, key: bytes) -> None:
        self._fernet = Fernet(key)

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> McpSecretCipher:
        settings = settings or get_settings()
        configured = settings.credential_master_key
        if not configured:
            if settings.environment.lower() not in DEVELOPMENT_ENVIRONMENTS:
                raise McpSecretError("MCP secret encryption is not configured.")
            configured = "agenthub-local-credential-master-key"
        try:
            decoded = base64.urlsafe_b64decode(configured.encode("ascii"))
        except (UnicodeError, ValueError):
            decoded = b""
        key = configured.encode("ascii") if len(decoded) == 32 else _derived_key(configured)
        try:
            return cls(key)
        except (TypeError, ValueError) as exc:
            raise McpSecretError("MCP secret encryption is not configured.") from exc

    def encrypt(self, secret: str) -> str:
        if not isinstance(secret, str) or not secret:
            raise McpSecretError("MCP secret is invalid.")
        try:
            token = self._fernet.encrypt(secret.encode("utf-8")).decode("ascii")
        except (UnicodeError, ValueError) as exc:
            raise McpSecretError("MCP secret encryption failed.") from exc
        return f"v{MCP_SECRET_CIPHERTEXT_VERSION}:{token}"

    def decrypt(self, ciphertext: str) -> str:
        prefix = f"v{MCP_SECRET_CIPHERTEXT_VERSION}:"
        if not isinstance(ciphertext, str) or not ciphertext.startswith(prefix):
            raise McpSecretError("MCP secret ciphertext is invalid.")
        try:
            return self._fernet.decrypt(ciphertext[len(prefix) :].encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError) as exc:
            raise McpSecretError("MCP secret ciphertext could not be decrypted.") from exc


def _derived_key(value: str) -> bytes:
    return base64.urlsafe_b64encode(hashlib.sha256(value.encode("utf-8")).digest())


def parse_endpoint_url(raw: str) -> McpEndpoint:
    """Validate endpoint shape without touching the network.

    Runs at create time so a malformed or unsupported endpoint is rejected
    before a row exists, and again inside the transport guard so that any URL
    the SDK derives from it is held to the same rule.
    """

    if not isinstance(raw, str):
        raise McpTargetError(ENDPOINT_INVALID)
    candidate = raw.strip()
    if not candidate or len(candidate) > MAX_ENDPOINT_URL_LENGTH:
        raise McpTargetError(ENDPOINT_INVALID)
    try:
        parts = urlsplit(candidate)
    except ValueError as exc:
        raise McpTargetError(ENDPOINT_INVALID) from exc
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise McpTargetError(ENDPOINT_INVALID)
    # Credentials in the URL would be a second, unmanaged way to authenticate,
    # and one that ends up in logs and redirects. Connections carry their token
    # in an encrypted column or not at all.
    if parts.username or parts.password:
        raise McpTargetError(ENDPOINT_INVALID)
    try:
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise McpTargetError(ENDPOINT_INVALID) from exc
    if not host:
        raise McpTargetError(ENDPOINT_INVALID)
    scheme = parts.scheme.lower()
    resolved_port = port if port is not None else DEFAULT_PORTS[scheme]
    if resolved_port <= 0 or resolved_port > 65_535:
        raise McpTargetError(ENDPOINT_INVALID)
    return McpEndpoint(url=candidate, scheme=scheme, host=host, port=resolved_port)


def _normalize_address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    # getaddrinfo returns scoped literals such as "fe80::1%eth0" for link-local
    # results; the zone index is not part of the address.
    candidate = value.split("%", 1)[0]
    address = ipaddress.ip_address(candidate)
    if isinstance(address, ipaddress.IPv6Address):
        # An attacker who cannot name 169.254.169.254 directly can often still
        # name ::ffff:169.254.169.254 or its 6to4 form. Judge the address that
        # packets will actually reach.
        mapped = address.ipv4_mapped
        if mapped is not None:
            return mapped
        sixtofour = address.sixtofour
        if sixtofour is not None:
            return sixtofour
    return address


def is_forbidden_address(value: str) -> bool:
    """True when an address must never be reached on a user's instruction."""

    try:
        address = _normalize_address(value)
    except ValueError:
        # An address we cannot parse is an address we cannot vouch for.
        return True
    return bool(
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


async def _default_resolver(host: str, port: int) -> tuple[str, ...]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return tuple(str(info[4][0]) for info in infos)


async def authorize_endpoint(
    endpoint: McpEndpoint,
    *,
    allow_private_targets: bool,
    resolver: Resolver | None = None,
) -> tuple[str, ...]:
    """Resolve an endpoint and decide whether AgentHub may connect to it.

    Every address the name resolves to has to pass. A host that answers with
    one public and one private record is rejected outright: accepting it would
    mean the decision was made by whichever record the connection happened to
    pick, which is exactly the property a rebinding attack relies on.
    """

    resolve = resolver or _default_resolver
    try:
        addresses = await resolve(endpoint.host, endpoint.port)
    except (OSError, socket.gaierror, UnicodeError) as exc:
        raise McpTargetError(DNS_FAILED) from exc
    if not addresses:
        raise McpTargetError(DNS_FAILED)
    if allow_private_targets:
        # The operator has taken responsibility for where this points. The
        # scheme restriction above still stands: opting into a local MCP demo
        # server is not opting into arbitrary URI schemes.
        return tuple(addresses)
    for address in addresses:
        if is_forbidden_address(address):
            raise McpTargetError(TARGET_FORBIDDEN)
    return tuple(addresses)


def authorize_peer_address(value: str | None, *, allow_private_targets: bool) -> None:
    """Re-check the address a connection actually landed on.

    The pre-flight check inspects DNS answers; this one inspects the socket.
    Between the two there is a window in which a name can start resolving
    somewhere else, so the second look is what closes it.
    """

    if allow_private_targets or value is None:
        return
    if is_forbidden_address(value):
        raise McpTargetError(TARGET_FORBIDDEN)


__all__ = [
    "ALLOWED_SCHEMES",
    "DNS_FAILED",
    "ENDPOINT_INVALID",
    "MCP_SECRET_CIPHERTEXT_VERSION",
    "MAX_ENDPOINT_URL_LENGTH",
    "TARGET_FORBIDDEN",
    "McpEndpoint",
    "McpSecretCipher",
    "McpSecretError",
    "McpTargetError",
    "authorize_endpoint",
    "authorize_peer_address",
    "is_forbidden_address",
    "parse_endpoint_url",
]
