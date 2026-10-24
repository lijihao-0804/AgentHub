"""Bounded configuration-backed model route for local RAG experiments."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from dotenv import dotenv_values

from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.model_gateway.adapters.litellm import LiteLLMProviderAdapter
from packages.model_gateway.errors import ModelGatewayError, ModelGatewayErrorCode
from packages.model_gateway.gateway import ModelGatewayService, ModelProviderAdapter
from packages.model_gateway.models import ModelProfile, ProviderCredential
from packages.observability.contracts import TraceSink

_SCHEMA_VERSION = 1
_ROUTE_NAME = "local-rag"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_CAPABILITIES = {
    "tool_calling",
    "streaming",
    "structured_output",
    "vision",
    "max_context_tokens",
}
_ENDPOINT_FIELDS = {
    "name",
    "provider",
    "model",
    "base_url",
    "base_url_env",
    "api_key",
    "api_key_env",
    "timeout_seconds",
    "max_tokens",
    "temperature",
    "capabilities",
}


@dataclass(frozen=True, slots=True)
class Endpoint:
    """One fully resolved endpoint. Secret values are excluded from repr."""

    name: str
    provider: str
    model: str
    base_url: str
    api_key: str = field(repr=False)
    temperature: Decimal = Decimal("0")
    max_tokens: int = 2048
    timeout_seconds: Decimal = Decimal("20")
    capabilities: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ConfiguredRoute:
    schema_version: int
    name: str
    primary: Endpoint
    fallbacks: tuple[Endpoint, ...]


class ConfiguredRouteError(ValueError):
    """A fixed, value-free configuration error."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class _InMemoryModelGatewayRepository:
    """Transient model data isolated to one workspace."""

    def __init__(
        self,
        profiles: Sequence[ModelProfile],
        credentials: Sequence[ProviderCredential],
    ) -> None:
        self._profiles = {profile.id: profile for profile in profiles}
        self._credentials = {credential.id: credential for credential in credentials}

    async def get_model_profile(
        self,
        context: WorkspaceExecutionContext,
        profile_id: UUID,
    ) -> ModelProfile | None:
        workspace_id = _context_workspace_id(context)
        profile = self._profiles.get(profile_id)
        if workspace_id is None or profile is None or profile.workspace_id != workspace_id:
            return None
        return profile

    async def get_provider_credential(
        self,
        context: WorkspaceExecutionContext,
        credential_id: UUID,
    ) -> ProviderCredential | None:
        workspace_id = _context_workspace_id(context)
        credential = self._credentials.get(credential_id)
        if workspace_id is None or credential is None or credential.workspace_id != workspace_id:
            return None
        return credential

    async def list_model_profiles(
        self,
        context: WorkspaceExecutionContext,
    ) -> Sequence[ModelProfile]:
        workspace_id = _context_workspace_id(context)
        if workspace_id is None:
            return ()
        return tuple(
            profile for profile in self._profiles.values() if profile.workspace_id == workspace_id
        )


def load_route(
    path: Path,
    *,
    env: Mapping[str, str] | None = None,
    env_file: Path = Path(".env"),
) -> ConfiguredRoute:
    """Load a strict route; process environment wins over the dotenv file."""

    try:
        raw = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ConfiguredRouteError):
        raise ConfiguredRouteError("route configuration could not be read") from None
    document = _mapping(raw, "route configuration must be an object")
    _exact_keys(
        document,
        {"schema_version", "name", "retry_policy", "primary", "fallbacks"},
        "route configuration",
    )
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ConfiguredRouteError("schema_version must be 1")
    if document["name"] != _ROUTE_NAME:
        raise ConfiguredRouteError("route name must be local-rag")
    retry_policy = _mapping(document["retry_policy"], "retry_policy must be an object")
    _exact_keys(retry_policy, {"max_attempts"}, "retry_policy")
    if type(retry_policy["max_attempts"]) is not int or retry_policy["max_attempts"] != 1:
        raise ConfiguredRouteError("retry_policy.max_attempts must be 1")

    file_values = _read_env_file(env_file)
    process_values = dict(env) if env is not None else _process_environment()
    values = {
        **file_values,
        **{
            name: value
            for name, value in process_values.items()
            if isinstance(name, str) and isinstance(value, str) and value.strip()
        },
    }

    primary = _load_endpoint(document["primary"], values, kind="primary")
    raw_fallbacks = document["fallbacks"]
    if not isinstance(raw_fallbacks, list) or not 1 <= len(raw_fallbacks) <= 2:
        raise ConfiguredRouteError("fallbacks must contain one or two endpoints")
    fallbacks = tuple(
        _load_endpoint(value, values, kind=f"fallback {index}")
        for index, value in enumerate(raw_fallbacks, start=1)
    )
    names = [primary.name, *(endpoint.name for endpoint in fallbacks)]
    if len(set(names)) != len(names):
        raise ConfiguredRouteError("endpoint names must be unique")
    return ConfiguredRoute(
        schema_version=_SCHEMA_VERSION,
        name=_ROUTE_NAME,
        primary=primary,
        fallbacks=fallbacks,
    )


