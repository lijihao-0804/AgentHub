"""Configure and smoke-check local model routes through the existing gateway.

Run from the repository root with ``python -m scripts.model_route``.
No schema setup, historical AgentVersion changes, or RAG quality runs occur here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import socket
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

from sqlalchemy import select

from packages.agent_runtime.context_budget import (
    ContextBudgetConfig,
    ContextBudgetPolicy,
    ContextCategory,
)
from packages.agent_runtime.models import AgentVersion
from packages.control_plane.models import OrganizationMembership, Workspace, WorkspaceMembership
from packages.control_plane.product_control_plane import ProductControlPlaneService
from packages.control_plane.rbac import resolve_permissions
from packages.core.config.settings import Settings
from packages.core.database import create_database
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.model_gateway.adapters.litellm import LiteLLMProviderAdapter
from packages.model_gateway.configured_route import build_gateway, load_route
from packages.model_gateway.contracts import ModelMessage, ModelRequest, ModelToolDefinition
from packages.model_gateway.credentials import ProviderCredentialCipher
from packages.model_gateway.gateway import SqlAlchemyModelGateway
from packages.model_gateway.models import ModelProfile, ProviderCredential

DEFAULT_CONFIG = Path("benchmarks/public_rag/model-route.json")


def projection(route):
    return {
        "name": route.name,
        "chain": [
            {
                "name": e.name,
                "provider": e.provider,
                "base_url": e.base_url,
                "model": e.model,
                "timeout_seconds": str(e.timeout_seconds),
            }
            for e in (route.primary, *route.fallbacks)
        ],
    }


def context(workspace_id, organization_id, user_id=None, *, org_role="OWNER", ws_role="DEVELOPER"):
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(
                user_id=str(user_id) if user_id else None,
                request_id="model-route-" + uuid4().hex,
                trace_id="model-route-" + uuid4().hex,
            ),
            organization_id=str(organization_id),
            org_role=org_role,
        ),
        workspace_id=str(workspace_id),
        workspace_role=ws_role,
        permissions=resolve_permissions(org_role, ws_role),
    )


def target(args):
    if args.state:
        state = json.loads(args.state.read_text(encoding="utf-8"))
        return (
            state["infrastructure"]["database_url"],
            UUID(state["identities"]["workspace_id"]),
            UUID(state["identities"]["user_id"]),
        )
    if not args.workspace_id or not args.actor_user_id:
        raise ValueError("install requires --state or explicit workspace and actor IDs")
    return Settings().database_url, args.workspace_id, args.actor_user_id


async def install(route, args):
    url, workspace_id, actor = target(args)
    engine, factory = create_database(url)
    service = ProductControlPlaneService()
    cipher = ProviderCredentialCipher.from_settings()
    installed = []
    try:
        async with factory() as db:
            workspace = await db.get(Workspace, workspace_id)
            if workspace is None:
                raise ValueError("workspace unavailable")
            member = await db.get(OrganizationMembership, (workspace.organization_id, actor))
            ws_member = await db.get(WorkspaceMembership, (workspace_id, actor))
            if member is None or member.role not in {"OWNER", "ADMIN"}:
                raise ValueError("actor must be an existing organization owner or administrator")
            ctx = context(
                workspace_id,
                workspace.organization_id,
                actor,
                org_role=member.role,
                ws_role=ws_member.role if ws_member else None,
            )
            before = dict(
                (
                    await db.execute(
                        select(AgentVersion.id, AgentVersion.resolved_spec).where(
                            AgentVersion.workspace_id == workspace_id
                        )
                    )
                ).all()
            )
            fallback_id = None
            for endpoint in reversed((route.primary, *route.fallbacks)):
                name = f"route:{route.name}:{endpoint.name}"
                credentials = (
                    await db.scalars(
                        select(ProviderCredential).where(
                            ProviderCredential.workspace_id == workspace_id,
                            ProviderCredential.name == name,
                            ProviderCredential.provider == endpoint.provider,
                            ProviderCredential.base_url == endpoint.base_url,
                            ProviderCredential.enabled.is_(True),
                        )
                    )
                ).all()
                credential = next(
                    (
                        c
                        for c in credentials
                        if c.secret_ciphertext
                        and cipher.decrypt(c.secret_ciphertext) == endpoint.api_key
                    ),
                    None,
                )
                if credential is None:
                    created = await service.create_provider_credential(
                        db,
                        ctx,
                        provider=endpoint.provider,
                        name=name,
                        secret=endpoint.api_key,
                        base_url=endpoint.base_url,
                        enabled=True,
                    )
                    credential = await db.get(ProviderCredential, created["id"])
                assert credential.secret is None and credential.secret_ciphertext
                values = {
                    "provider_credential_id": credential.id,
                    "model": endpoint.model,
                    "temperature": endpoint.temperature,
                    "max_tokens": endpoint.max_tokens,
                    "timeout_seconds": endpoint.timeout_seconds,
                    "capabilities": endpoint.capabilities,
                    "fallback_profile_id": fallback_id,
                    "enabled": True,
                }
                profiles = (
                    await db.scalars(
                        select(ModelProfile).where(
                            ModelProfile.workspace_id == workspace_id,
                            ModelProfile.provider_credential_id == credential.id,
                        )
                    )
                ).all()
                profile = next(
                    (
                        p
                        for p in profiles
                        if all(getattr(p, key) == value for key, value in values.items())
                    ),
                    None,
                )
                if profile is None:
                    profile = await service.create_model_profile(db, ctx, values)
                installed.append(
                    {
                        "profile_id": str(profile.id),
                        "model": profile.model,
                        "credential_id": str(credential.id),
                        "name": endpoint.name,
                    }
                )
                fallback_id = profile.id
            after = dict(
                (
                    await db.execute(
                        select(AgentVersion.id, AgentVersion.resolved_spec).where(
                            AgentVersion.workspace_id == workspace_id
                        )
                    )
                ).all()
            )
            assert before == after
            return {
                **projection(route),
                "workspace_id": str(workspace_id),
                "organization_id": str(workspace.organization_id),
                "actor_user_id": str(actor),
                "primary_profile_id": str(fallback_id),
                "profiles": list(reversed(installed)),
                "existing_versions_preserved": len(before),
                "credentials_encrypted": True,
                "activation": "Select primary_profile_id on a draft; publish a new version.",
            }
    finally:
        await engine.dispose()


class RecordedClient:
    def __init__(self, primary_base):
        self.primary_base = primary_base
        self.force_base = None
        self.calls = []

    async def acompletion(self, **kwargs):
        import litellm

        if len(self.calls) >= 6:
            raise ValueError("smoke request ceiling reached")
        original_base = kwargs.get("api_base")
        injected = self.force_base is not None and original_base == self.primary_base
        if injected:
            kwargs["api_base"] = self.force_base
        kwargs["max_tokens"] = min(kwargs["max_tokens"], 128)
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        entry = {
            "endpoint": kwargs.get("api_base"),
            "requested_model": kwargs["model"],
            "injected_connection_refusal": injected,
            "status": "DISPATCHED",
        }
        self.calls.append(entry)
        try:
            result = await litellm.acompletion(**kwargs)
            entry.update(status="RECEIVED", raw_response_model=getattr(result, "model", None))
            usage = getattr(result, "usage", None)
            if usage:
                entry["usage"] = {
                    k: getattr(usage, k, None)
                    for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                }
            return result
        except Exception as error:
            entry.update(status="FAILED", error_type=type(error).__name__)
            raise


async def smoke(route, args):
    client = RecordedClient(route.primary.base_url)
    adapter = LiteLLMProviderAdapter(client=client)
    engine = None
    if args.installed:
        installed = json.loads(args.installed.read_text(encoding="utf-8"))
        url, workspace_id, actor = target(args)
        if str(workspace_id) != installed["workspace_id"]:
            raise ValueError("installed route belongs to a different workspace")
        ctx = context(workspace_id, installed["organization_id"], actor)
        engine, factory = create_database(url)
        primary_id = UUID(installed["primary_profile_id"])

        async def generate(request):
            async with factory() as db:
                return await SqlAlchemyModelGateway(db, adapter=adapter).generate(
                    ctx, primary_id, request
                )
    else:
        ctx = context(uuid4(), uuid4())
        gateway, primary_id = build_gateway(route, ctx, adapter=adapter)

        async def generate(request):
            return await gateway.generate(ctx, primary_id, request)

    send = generate
    policy = ContextBudgetPolicy(
        profiles=[
            SimpleNamespace(max_context_tokens=e.capabilities["max_context_tokens"], max_tokens=128)
            for e in (route.primary, *route.fallbacks)
        ],
        config=ContextBudgetConfig(reserved_output_tokens=128),
    )

    async def generate(request):
        admitted = policy.admit(
            request.messages,
            categories=[ContextCategory.CURRENT_USER_TASK] * len(request.messages),
            tool_definitions=request.tools,
        )
        return await send(
            replace(request, messages=admitted.messages, tools=admitted.tool_definitions)
        )

    checks = []
    try:
        simple = ModelRequest(messages=(ModelMessage(role="user", content="Reply exactly OK."),))
        response = await generate(simple)
        checks.append(
            {
                "check": "primary_chat",
                "passed": response.content.strip() == "OK"
                and response.model == route.primary.model,
                "selected_model": response.model,
            }
        )
        tools = ModelRequest(
            messages=(
                ModelMessage(
                    role="user",
                    content=("Call route_probe with value ok. Do not answer with text."),
                ),
            ),
            tools=(
                ModelToolDefinition(
                    name="route_probe",
                    description="An inert interface check; does not execute actions.",
                    parameters={
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                ),
            ),
        )
        response = await generate(tools)
        checks.append(
            {
                "check": "primary_native_tools",
                "passed": response.model == route.primary.model
                and any(
                    c.name == "route_probe" and c.arguments == {"value": "ok"}
                    for c in response.tool_calls
                ),
                "selected_model": response.model,
            }
        )
        # A bound, non-listening local socket refuses the temporary probe connection.
        # The user's running Magpie gateway and the configured endpoint are untouched.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as refusal:
            refusal.bind(("127.0.0.1", 0))
            client.force_base = f"http://127.0.0.1:{refusal.getsockname()[1]}/v1"
            offset = len(client.calls)
            response = await generate(simple)
            attempts = client.calls[offset:]
            checks.append(
                {
                    "check": "real_connection_refusal_to_aliyun",
                    "passed": response.content.strip() == "OK"
                    and len(attempts) == 2
                    and attempts[0]["injected_connection_refusal"]
                    and attempts[0]["status"] == "FAILED"
                    and attempts[1]["endpoint"] == route.fallbacks[0].base_url
                    and response.model == route.fallbacks[0].model,
                    "selected_model": response.model,
                }
            )
        return {
            "passed": all(c["passed"] for c in checks),
            "checks": checks,
            "physical_calls": client.calls,
            "request_ceiling": 6,
            "per_request_output_cap": 128,
            "path": "persisted SQL gateway" if args.installed else "configuration gateway",
            "currency_cost": None,
            "scope": "Connectivity/tools/failover, not RAG quality.",
            "context_budget_applied": True,
        }
    except Exception as error:
        return {
            "passed": False,
            "checks": checks,
            "physical_calls": client.calls,
            "failure_type": type(error).__name__,
            "failure_code": str(getattr(error, "code", "SMOKE_FAILED")),
            "request_ceiling": 6,
            "per_request_output_cap": 128,
            "currency_cost": None,
            "scope": "Incomplete connectivity smoke; preserve attempts without retry.",
        }
    finally:
        if engine:
            await engine.dispose()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("show", "install", "smoke"))
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--workspace-id", type=UUID)
    parser.add_argument("--actor-user-id", type=UUID)
    parser.add_argument("--installed", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    route = load_route(args.config)
    if args.action == "show":
        result = projection(route)
    elif args.action == "install":
        result = await install(route, args)
    else:
        result = await smoke(route, args)
    result["recorded_at_utc"] = datetime.now(UTC).isoformat()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    if result.get("passed") is False:
        raise SystemExit(2)


if __name__ == "__main__":
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(main())
