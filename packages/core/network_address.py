"""Pure IP-address classification shared by outbound target policies.

The standard library's ``is_global`` is the baseline. Some standard IPv6
transition forms also carry an IPv4 destination, so the transition address is
not globally safe when that embedded IPv4 address is non-global.
"""

from __future__ import annotations

import ipaddress

Address = ipaddress.IPv4Address | ipaddress.IPv6Address

_NAT64_WELL_KNOWN_PREFIX = ipaddress.IPv6Network("64:ff9b::/96")


def embedded_ipv4_addresses(address: Address) -> tuple[ipaddress.IPv4Address, ...]:
    """Return the IPv4 destinations represented by standard transition forms."""

    if isinstance(address, ipaddress.IPv4Address):
        return ()

    embedded: list[ipaddress.IPv4Address] = []
    if address.ipv4_mapped is not None:
        embedded.append(address.ipv4_mapped)
    if address.sixtofour is not None:
        embedded.append(address.sixtofour)
    if address.teredo is not None:
        server, client = address.teredo
        embedded.extend((server, client))
    if address in _NAT64_WELL_KNOWN_PREFIX:
        embedded.append(ipaddress.IPv4Address(int(address) & 0xFFFF_FFFF))
    return tuple(embedded)


def is_strict_global_address(address: Address) -> bool:
    """Return whether the address and every standard IPv4 destination are global."""

    if (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or getattr(address, "is_site_local", False)
        or not address.is_global
    ):
        return False
    return all(
        is_strict_global_address(embedded) for embedded in embedded_ipv4_addresses(address)
    )


__all__ = ["Address", "embedded_ipv4_addresses", "is_strict_global_address"]