def build_gateway(
    route: ConfiguredRoute,
    context: WorkspaceExecutionContext,
    *,
    adapter: ModelProviderAdapter | None = None,
    trace_sink: TraceSink | None = None,
) -> tuple[ModelGatewayService, UUID]:
    """Create a transient production gateway for one workspace."""

    workspace_id = _context_workspace_id(context)
    if workspace_id is None:
        raise ModelGatewayError(ModelGatewayErrorCode.MODEL_PROFILE_DISABLED)

    endpoints = (route.primary, *route.fallbacks)
    profiles: list[ModelProfile] = []
    credentials: list[ProviderCredential] = []
    for endpoint in endpoints:
        credential = ProviderCredential(
            id=uuid4(),
            workspace_id=workspace_id,
            provider=endpoint.provider,
            name=endpoint.name,
            secret=endpoint.api_key,
            base_url=endpoint.base_url,
            enabled=True,
        )
        profile = ModelProfile(
            id=uuid4(),
            workspace_id=workspace_id,
            provider_credential_id=credential.id,
            model=endpoint.model,
            temperature=endpoint.temperature,
            max_tokens=endpoint.max_tokens,
            timeout_seconds=endpoint.timeout_seconds,
            capabilities={
                "tool_calling": endpoint.capabilities["tool_calling"],
                "streaming": endpoint.capabilities["streaming"],
                "structured_output": endpoint.capabilities["structured_output"],
                "vision": endpoint.capabilities["vision"],
                "max_context_tokens": endpoint.capabilities["max_context_tokens"],
            },
            enabled=True,
        )
        credentials.append(credential)
        profiles.append(profile)
    for profile, fallback in zip(profiles, profiles[1:], strict=False):
        profile.fallback_profile_id = fallback.id

    service = ModelGatewayService(
        _InMemoryModelGatewayRepository(profiles, credentials),
        adapter if adapter is not None else LiteLLMProviderAdapter(),
        trace_sink,
    )
    return service, profiles[0].id


def _load_endpoint(
    value: Any,
    environment: Mapping[str, str],
    *,
    kind: str,
) -> Endpoint:
    document = _mapping(value, f"{kind} must be an object")
    _exact_keys(document, _ENDPOINT_FIELDS, kind)
    name = _text(document["name"], f"{kind} name")
    provider = _text(document["provider"], f"{kind} provider")
    if provider not in {"openai-compatible", "openai", "deepseek"}:
        raise ConfiguredRouteError(f"{kind} provider is unsupported")
    model = _text(document["model"], f"{kind} model")
    base_url = _resolve_endpoint_value(document, environment, "base_url", kind)
    _validate_base_url(base_url)
    api_key = _resolve_endpoint_value(document, environment, "api_key", kind)
    if not api_key:
        raise ConfiguredRouteError(f"{kind} credential is missing")
    direct_api_key = document["api_key"]
    if direct_api_key is not None and not (
        direct_api_key == "magpie" and _is_loopback_url(base_url)
    ):
        raise ConfiguredRouteError(f"{kind} api_key must not be stored in route configuration")
    temperature = _decimal(document["temperature"], f"{kind} temperature")
    if temperature < 0:
        raise ConfiguredRouteError(f"{kind} temperature must be non-negative")
    timeout_seconds = _decimal(document["timeout_seconds"], f"{kind} timeout_seconds")
    if timeout_seconds <= 0:
        raise ConfiguredRouteError(f"{kind} timeout_seconds must be positive")
    max_tokens = document["max_tokens"]
    if type(max_tokens) is not int or max_tokens <= 0:
        raise ConfiguredRouteError(f"{kind} max_tokens must be positive")
    capabilities = _mapping(document["capabilities"], f"{kind} capabilities")
    _exact_keys(capabilities, _CAPABILITIES, f"{kind} capabilities")
    for capability in _CAPABILITIES - {"max_context_tokens"}:
        if type(capabilities[capability]) is not bool:
            raise ConfiguredRouteError(f"{kind} capabilities must be boolean")
    max_context_tokens = capabilities["max_context_tokens"]
    if type(max_context_tokens) is not int or max_context_tokens <= 0:
        raise ConfiguredRouteError(f"{kind} max_context_tokens must be positive")
    return Endpoint(
        name=name,
        provider=provider,
        model=model,
        base_url=base_url,
        api_key=api_key,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout_seconds=timeout_seconds,
        capabilities={
            "tool_calling": capabilities["tool_calling"],
            "streaming": capabilities["streaming"],
            "structured_output": capabilities["structured_output"],
            "vision": capabilities["vision"],
            "max_context_tokens": max_context_tokens,
        },
    )


