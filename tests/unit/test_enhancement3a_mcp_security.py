"""Enhancement 3A: outbound target policy and bearer-token protection.

The endpoint of an MCP connection is attacker-influenced input: whoever can
create a connection chooses where AgentHub's own process will send an
authenticated request from inside the network. These tests pin the rules that
make that safe, and none of them may reach the public internet — resolution is
always injected.
"""

from __future__ import annotations

import pytest

from packages.core.config.settings import Settings
from packages.mcp.security import (
    DNS_FAILED,
    ENDPOINT_INVALID,
    TARGET_FORBIDDEN,
    McpSecretCipher,
    McpSecretError,
    McpTargetError,
    authorize_endpoint,
    authorize_peer_address,
    is_forbidden_address,
    parse_endpoint_url,
)

PUBLIC = "93.184.216.34"


def resolver_for(*addresses: str):
    async def resolve(_host: str, _port: int) -> tuple[str, ...]:
        return addresses

    return resolve


async def failing_resolver(_host: str, _port: int) -> tuple[str, ...]:
    raise OSError("getaddrinfo failed for internal-host.corp")


# --- scheme and shape -------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://mcp.example.com/mcp",
        "https://mcp.example.com/mcp",
        "HTTPS://MCP.EXAMPLE.COM:8443/mcp",
    ],
)
def test_http_and_https_endpoints_are_accepted(url: str) -> None:
    assert parse_endpoint_url(url).scheme in {"http", "https"}


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://mcp.example.com/mcp",
        "gopher://mcp.example.com/mcp",
        "unix:///var/run/mcp.sock",
        "data:text/plain;base64,aGk=",
        "javascript:alert(1)",
        "ws://mcp.example.com/mcp",
        "mcp.example.com/mcp",
        "",
        "   ",
    ],
)
def test_every_other_scheme_is_rejected(url: str) -> None:
    with pytest.raises(McpTargetError) as excinfo:
        parse_endpoint_url(url)
    assert excinfo.value.failure_code == ENDPOINT_INVALID


def test_endpoint_may_not_smuggle_credentials_in_the_url() -> None:
    with pytest.raises(McpTargetError):
        parse_endpoint_url("https://user:token@mcp.example.com/mcp")


def test_endpoint_length_is_bounded() -> None:
    with pytest.raises(McpTargetError):
        parse_endpoint_url("https://mcp.example.com/" + "a" * 4000)


# --- address policy ---------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "::1",
        "10.1.2.3",
        "172.16.0.1",
        "172.31.255.254",
        "192.168.1.1",
        "169.254.0.1",
        "169.254.169.254",
        "0.0.0.0",
        "224.0.0.1",
        "240.0.0.1",
        "fe80::1",
        "fc00::1",
        # The metadata service wearing an IPv6 costume.
        "::ffff:169.254.169.254",
        "::ffff:127.0.0.1",
        "not-an-address",
    ],
)
def test_forbidden_addresses_are_recognised(address: str) -> None:
    assert is_forbidden_address(address) is True


@pytest.mark.parametrize("address", [PUBLIC, "8.8.8.8", "2606:4700:4700::1111"])
def test_public_addresses_are_allowed(address: str) -> None:
    assert is_forbidden_address(address) is False


@pytest.mark.asyncio
async def test_a_public_endpoint_resolves_and_is_authorized() -> None:
    endpoint = parse_endpoint_url("https://mcp.example.com/mcp")

    addresses = await authorize_endpoint(
        endpoint, allow_private_targets=False, resolver=resolver_for(PUBLIC)
    )

    assert addresses == (PUBLIC,)


@pytest.mark.asyncio
async def test_a_hostname_resolving_to_the_metadata_service_is_refused() -> None:
    endpoint = parse_endpoint_url("https://totally-normal.example.com/mcp")

    with pytest.raises(McpTargetError) as excinfo:
        await authorize_endpoint(
            endpoint, allow_private_targets=False, resolver=resolver_for("169.254.169.254")
        )

    assert excinfo.value.failure_code == TARGET_FORBIDDEN


