"""Provider base-URL normalization and outbound address policy.

This module is the provider-independent policy layer between credentials and a
future model transport. It deliberately does not open sockets. A transport
must resolve through :class:`ProviderEgressPolicy` before every connection,
pin a returned address, preserve the endpoint hostname for TLS SNI and
hostname verification, disable redirects, and disable trust-environment
settings.

No exception in this module includes a URL, hostname, resolved address, or
credential. Callers receive a stable failure code instead.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import urlsplit, urlunsplit

from packages.core.network_address import is_strict_global_address

MAX_PROVIDER_BASE_URL_LENGTH = 2048
PROVIDER_ALLOWED_SCHEMES = frozenset({"http", "https"})
PROVIDER_DEFAULT_PORTS = {"http": 80, "https": 443}
PROVIDER_EGRESS_V1_DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "openai-compatible": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com",
}

_ASCII_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_ASCII_PORT = re.compile(r"[0-9]+\Z")
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
Resolver = Callable[[str, int], Awaitable[Iterable[str]]]


class ProviderEgressFailureCode(StrEnum):
    ENDPOINT_INVALID = "PROVIDER_ENDPOINT_INVALID"
    CONFIG_INVALID = "PROVIDER_EGRESS_CONFIG_INVALID"
    DNS_FAILED = "PROVIDER_DNS_FAILED"
    TARGET_FORBIDDEN = "PROVIDER_TARGET_FORBIDDEN"
    PEER_FORBIDDEN = "PROVIDER_PEER_FORBIDDEN"


ENDPOINT_INVALID = ProviderEgressFailureCode.ENDPOINT_INVALID
CONFIG_INVALID = ProviderEgressFailureCode.CONFIG_INVALID
DNS_FAILED = ProviderEgressFailureCode.DNS_FAILED
TARGET_FORBIDDEN = ProviderEgressFailureCode.TARGET_FORBIDDEN
PEER_FORBIDDEN = ProviderEgressFailureCode.PEER_FORBIDDEN


class ProviderEgressError(Exception):
    """A provider egress refusal that is safe to expose across boundaries."""

    def __init__(self, failure_code: str | ProviderEgressFailureCode) -> None:
        self.failure_code = str(failure_code)
        super().__init__(self.failure_code)


@dataclass(frozen=True, slots=True)
class ProviderEndpoint:
    """A normalized provider endpoint that has not yet been authorized."""

    url: str
    scheme: str
    host: str
    port: int


@dataclass(frozen=True, slots=True)
class ProviderEgressAuthorization:
    """The exact DNS addresses authorized for one connection attempt."""

    endpoint: ProviderEndpoint
    addresses: tuple[str, ...]
    _private_cidrs: tuple[IPNetwork, ...] = field(repr=False, compare=False)

    def authorize_peer(self, peer_address: str | None) -> str:
        """Verify the socket peer against this authorization.

        The peer must be one exact address returned by the preceding DNS
        decision and must still satisfy the address policy. The canonical
        address is returned for transport logging or pin comparison.
        """

        if peer_address is None:
            raise ProviderEgressError(PEER_FORBIDDEN)
        try:
            canonical_peer = _canonicalize_address(peer_address)
        except (TypeError, ValueError):
            raise ProviderEgressError(PEER_FORBIDDEN) from None
        if canonical_peer not in self.addresses:
            raise ProviderEgressError(PEER_FORBIDDEN)
        if not _address_is_allowed(canonical_peer, self._private_cidrs):
            raise ProviderEgressError(PEER_FORBIDDEN)
        return canonical_peer


class ProviderEgressPolicy:
    """Authorize all DNS answers before a provider connection is attempted."""

    def __init__(
        self,
        private_cidrs: Iterable[str] = (),
        resolver: Resolver | None = None,
    ) -> None:
        self._private_cidrs = _normalize_private_cidrs(private_cidrs)
        self._resolver = resolver or _default_resolver

    @property
    def private_cidrs(self) -> tuple[str, ...]:
        return tuple(str(network) for network in self._private_cidrs)

    async def authorize(self, endpoint: ProviderEndpoint) -> ProviderEgressAuthorization:
        """Resolve once and authorize every answer, including mixed answers."""

        canonical_endpoint = _revalidate_endpoint(endpoint)
        try:
            resolved = await self._resolver(canonical_endpoint.host, canonical_endpoint.port)
        except Exception:
            raise ProviderEgressError(DNS_FAILED) from None

        addresses = _canonicalize_dns_answer(resolved)
        for address in addresses:
            if not _address_is_allowed(address, self._private_cidrs):
                raise ProviderEgressError(TARGET_FORBIDDEN)
        return ProviderEgressAuthorization(
            endpoint=canonical_endpoint,
            addresses=addresses,
            _private_cidrs=self._private_cidrs,
        )

    def authorize_peer(
        self,
        authorization: ProviderEgressAuthorization,
        peer_address: str | None,
    ) -> str:
        """Verify an actual socket peer against a prior authorization."""

        if not isinstance(authorization, ProviderEgressAuthorization):
            raise ProviderEgressError(PEER_FORBIDDEN)
        canonical_peer = authorization.authorize_peer(peer_address)
        if not _address_is_allowed(canonical_peer, self._private_cidrs):
            raise ProviderEgressError(PEER_FORBIDDEN)
        return canonical_peer


def normalize_provider_base_url(value: str | None) -> str | None:
    """Return a strict, normalized provider HTTP(S) base URL.

    ``None`` preserves the SDK's default-endpoint state. A configured URL must
    be absolute, credential-free, query-free, and fragment-free. Plain HTTP is
    accepted only for an explicitly loopback host; address authorization still
    has to allow that host through an operator CIDR.
    """

    if value is None:
        return None
    if not isinstance(value, str):
        raise ProviderEgressError(ENDPOINT_INVALID)
    if (
        not value
        or len(value) > MAX_PROVIDER_BASE_URL_LENGTH
        or value != value.strip()
        or any(character.isspace() for character in value)
        or any(_is_control(character) for character in value)
        or "\\" in value
        or "?" in value
        or "#" in value
    ):
        raise ProviderEgressError(ENDPOINT_INVALID)

    try:
        parts = urlsplit(value)
        scheme = parts.scheme.lower()
        if scheme not in PROVIDER_ALLOWED_SCHEMES or not parts.netloc or "@" in parts.netloc:
            raise ValueError
        raw_host, raw_port = _split_authority(parts.netloc)
        host = _normalize_host(raw_host)
        port = PROVIDER_DEFAULT_PORTS[scheme] if raw_port is None else _normalize_port(raw_port)
        path = _normalize_path(parts.path)
    except (TypeError, UnicodeError, ValueError):
        raise ProviderEgressError(ENDPOINT_INVALID) from None

    if scheme == "http" and not _is_loopback_host(host):
        raise ProviderEgressError(ENDPOINT_INVALID)

    url_host = f"[{host}]" if ":" in host else host
    authority = url_host
    if raw_port is not None and port != PROVIDER_DEFAULT_PORTS[scheme]:
        authority = f"{url_host}:{port}"
    normalized = urlunsplit((scheme, authority, path, "", ""))
    if not normalized or len(normalized) > MAX_PROVIDER_BASE_URL_LENGTH:
        raise ProviderEgressError(ENDPOINT_INVALID)
    return normalized


def parse_provider_endpoint(url: str) -> ProviderEndpoint:
    """Normalize and parse a provider endpoint without resolving it."""

    normalized = normalize_provider_base_url(url)
    if normalized is None:
        raise ProviderEgressError(ENDPOINT_INVALID)
    try:
        parts = urlsplit(normalized)
        raw_host, raw_port = _split_authority(parts.netloc)
        scheme = parts.scheme
        host = _normalize_host(raw_host)
        port = PROVIDER_DEFAULT_PORTS[scheme] if raw_port is None else _normalize_port(raw_port)
    except (KeyError, TypeError, UnicodeError, ValueError):
        raise ProviderEgressError(ENDPOINT_INVALID) from None
    return ProviderEndpoint(url=normalized, scheme=scheme, host=host, port=port)


def effective_provider_base_url(provider: str, base_url: str | None) -> str:
    """Resolve the immutable v1 default, never an SDK/environment override.

    Changing a default destination requires a new egress policy version.
    Explicit URLs retain their own route; provider support is also enforced
    by the adapter's existing model/provider allowlist.
    """
    configured = normalize_provider_base_url(base_url)
    if configured is not None:
        return configured
    try:
        return PROVIDER_EGRESS_V1_DEFAULT_BASE_URLS[provider.strip().lower().replace("_", "-")]
    except (AttributeError, KeyError):
        raise ProviderEgressError(ENDPOINT_INVALID) from None


async def _default_resolver(host: str, port: int) -> tuple[str, ...]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(
        host,
        port,
        family=socket.AF_UNSPEC,
        type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
    )
    return tuple(str(info[4][0]) for info in infos)


def _split_authority(authority: str) -> tuple[str, str | None]:
    if not authority:
        raise ValueError
    if authority.startswith("["):
        closing = authority.find("]")
        if closing < 0:
            raise ValueError
        host = authority[1:closing]
        remainder = authority[closing + 1 :]
        if not host or ":" not in host:
            raise ValueError
        if not remainder:
            return host, None
        if not remainder.startswith(":") or ":" in remainder[1:] or not remainder[1:]:
            raise ValueError
        return host, remainder[1:]
    if ":" in authority:
        host, separator, port = authority.rpartition(":")
        if not separator or not host or ":" in host or not port:
            raise ValueError
        return host, port
    return authority, None


def _normalize_host(raw_host: str) -> str:
    if not raw_host or raw_host.endswith("..") or raw_host == ".":
        raise ValueError
    candidate = raw_host[:-1] if raw_host.endswith(".") else raw_host
    if not candidate or "%" in candidate:
        raise ValueError
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        if ":" in candidate:
            raise
        ascii_host = candidate.encode("idna").decode("ascii").lower()
        if len(ascii_host) > 253:
            raise ValueError from None
        labels = ascii_host.split(".")
        if not labels or any(not _ASCII_LABEL.fullmatch(label) for label in labels):
            raise ValueError from None
        if all(character.isdigit() or character == "." for character in ascii_host):
            # Legacy inet_aton forms such as 127.1 and 2130706433 are not
            # DNS names, and libc may still interpret them as loopback.
            raise ValueError from None
        if ascii_host.startswith("0x") and all(
            character in _HEX_DIGITS or character == "." for character in ascii_host[2:]
        ):
            raise ValueError from None
        return ascii_host
    mapped = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) else None
    return str(mapped or address).lower()


def _normalize_port(raw_port: str) -> int:
    if not _ASCII_PORT.fullmatch(raw_port):
        raise ValueError
    port = int(raw_port)
    if not 1 <= port <= 65_535:
        raise ValueError
    return port


def _normalize_path(path: str) -> str:
    _validate_percent_escapes(path)
    # Keep repeated or embedded slashes because a provider may attach meaning
    # to them. A terminal slash is a conventional base-URL duplicate.
    return path.rstrip("/")


def _validate_percent_escapes(value: str) -> None:
    index = 0
    while index < len(value):
        if value[index] != "%":
            index += 1
            continue
        if index + 2 >= len(value):
            raise ValueError
        digits = value[index + 1 : index + 3]
        if any(character not in _HEX_DIGITS for character in digits):
            raise ValueError
        decoded = int(digits, 16)
        if decoded < 0x20 or decoded == 0x7F:
            raise ValueError
        index += 3


def _is_control(character: str) -> bool:
    codepoint = ord(character)
    return codepoint < 0x20 or 0x7F <= codepoint <= 0x9F


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _normalize_private_cidrs(values: Iterable[str]) -> tuple[IPNetwork, ...]:
    if isinstance(values, str | bytes):
        raise ProviderEgressError(CONFIG_INVALID)
    try:
        materialized = tuple(values)
    except TypeError:
        raise ProviderEgressError(CONFIG_INVALID) from None
    networks: list[IPNetwork] = []
    for value in materialized:
        if not isinstance(value, str) or "/" not in value or value != value.strip():
            raise ProviderEgressError(CONFIG_INVALID)
        try:
            networks.append(ipaddress.ip_network(value, strict=True))
        except ValueError:
            raise ProviderEgressError(CONFIG_INVALID) from None
    return tuple(networks)


def _revalidate_endpoint(endpoint: ProviderEndpoint) -> ProviderEndpoint:
    if not isinstance(endpoint, ProviderEndpoint):
        raise ProviderEgressError(ENDPOINT_INVALID)
    try:
        canonical = parse_provider_endpoint(endpoint.url)
    except ProviderEgressError:
        raise ProviderEgressError(ENDPOINT_INVALID) from None
    if canonical != endpoint:
        raise ProviderEgressError(ENDPOINT_INVALID)
    return canonical


def _canonicalize_dns_answer(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, str | bytes) or values is None:
        raise ProviderEgressError(DNS_FAILED)
    try:
        materialized = tuple(values)
    except TypeError:
        raise ProviderEgressError(DNS_FAILED) from None
    if not materialized:
        raise ProviderEgressError(DNS_FAILED)
    canonical: list[str] = []
    seen: set[str] = set()
    for value in materialized:
        if not isinstance(value, str):
            raise ProviderEgressError(DNS_FAILED)
        try:
            address = _canonicalize_address(value)
        except (TypeError, ValueError):
            raise ProviderEgressError(DNS_FAILED) from None
        if address not in seen:
            canonical.append(address)
            seen.add(address)
    return tuple(canonical)


def _canonicalize_address(value: str) -> str:
    if not isinstance(value, str) or not value or "%" in value:
        raise ValueError
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return str(address)


def _address_is_allowed(address: str, private_cidrs: tuple[IPNetwork, ...]) -> bool:
    candidate = ipaddress.ip_address(address)
    if any(
        candidate.version == network.version and candidate in network for network in private_cidrs
    ):
        return True
    return is_strict_global_address(candidate)


__all__ = [
    "CONFIG_INVALID",
    "DNS_FAILED",
    "ENDPOINT_INVALID",
    "MAX_PROVIDER_BASE_URL_LENGTH",
    "PEER_FORBIDDEN",
    "PROVIDER_ALLOWED_SCHEMES",
    "PROVIDER_DEFAULT_PORTS",
    "PROVIDER_EGRESS_V1_DEFAULT_BASE_URLS",
    "TARGET_FORBIDDEN",
    "ProviderEgressAuthorization",
    "ProviderEgressError",
    "ProviderEgressFailureCode",
    "ProviderEgressPolicy",
    "ProviderEndpoint",
    "Resolver",
    "normalize_provider_base_url",
    "effective_provider_base_url",
    "parse_provider_endpoint",
]
