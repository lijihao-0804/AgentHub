"""Vectors for strict global address classification across outbound boundaries."""

from __future__ import annotations

import ipaddress

import pytest

from packages.core.network_address import embedded_ipv4_addresses, is_strict_global_address
from packages.mcp.security import (
    TARGET_FORBIDDEN,
    McpTargetError,
    authorize_endpoint,
    is_forbidden_address,
    parse_endpoint_url,
)
from packages.model_gateway.egress import TARGET_FORBIDDEN as PROVIDER_TARGET_FORBIDDEN
from packages.model_gateway.egress import (
    ProviderEgressError,
    ProviderEgressPolicy,
    parse_provider_endpoint,
)


def resolver_for(*addresses: str):
    async def resolve(_host: str, _port: int) -> tuple[str, ...]:
        return addresses

    return resolve


@pytest.mark.parametrize(
    ("literal", "expected"),
    [
        ("2002:6440:1::", ("100.64.0.1",)),
        ("64:ff9b::a9fe:a9fe", ("169.254.169.254",)),
        ("::ffff:10.0.0.1", ("10.0.0.1",)),
        (
            "2001:0:4136:e378:8000:63bf:9bbf:fffe",
            ("65.54.227.120", "100.64.0.1"),
        ),
    ],
)
def test_standard_transition_fields_expose_embedded_ipv4(
    literal: str,
    expected: tuple[str, ...],
) -> None:
    embedded = embedded_ipv4_addresses(ipaddress.ip_address(literal))

    assert tuple(str(address) for address in embedded) == expected


@pytest.mark.parametrize(
    "literal",
    [
        "64:ff9b::a00:1",
        "64:ff9b::a9fe:a9fe",
        "64:ff9b::6440:1",
        "2002:6440:1::",
        "2001:0:4136:e378:8000:63bf:9bbf:fffe",
        "224.0.0.1",
        "ff02::1",
        "64:ff9b::808:808",
        "fec0::1",
    ],
)
def test_strict_global_rejects_non_public_or_embedded_non_global_forms(literal: str) -> None:
    assert is_strict_global_address(ipaddress.ip_address(literal)) is False


@pytest.mark.parametrize(
    "literal",
    ["8.8.8.8", "2606:4700:4700::1111", "::ffff:808:808"],
)
def test_strict_global_preserves_global_standard_forms(literal: str) -> None:
    assert is_strict_global_address(ipaddress.ip_address(literal)) is True


def test_strict_global_rejects_mapped_multicast() -> None:
    assert is_strict_global_address(ipaddress.ip_address("::ffff:224.0.0.1")) is False


@pytest.mark.asyncio
async def test_provider_rejects_nat64_cgnat_unless_operator_cidr_authorizes_it() -> None:
    endpoint = parse_provider_endpoint("https://provider.example/v1")
    answer = "64:ff9b::6440:1"

    with pytest.raises(ProviderEgressError) as excinfo:
        await ProviderEgressPolicy(resolver=resolver_for(answer)).authorize(endpoint)
    assert excinfo.value.failure_code == PROVIDER_TARGET_FORBIDDEN

    authorization = await ProviderEgressPolicy(
        private_cidrs=("64:ff9b::/96",),
        resolver=resolver_for(answer),
    ).authorize(endpoint)
    assert authorization.addresses == (answer,)


@pytest.mark.asyncio
async def test_mcp_default_rejects_cgnat_while_the_existing_opt_in_is_preserved() -> None:
    endpoint = parse_endpoint_url("https://mcp.example/mcp")
    answer = "100.64.0.1"

    assert is_forbidden_address(answer) is True
    with pytest.raises(McpTargetError) as excinfo:
        await authorize_endpoint(
            endpoint,
            allow_private_targets=False,
            resolver=resolver_for(answer),
        )
    assert excinfo.value.failure_code == TARGET_FORBIDDEN
    assert answer not in repr(excinfo.value)

    assert (
        await authorize_endpoint(
            endpoint,
            allow_private_targets=True,
            resolver=resolver_for(answer),
        )
        == (answer,)
    )