@pytest.mark.asyncio
async def test_one_forbidden_record_poisons_a_mixed_answer() -> None:
    """A public record next to a private one must not buy passage.

    Allowing this would leave the decision to whichever record the socket
    happened to pick — the exact property a rebinding attack is built on.
    """

    endpoint = parse_endpoint_url("https://dual.example.com/mcp")

    with pytest.raises(McpTargetError) as excinfo:
        await authorize_endpoint(
            endpoint, allow_private_targets=False, resolver=resolver_for(PUBLIC, "10.0.0.5")
        )

    assert excinfo.value.failure_code == TARGET_FORBIDDEN


@pytest.mark.asyncio
async def test_resolution_failure_is_reported_without_naming_the_host() -> None:
    endpoint = parse_endpoint_url("https://internal-host.corp/mcp")

    with pytest.raises(McpTargetError) as excinfo:
        await authorize_endpoint(
            endpoint, allow_private_targets=False, resolver=failing_resolver
        )

    assert excinfo.value.failure_code == DNS_FAILED
    assert str(excinfo.value) == DNS_FAILED
    assert "internal-host.corp" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_an_empty_dns_answer_is_a_resolution_failure() -> None:
    endpoint = parse_endpoint_url("https://void.example.com/mcp")

    with pytest.raises(McpTargetError) as excinfo:
        await authorize_endpoint(endpoint, allow_private_targets=False, resolver=resolver_for())

    assert excinfo.value.failure_code == DNS_FAILED


# --- the explicit opt-in ----------------------------------------------------


@pytest.mark.asyncio
async def test_the_opt_in_allows_a_private_target() -> None:
    endpoint = parse_endpoint_url("http://localhost:8931/mcp")

    addresses = await authorize_endpoint(
        endpoint, allow_private_targets=True, resolver=resolver_for("127.0.0.1")
    )

    assert addresses == ("127.0.0.1",)


def test_the_opt_in_does_not_widen_the_scheme_allowlist() -> None:
    """Opting into a local MCP server is not opting into arbitrary URIs."""

    with pytest.raises(McpTargetError):
        parse_endpoint_url("file:///etc/passwd")


def test_the_opt_in_defaults_to_off_and_is_not_inferred_from_the_environment() -> None:
    assert Settings(testing=True, environment="local").mcp_allow_private_targets is False
    production = Settings(testing=True, environment="production", auth_jwt_secret="x" * 32)
    assert production.mcp_allow_private_targets is False


def test_peer_address_is_rechecked_after_the_connection_is_made() -> None:
    authorize_peer_address(PUBLIC, allow_private_targets=False)
    authorize_peer_address(None, allow_private_targets=False)
    authorize_peer_address("10.0.0.5", allow_private_targets=True)

    with pytest.raises(McpTargetError) as excinfo:
        authorize_peer_address("169.254.169.254", allow_private_targets=False)

    assert excinfo.value.failure_code == TARGET_FORBIDDEN


# --- secret protection ------------------------------------------------------


def cipher() -> McpSecretCipher:
    return McpSecretCipher.from_settings(
        Settings(testing=True, environment="test", credential_master_key="unit-test-master-key")
    )


def test_a_token_round_trips_through_versioned_ciphertext() -> None:
    engine = cipher()

    ciphertext = engine.encrypt("tok-super-secret")

    assert ciphertext.startswith("v1:")
    assert "tok-super-secret" not in ciphertext
    assert engine.decrypt(ciphertext) == "tok-super-secret"


def test_ciphertext_from_another_master_key_cannot_be_read() -> None:
    other = McpSecretCipher.from_settings(
        Settings(testing=True, environment="test", credential_master_key="a-different-key")
    )

    with pytest.raises(McpSecretError):
        cipher().decrypt(other.encrypt("tok-super-secret"))


@pytest.mark.parametrize("bad", ["", "not-versioned", "v1:garbage", "v9:abc"])
def test_malformed_ciphertext_is_refused_without_echoing_it(bad: str) -> None:
    with pytest.raises(McpSecretError) as excinfo:
        cipher().decrypt(bad)

    assert bad not in str(excinfo.value) or bad == ""


def test_encryption_is_unavailable_outside_development_without_a_master_key() -> None:
    with pytest.raises(McpSecretError):
        McpSecretCipher.from_settings(
            Settings(
                testing=True,
                environment="production",
                auth_jwt_secret="x" * 32,
                credential_master_key=None,
            )
        )