def _resolve_endpoint_value(
    document: Mapping[str, Any],
    environment: Mapping[str, str],
    field_name: str,
    kind: str,
) -> str:
    direct = document[field_name]
    variable = document[f"{field_name}_env"]
    if direct is not None and variable is not None:
        raise ConfiguredRouteError(f"{kind} {field_name} has conflicting sources")
    if direct is not None:
        return _text(direct, f"{kind} {field_name}")
    if variable is not None:
        env_name = _text(variable, f"{kind} {field_name}_env")
        configured = environment.get(env_name)
        if configured is not None and configured.strip():
            return configured.strip()
    if field_name == "api_key" and variable == "AGENTHUB_MAGPIE_API_KEY":
        loopback_value = document["base_url"]
        if loopback_value is None:
            env_name = _text(document["base_url_env"], f"{kind} base_url_env")
            loopback_value = environment.get(env_name)
        if not isinstance(loopback_value, str):
            raise ConfiguredRouteError(f"{kind} base_url is missing")
        try:
            urlsplit(loopback_value.strip())
        except ValueError:
            raise ConfiguredRouteError(f"{kind} base_url is invalid") from None
        if _is_loopback_url(loopback_value.strip()):
            return "magpie"
    raise ConfiguredRouteError(f"{kind} {field_name} is missing")


def _validate_base_url(value: str) -> None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ConfiguredRouteError("endpoint URL is invalid") from None
    if any(
        (
            value != value.strip(),
            any(character.isspace() for character in value),
            "\\" in value,
            "@" in value,
            not parsed.hostname,
            parsed.username is not None,
            parsed.password is not None,
            parsed.query,
            parsed.fragment,
            "?" in value,
            "#" in value,
        )
    ):
        raise ConfiguredRouteError("endpoint URL must not contain credentials or extras")
    if port is not None and not 1 <= port <= 65535:
        raise ConfiguredRouteError("endpoint URL port is invalid")
    if parsed.scheme == "https":
        return
    if parsed.scheme != "http" or parsed.hostname not in _LOOPBACK_HOSTS:
        raise ConfiguredRouteError("HTTP endpoints must use loopback")


def _read_env_file(path: Path) -> dict[str, str]:
    try:
        if not path.exists():
            return {}
        loaded = dotenv_values(path, encoding="utf-8-sig")
    except (OSError, UnicodeError):
        raise ConfiguredRouteError("route environment could not be read") from None
    return {
        key: value
        for key, value in loaded.items()
        if isinstance(key, str) and isinstance(value, str) and value.strip()
    }


def _process_environment() -> dict[str, str]:
    import os

    return {key: value for key, value in os.environ.items() if value.strip()}


def _context_workspace_id(context: WorkspaceExecutionContext) -> UUID | None:
    try:
        return UUID(context.workspace_id)
    except (AttributeError, TypeError, ValueError):
        return None


def _is_loopback_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme == "http" and parsed.hostname in _LOOPBACK_HOSTS


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ConfiguredRouteError("route configuration contains duplicate keys")
        result[key] = value
    return result


def _mapping(value: Any, message: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfiguredRouteError(message)
    return value


def _exact_keys(
    value: Mapping[str, Any],
    expected: set[str],
    description: str,
) -> None:
    if set(value) != expected:
        raise ConfiguredRouteError(f"{description} has an invalid schema")


def _text(value: Any, description: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfiguredRouteError(f"{description} must be non-empty")
    return value.strip()


def _decimal(value: Any, description: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ConfiguredRouteError(f"{description} must be numeric")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ConfiguredRouteError(f"{description} must be numeric") from None
    if not number.is_finite():
        raise ConfiguredRouteError(f"{description} must be finite")
    return number


__all__ = ["ConfiguredRoute", "ConfiguredRouteError", "Endpoint", "build_gateway", "load_route"]
