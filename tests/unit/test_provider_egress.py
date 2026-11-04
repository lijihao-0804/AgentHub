"""Boundary tests for provider base URLs and outbound address authorization."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from packages.core.config.settings import Settings
from packages.model_gateway.egress import (
    CONFIG_INVALID,
    DNS_FAILED,
    ENDPOINT_INVALID,
    PEER_FORBIDDEN,
    TARGET_FORBIDDEN,
    ProviderEgressError,
    ProviderEgressPolicy,
    ProviderEndpoint,
    normalize_provider_base_url,
    parse_provider_endpoint,
)
from tests.support.settings import declared_settings

PUBLIC = "93.184.216.34"
PUBLIC_V6 = "2606:4700:4700::1111"


def resolver_for(*addresses: str):
    calls: list[tuple[str, int]] = []

    async def resolve(host: str, port: int) -> tuple[str, ...]:
        calls.append((host, port))
        return addresses

    resolve.calls = calls  # type: ignore[attr-defined]
    return resolve


def resolver_returning(value: object):
    async def resolve(_host: str, _port: int) -> object:
        return value

    return resolve


async def resolver_that_fails(_host: str, _port: int) -> tuple[str, ...]:
    raise OSError("lookup failed for provider-secret.internal with token super-secret")


def test_none_preserves_the_sdk_default_endpoint_state() -> None:
    assert normalize_provider_base_url(None) is None


def test_a_base_url_is_canonicalized_without_changing_path_semantics() -> None:
    assert (
        normalize_provider_base_url("HTTPS://BÜCHER.Example.:443/v1//models/")
        == "https://xn--bcher-kva.example/v1//models"
    )


def test_endpoint_exposes_the_effective_default_port() -> None:
    endpoint = parse_provider_endpoint("HTTPS://Provider.Example/v1/")

    assert endpoint == ProviderEndpoint(
        url="https://provider.example/v1",
        scheme="https",
        host="provider.example",
        port=443,
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:3425/v1",
        "http://localhost:3425/v1",
        "http://[::1]:3425/v1",
        "http://[::ffff:127.0.0.1]:3425/v1",
    ],
)
def test_plain_http_is_admitted_for_loopback_hosts_only(url: str) -> None:
    assert normalize_provider_base_url(url) is not None


@pytest.mark.parametrize(
    "url",
    [
        "http://provider.example/v1",
        "http://127.1/v1",
        "http://0x7f000001/v1",
        "http://10.0.0.1/v1",
    ],
)
def test_plain_http_does_not_admit_non_loopback_or_ambiguous_hosts(url: str) -> None:
    with pytest.raises(ProviderEgressError) as excinfo:
        normalize_provider_base_url(url)
    assert excinfo.value.failure_code == ENDPOINT_INVALID


@pytest.mark.parametrize(
    "url",
    [
        "",
        "provider.example/v1",
        "ftp://provider.example/v1",
        "https://user:token@provider.example/v1",
        "https://provider.example/v1?api_key=secret",
        "https://provider.example/v1#fragment",
        "https://provider.example/v1 with-space",
        "https://provider.example/v1\n",
        "https://provider.example\\@attacker.example/v1",
        "https://provider.example:0/v1",
        "https://provider.example:65536/v1",
        "https://provider.example:not-a-port/v1",
        "https://provider.example:/v1",
        "https://provider.example/%zz",
        "https://provider.example/%0d%0aInjected",
        "https://bad_host.example/v1",
        "https://127.1/v1",
        "https://2130706433/v1",
    ],
)
def test_ambiguous_or_polluted_urls_are_refused(url: str) -> None:
    with pytest.raises(ProviderEgressError) as excinfo:
        normalize_provider_base_url(url)

    assert excinfo.value.failure_code == ENDPOINT_INVALID
    assert str(excinfo.value) == ENDPOINT_INVALID


def test_url_length_is_bounded() -> None:
    with pytest.raises(ProviderEgressError) as excinfo:
        normalize_provider_base_url("https://provider.example/" + "a" * 2048)
    assert excinfo.value.failure_code == ENDPOINT_INVALID


@pytest.mark.asyncio
async def test_public_dns_answers_are_authorized_and_resolved_every_time() -> None:
    resolver = resolver_for(PUBLIC, PUBLIC_V6, PUBLIC)
    policy = ProviderEgressPolicy(resolver=resolver)
    endpoint = parse_provider_endpoint("https://provider.example/v1")

    first = await policy.authorize(endpoint)
    second = await policy.authorize(endpoint)

    assert first.endpoint == endpoint
    assert first.addresses == (PUBLIC, PUBLIC_V6)
    assert second.addresses == (PUBLIC, PUBLIC_V6)
    assert resolver.calls == [("provider.example", 443), ("provider.example", 443)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "127.0.0.1",
        "169.254.169.254",
        "100.64.0.1",
        "192.0.2.1",
        "fe80::1",
        "::1",
        "fc00::1",
    ],
)
async def test_non_global_addresses_are_refused_by_default(address: str) -> None:
    policy = ProviderEgressPolicy(resolver=resolver_for(address))

    with pytest.raises(ProviderEgressError) as excinfo:
        await policy.authorize(parse_provider_endpoint("https://provider.example/v1"))

    assert excinfo.value.failure_code == TARGET_FORBIDDEN
    assert address not in str(excinfo.value)


@pytest.mark.asyncio
async def test_one_forbidden_address_rejects_a_mixed_dns_answer() -> None:
    policy = ProviderEgressPolicy(resolver=resolver_for(PUBLIC, "10.0.0.5"))

    with pytest.raises(ProviderEgressError) as excinfo:
        await policy.authorize(parse_provider_endpoint("https://mixed.example/v1"))

    assert excinfo.value.failure_code == TARGET_FORBIDDEN


@pytest.mark.asyncio
async def test_dns_failure_is_safe_and_does_not_expose_the_resolver_error() -> None:
    policy = ProviderEgressPolicy(resolver=resolver_that_fails)

    with pytest.raises(ProviderEgressError) as excinfo:
        await policy.authorize(parse_provider_endpoint("https://provider-secret.internal/v1"))

    error = excinfo.value
    assert error.failure_code == DNS_FAILED
    assert str(error) == DNS_FAILED
    assert "provider-secret.internal" not in repr(error)
    assert "super-secret" not in repr(error)


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [(), "93.184.216.34", None, (None,), ("not-an-address",)])
async def test_empty_or_invalid_dns_answers_are_resolution_failures(answer: object) -> None:
    policy = ProviderEgressPolicy(resolver=resolver_returning(answer))

    with pytest.raises(ProviderEgressError) as excinfo:
        await policy.authorize(parse_provider_endpoint("https://provider.example/v1"))

    assert excinfo.value.failure_code == DNS_FAILED


@pytest.mark.asyncio
async def test_ipv4_mapped_ipv6_is_judged_as_its_actual_ipv4_address() -> None:
    private_policy = ProviderEgressPolicy(resolver=resolver_for("::ffff:10.1.2.3"))
    public_policy = ProviderEgressPolicy(resolver=resolver_for("::ffff:8.8.8.8"))
    endpoint = parse_provider_endpoint("https://provider.example/v1")

    with pytest.raises(ProviderEgressError) as excinfo:
        await private_policy.authorize(endpoint)
    assert excinfo.value.failure_code == TARGET_FORBIDDEN

    authorization = await public_policy.authorize(endpoint)
    assert authorization.addresses == ("8.8.8.8",)


@pytest.mark.asyncio
async def test_explicit_loopback_cidr_allows_magpie_and_rejects_its_neighbor() -> None:
    endpoint = parse_provider_endpoint("http://127.0.0.1:3425/v1")
    allowed = ProviderEgressPolicy(
        private_cidrs=("127.0.0.0/8",),
        resolver=resolver_for("127.0.0.1"),
    )
    neighbor = ProviderEgressPolicy(
        private_cidrs=("127.0.0.0/32",),
        resolver=resolver_for("127.0.0.2"),
    )

    authorization = await allowed.authorize(endpoint)
    assert authorization.addresses == ("127.0.0.1",)

    with pytest.raises(ProviderEgressError) as excinfo:
        await neighbor.authorize(endpoint)
    assert excinfo.value.failure_code == TARGET_FORBIDDEN


@pytest.mark.asyncio
async def test_loopback_http_still_requires_an_operator_cidr_allowlist() -> None:
    endpoint = parse_provider_endpoint("http://127.0.0.1:3425/v1")
    policy = ProviderEgressPolicy(resolver=resolver_for("127.0.0.1"))

    with pytest.raises(ProviderEgressError) as excinfo:
        await policy.authorize(endpoint)

    assert excinfo.value.failure_code == TARGET_FORBIDDEN


@pytest.mark.parametrize(
    "cidrs",
    [
        "127.0.0.0/8",
        ("10.0.0.0",),
        ("10.0.0.1/8",),
        (" 10.0.0.0/8",),
        ("garbage/8",),
    ],
)
def test_private_cidrs_are_strictly_validated(cidrs: object) -> None:
    with pytest.raises(ProviderEgressError) as excinfo:
        ProviderEgressPolicy(private_cidrs=cidrs)  # type: ignore[arg-type]

    assert excinfo.value.failure_code == CONFIG_INVALID


@pytest.mark.asyncio
async def test_peer_must_be_the_exact_authorized_address_and_still_satisfy_policy() -> None:
    endpoint = parse_provider_endpoint("https://provider.example/v1")
    policy = ProviderEgressPolicy(resolver=resolver_for(PUBLIC, PUBLIC_V6))
    authorization = await policy.authorize(endpoint)

    assert policy.authorize_peer(authorization, PUBLIC) == PUBLIC
    assert authorization.authorize_peer(PUBLIC_V6) == PUBLIC_V6

    for peer in (None, "8.8.8.8", "not-an-address", "10.0.0.1"):
        with pytest.raises(ProviderEgressError) as excinfo:
            authorization.authorize_peer(peer)
        assert excinfo.value.failure_code == PEER_FORBIDDEN
        assert str(excinfo.value) == PEER_FORBIDDEN


@pytest.mark.asyncio
async def test_mapped_ipv6_peer_matches_the_authorized_ipv4_address() -> None:
    endpoint = parse_provider_endpoint("https://provider.example/v1")
    policy = ProviderEgressPolicy(resolver=resolver_for("8.8.8.8"))
    authorization = await policy.authorize(endpoint)

    assert authorization.authorize_peer("::ffff:8.8.8.8") == "8.8.8.8"


@pytest.mark.asyncio
async def test_peer_is_rechecked_against_the_policy_doing_the_check() -> None:
    endpoint = parse_provider_endpoint("http://127.0.0.1:3425/v1")
    allowing_policy = ProviderEgressPolicy(
        private_cidrs=("127.0.0.0/8",),
        resolver=resolver_for("127.0.0.1"),
    )
    authorization = await allowing_policy.authorize(endpoint)
    stricter_policy = ProviderEgressPolicy()

    with pytest.raises(ProviderEgressError) as excinfo:
        stricter_policy.authorize_peer(authorization, "127.0.0.1")

    assert excinfo.value.failure_code == PEER_FORBIDDEN


def test_authorization_type_is_checked_before_peer_validation() -> None:
    policy = ProviderEgressPolicy()

    with pytest.raises(ProviderEgressError) as excinfo:
        policy.authorize_peer(object(), PUBLIC)  # type: ignore[arg-type]

    assert excinfo.value.failure_code == PEER_FORBIDDEN


@pytest.mark.asyncio
async def test_a_tampered_endpoint_is_refused_before_resolution() -> None:
    endpoint = ProviderEndpoint(
        url="https://provider.example/v1",
        scheme="https",
        host="attacker.example",
        port=443,
    )
    resolver = resolver_for(PUBLIC)
    policy = ProviderEgressPolicy(resolver=resolver)

    with pytest.raises(ProviderEgressError) as excinfo:
        await policy.authorize(endpoint)

    assert excinfo.value.failure_code == ENDPOINT_INVALID
    assert resolver.calls == []


def test_provider_private_cidr_setting_defaults_to_empty_and_is_operator_owned() -> None:
    local = declared_settings(testing=True, environment="local")
    production = declared_settings(
        testing=True,
        environment="production",
        auth_jwt_secret="x" * 32,
    )

    assert local.model_provider_private_cidrs == ()
    assert production.model_provider_private_cidrs == ()


def test_provider_private_cidr_setting_canonicalizes_explicit_networks() -> None:
    settings = declared_settings(
        testing=True,
        model_provider_private_cidrs=("127.0.0.0/8", "2001:DB8::/32"),
    )

    assert settings.model_provider_private_cidrs == ("127.0.0.0/8", "2001:db8::/32")


@pytest.mark.parametrize(
    "cidrs",
    [
        "127.0.0.0/8",
        ("10.0.0.0",),
        ("10.0.0.1/8",),
        ("garbage/8",),
    ],
)
def test_provider_private_cidr_setting_rejects_non_cidr_values(cidrs: object) -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            testing=True,
            model_provider_private_cidrs=cidrs,
        )


def test_invalid_url_errors_do_not_echo_credentials_or_internal_hosts() -> None:
    cases = (
        "https://admin:top-secret@internal-provider.example/v1",
        "https://internal-provider.example/v1?token=top-secret",
    )

    for value in cases:
        with pytest.raises(ProviderEgressError) as excinfo:
            normalize_provider_base_url(value)
        rendered = f"{str(excinfo.value)} {repr(excinfo.value)}"
        assert "internal-provider.example" not in rendered
        assert "top-secret" not in rendered
        assert "admin" not in rendered
