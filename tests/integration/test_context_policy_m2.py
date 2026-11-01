from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.frozen import parse_frozen_agent_spec
from packages.agent_runtime.models import Agent, AgentKnowledgeBinding, AgentRun, AgentVersion
from packages.agent_runtime.publish import AgentPublishService
from packages.agent_runtime.runtime import AgentRunService
from packages.control_plane.models import (
    Organization,
    OrganizationMembership,
    User,
    Workspace,
)
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.config.settings import Settings, get_settings
from packages.core.database import create_database
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.knowledge.contracts import RetrievalStrategy
from packages.knowledge.models import (
    Document,
    DocumentRevision,
    KnowledgeBase,
    RevisionIngestionStatus,
    RevisionLifecycleStatus,
)
from packages.knowledge.snapshots import KnowledgeSnapshotService
from packages.model_gateway.context_metadata import (
    ContextMetadata,
    InputOutputSemantics,
    OutputReserveSemantics,
)
from packages.model_gateway.models import ModelProfile, ProviderCredential

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Set AGENTHUB_TEST_DATABASE_URL to run the M2 PostgreSQL integration tests.",
    ),
]


def _async_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+asyncpg://", 1)
    return database_url


@pytest_asyncio.fixture
async def db_factory() -> async_sessionmaker[AsyncSession]:
    engine, factory = create_database(_async_database_url(TEST_DATABASE_URL))
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture
def catalog_dir() -> Path:
    directory = Path(os.environ["TEMP"]) / f"agenthub-context-policy-m2-{uuid4().hex}"
    directory.mkdir()
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _context(user_id: UUID, workspace_id: UUID, organization_id: UUID) -> WorkspaceExecutionContext:
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(
                request_id=f"context-policy-m2-{uuid4().hex}",
                trace_id=f"context-policy-m2-{uuid4().hex}",
                user_id=str(user_id),
            ),
            organization_id=str(organization_id),
            org_role="OWNER",
        ),
        workspace_id=str(workspace_id),
        workspace_role="DEVELOPER",
        permissions=frozenset(
            {
                "agent_create",
                "agent_edit",
                "agent_run",
                "knowledge_run",
                "workspace_read",
            }
        ),
    )


def _declared_metadata(resolved_model: str = "UNKNOWN") -> ContextMetadata:
    return ContextMetadata(
        deployment_id="deployment-a",
        resolved_model=resolved_model,
        source_version="agent-profile-v1",
        max_input_tokens=200_000,
        max_output_tokens=32_000,
        deployment_input_limit=140_000,
        input_output_semantics=InputOutputSemantics.TOTAL_WINDOW,
        output_reserve_semantics=OutputReserveSemantics.INCLUDES_REASONING,
        catalog_ref="profile-declaration",
    )


def _i_only_metadata() -> ContextMetadata:
    return ContextMetadata(
        deployment_id="deployment-a",
        resolved_model="UNKNOWN",
        source_version="agent-profile-v1",
        max_input_tokens=120_000,
        max_output_tokens=16_384,
        input_output_semantics=InputOutputSemantics.INDEPENDENT_INPUT_OUTPUT,
        output_reserve_semantics=OutputReserveSemantics.INCLUDES_REASONING,
        catalog_ref="forged-profile-catalog",
        source_records=(
            {
                "kind": "LOCAL_CATALOG",
                "catalog_id": "forged-source-record",
            },
        ),
    )


def _catalog_document(
    *,
    catalog_id: str,
    source_version: str,
    source_label: str,
    total_context_tokens: int | None,
    deployment_input_limit: int | None,
    resolved_model: str,
    provider: str = "offline",
    model: str = "m2-exact-model",
    deployment_id: str = "deployment-a",
    input_output_semantics: InputOutputSemantics = InputOutputSemantics.TOTAL_WINDOW,
    max_input_tokens: int | None = None,
    max_output_tokens: int | None = None,
) -> dict[str, object]:
    if max_input_tokens is None and total_context_tokens is not None:
        max_input_tokens = min(150_000, total_context_tokens)
    if max_output_tokens is None and total_context_tokens is not None:
        max_output_tokens = min(24_000, total_context_tokens)
    return {
        "schema_version": 1,
        "catalog_id": catalog_id,
        "source_version": source_version,
        "source_label": source_label,
        "entries": [
            {
                "provider": provider,
                "model": model,
                "deployment_id": deployment_id,
                "max_context_tokens": total_context_tokens,
                "metadata": {
                    "resolved_model": resolved_model,
                    "source_version": source_version,
                    "max_input_tokens": max_input_tokens,
                    "max_output_tokens": max_output_tokens,
                    "deployment_input_limit": deployment_input_limit,
                    "input_output_semantics": input_output_semantics.value,
                    "output_reserve_semantics": "INCLUDES_REASONING",
                },
            }
        ],
    }


def _write_catalog(path: Path, value: dict[str, object]) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True),
        encoding="utf-8",
    )


async def _create_workspace(session: AsyncSession) -> dict[str, object]:
    label = uuid4().hex
    email = f"context-policy-m2-{label}@example.test"
    user = User(email=email, normalized_email=email, password_hash="not-used")
    session.add(user)
    await session.flush()
    organization = Organization(name=f"M2 {label}", created_by=user.id)
    session.add(organization)
    await session.flush()
    session.add(
        OrganizationMembership(organization_id=organization.id, user_id=user.id, role="OWNER")
    )
    workspace = Workspace(organization_id=organization.id, name=f"M2 {label}")
    session.add(workspace)
    await session.flush()
    credential = ProviderCredential(
        workspace_id=workspace.id,
        provider="offline",
        name=f"m2-credential-{label}",
        secret="local-only-test-secret",
    )
    session.add(credential)
    await session.flush()
    profile = ModelProfile(
        workspace_id=workspace.id,
        provider_credential_id=credential.id,
        model="m2-exact-model",
        temperature=0,
        max_tokens=8_192,
        timeout_seconds=10,
        capabilities={
            "streaming": True,
            "max_context_tokens": 256_000,
            "context_metadata": _declared_metadata().as_dict(),
        },
    )
    session.add(profile)
    await session.commit()
    return {
        "user": user,
        "workspace": workspace,
        "profile": profile,
        "context": _context(user.id, workspace.id, organization.id),
    }


async def _create_knowledge_base(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    label: str,
) -> dict[str, object]:
    knowledge_base = KnowledgeBase(workspace_id=workspace_id, name=f"M2 KB {label}")
    session.add(knowledge_base)
    await session.flush()
    document = Document(
        workspace_id=workspace_id,
        knowledge_base_id=knowledge_base.id,
        name=f"{label}.txt",
    )
    session.add(document)
    await session.flush()
    revision = DocumentRevision(
        workspace_id=workspace_id,
        knowledge_base_id=knowledge_base.id,
        document_id=document.id,
        revision_number=1,
        original_filename=f"{label}.txt",
        blob_key=f"m2/{uuid4().hex}",
        media_type="text/plain",
        file_size=32,
        ingestion_status=RevisionIngestionStatus.READY,
        lifecycle_status=RevisionLifecycleStatus.ACTIVE,
    )
    session.add(revision)
    await session.commit()
    return {
        "knowledge_base": knowledge_base,
        "document": document,
        "revision": revision,
    }


async def _create_published_agent(
    session: AsyncSession,
    base: dict[str, object],
    knowledge: dict[str, object],
    *,
    binding_mode: str,
    snapshot_id: UUID | None,
    catalog_path: Path,
    reserved_output_tokens: int,
) -> tuple[Agent, AgentVersion, UUID]:
    workspace = base["workspace"]
    assert isinstance(workspace, Workspace)
    profile = base["profile"]
    assert isinstance(profile, ModelProfile)
    context = base["context"]
    assert isinstance(context, WorkspaceExecutionContext)
    knowledge_base = knowledge["knowledge_base"]
    assert isinstance(knowledge_base, KnowledgeBase)
    service = AgentPublishService(
        settings=Settings(
            testing=True,
            model_context_catalog_path=str(catalog_path),
        )
    )
    agent = await service.create_draft(
        session,
        context,
        name=f"M2 policy {uuid4().hex}",
        system_prompt="Use only the frozen context.",
        model_profile_id=profile.id,
        knowledge_binding_mode=binding_mode,
        retrieval_config={
            "embedding_model": get_settings().knowledge_embedding_model,
            "reranker_model": get_settings().knowledge_reranker_model,
            "retrieval_strategy": RetrievalStrategy.HYBRID_RERANK.value,
        },
        runtime_config={
            "context_budget": {
                "reserved_output_tokens": reserved_output_tokens,
                "max_retrieval_tokens": 1_000,
                "max_tool_result_tokens": 1_000,
            },
            "context_policy": {
                "policy_version": "adaptive_context_v1",
                "preset": "GENERAL_64K",
            },
        },
    )
    session.add(
        AgentKnowledgeBinding(
            workspace_id=workspace.id,
            agent_id=agent.id,
            knowledge_base_id=knowledge_base.id,
            binding_mode=binding_mode,
            snapshot_id=snapshot_id,
        )
    )
    await session.commit()
    published = await service.publish(session, context, agent.id)
    version = await session.get(AgentVersion, published.id)
    assert version is not None
    return agent, version, knowledge_base.id


async def _create_unbound_policy_draft(
    session: AsyncSession,
    base: dict[str, object],
    *,
    catalog_path: Path,
    reserved_output_tokens: int = 4_096,
) -> tuple[AgentPublishService, Agent]:
    workspace = base["workspace"]
    assert isinstance(workspace, Workspace)
    profile = base["profile"]
    assert isinstance(profile, ModelProfile)
    context = base["context"]
    assert isinstance(context, WorkspaceExecutionContext)
    service = AgentPublishService(
        settings=Settings(
            testing=True,
            model_context_catalog_path=str(catalog_path),
        )
    )
    agent = await service.create_draft(
        session,
        context,
        name=f"M2 unbound policy {uuid4().hex}",
        system_prompt="Use only the frozen context.",
        model_profile_id=profile.id,
        knowledge_binding_mode="LATEST",
        runtime_config={
            "context_budget": {
                "reserved_output_tokens": reserved_output_tokens,
            },
            "context_policy": {
                "policy_version": "adaptive_context_v1",
                "preset": "GENERAL_64K",
            },
        },
    )
    return service, agent


class _NoNetworkModelFactory:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, session: AsyncSession):
        del session
        self.calls += 1
        raise AssertionError("M2 prepare_run must not construct a business model gateway")


@pytest.mark.parametrize(
    "catalog_overrides",
    [
        {"model": "m2-exact-model-v2"},
        {"provider": "near-offline"},
        {"deployment_id": "deployment-a-legacy"},
    ],
    ids=["near-model-name", "provider-mismatch", "deployment-mismatch"],
)
@pytest.mark.asyncio
async def test_i_only_publication_rejects_untrusted_or_nonexact_catalog_proof(
    db_factory: async_sessionmaker[AsyncSession],
    catalog_dir: Path,
    catalog_overrides: dict[str, str],
) -> None:
    catalog_path = catalog_dir / "untrusted-i-only-catalog.json"
    _write_catalog(
        catalog_path,
        _catalog_document(
            catalog_id="m2-untrusted-i-only",
            source_version="2026-10-27-untrusted",
            source_label="near-match catalog",
            total_context_tokens=128_000,
            deployment_input_limit=96_000,
            resolved_model="m2-untrusted-model",
            **catalog_overrides,
        ),
    )

    async with db_factory() as session:
        base = await _create_workspace(session)
        profile = base["profile"]
        assert isinstance(profile, ModelProfile)
        profile.capabilities = {
            "streaming": True,
            "context_metadata": _i_only_metadata().as_dict(),
        }
        await session.commit()
        service, agent = await _create_unbound_policy_draft(
            session,
            base,
            catalog_path=catalog_path,
        )
        agent_id = agent.id
        workspace_id = agent.workspace_id
        context = base["context"]
        assert isinstance(context, WorkspaceExecutionContext)

        with pytest.raises(AgentHubError) as raised:
            await service.publish(session, context, agent_id)

        assert raised.value.code == "INVALID_CONTEXT_POLICY"
        assert raised.value.status_code == 422
        await session.rollback()
        published_versions = list(
            (
                await session.scalars(
                    select(AgentVersion).where(
                        AgentVersion.workspace_id == workspace_id,
                        AgentVersion.agent_id == agent_id,
                    )
                )
            ).all()
        )
        assert published_versions == []


@pytest.mark.asyncio
async def test_i_only_exact_catalog_hit_is_frozen_after_catalog_drift(
    db_factory: async_sessionmaker[AsyncSession],
    catalog_dir: Path,
) -> None:
    catalog_path = catalog_dir / "exact-i-only-catalog.json"
    _write_catalog(
        catalog_path,
        _catalog_document(
            catalog_id="m2-exact-i-only",
            source_version="2026-10-27-exact",
            source_label="trusted independent input/output catalog",
            total_context_tokens=None,
            deployment_input_limit=48_000,
            resolved_model="m2-resolved-i-only",
            input_output_semantics=InputOutputSemantics.INDEPENDENT_INPUT_OUTPUT,
            max_input_tokens=64_000,
            max_output_tokens=16_384,
        ),
    )

    async with db_factory() as session:
        base = await _create_workspace(session)
        profile = base["profile"]
        assert isinstance(profile, ModelProfile)
        profile.capabilities = {
            "streaming": True,
            "context_metadata": _i_only_metadata().as_dict(),
        }
        await session.commit()
        context = base["context"]
        assert isinstance(context, WorkspaceExecutionContext)
        service, agent = await _create_unbound_policy_draft(
            session,
            base,
            catalog_path=catalog_path,
        )
        published = await service.publish(session, context, agent.id)
        version = await session.get(AgentVersion, published.id)
        assert version is not None

        policy = version.resolved_spec["runtime"]["context_policy"]
        model = policy["models"][0]
        source = model["source"]
        assert model["limits"] == {
            "requested_max_tokens": 8_192,
            "total_context_tokens": None,
            "max_input_tokens": 64_000,
            "deployment_input_limit": 48_000,
            "max_output_tokens": 16_384,
            "input_output_semantics": "INDEPENDENT_INPUT_OUTPUT",
            "output_reserve_semantics": "INCLUDES_REASONING",
        }
        assert policy["input_limit_tokens"] == 45_600
        assert policy["min_total_context_tokens"] is None
        assert "MIN_TOTAL_CONTEXT_TOKENS" in policy["unknowns"]
        assert source["declared_max_context_tokens"] is None
        assert source["resolved_model"] == "m2-resolved-i-only"
        assert source["catalog_ref"].startswith("m2-exact-i-only@2026-10-27-exact#")
        assert [record["kind"] for record in source["source_records"]] == ["INTERSECTION"]
        intersection = source["source_records"][0]
        assert [record["kind"] for record in intersection["sources"]] == [
            "DECLARED",
            "LOCAL_CATALOG",
        ]
        local_catalog = intersection["sources"][1]
        assert local_catalog["provider"] == "offline"
        assert local_catalog["model"] == "m2-exact-model"
        assert local_catalog["deployment_id"] == "deployment-a"
        assert local_catalog["limits"]["max_context_tokens"] is None
        assert local_catalog["limits"]["input_output_semantics"] == "INDEPENDENT_INPUT_OUTPUT"
        assert local_catalog["limits"]["max_input_tokens"] == 64_000
        assert "forged-profile-catalog" not in json.dumps(source, sort_keys=True)
        assert "forged-source-record" not in json.dumps(source, sort_keys=True)
        version_id = version.id
        version_hash = version.resolved_spec_hash
        frozen_spec = json.loads(json.dumps(version.resolved_spec))

    _write_catalog(
        catalog_path,
        _catalog_document(
            catalog_id="m2-drifted-i-only",
            source_version="2026-10-27-drifted",
            source_label="drifted catalog",
            total_context_tokens=32_000,
            deployment_input_limit=16_000,
            resolved_model="m2-drifted-model",
        ),
    )
    async with db_factory() as session:
        fresh_version = await session.get(AgentVersion, version_id)
        assert fresh_version is not None
        assert fresh_version.resolved_spec == frozen_spec

    model_factory = _NoNetworkModelFactory()
    recovered_run = await AgentRunService(
        db_factory,
        model_gateway_factory=model_factory,
    ).prepare_run(
        context,
        agent_version_id=version_id,
        input_text="recover after the trusted independent catalog drifts",
    )
    assert model_factory.calls == 0
    assert recovered_run.resolved_spec_hash == version_hash


@pytest.mark.asyncio
async def test_total_window_catalog_miss_remains_declared_and_unknown(
    db_factory: async_sessionmaker[AsyncSession],
    catalog_dir: Path,
) -> None:
    catalog_path = catalog_dir / "missing-total-window-catalog.json"
    _write_catalog(
        catalog_path,
        _catalog_document(
            catalog_id="m2-near-total-window",
            source_version="2026-10-27-near",
            source_label="near-name catalog",
            total_context_tokens=128_000,
            deployment_input_limit=96_000,
            resolved_model="m2-near-model",
            model="m2-exact-model-v2",
        ),
    )

    async with db_factory() as session:
        base = await _create_workspace(session)
        context = base["context"]
        assert isinstance(context, WorkspaceExecutionContext)
        service, agent = await _create_unbound_policy_draft(
            session,
            base,
            catalog_path=catalog_path,
        )
        published = await service.publish(session, context, agent.id)
        version = await session.get(AgentVersion, published.id)
        assert version is not None

        policy = version.resolved_spec["runtime"]["context_policy"]
        model = policy["models"][0]
        source = model["source"]
        assert source["declared_max_context_tokens"] == 256_000
        assert source["resolved_model"] == "UNKNOWN"
        assert source["source_version"] == "agent-profile-v1"
        assert source["catalog_ref"] is None
        assert source["source_records"] == [
            {
                "kind": "DECLARED",
                "source_version": "agent-profile-v1",
                "deployment_id": "deployment-a",
                "resolved_model": "UNKNOWN",
                "catalog_ref": None,
                "limits": {
                    "max_context_tokens": 256_000,
                    "max_input_tokens": 200_000,
                    "max_output_tokens": 32_000,
                    "deployment_input_limit": 140_000,
                    "input_output_semantics": "TOTAL_WINDOW",
                    "output_reserve_semantics": "INCLUDES_REASONING",
                },
            }
        ]
        assert model["limits"]["input_output_semantics"] == "TOTAL_WINDOW"
        assert "RESOLVED_MODEL" in policy["unknowns"]
        version_id = version.id
        version_hash = version.resolved_spec_hash

    model_factory = _NoNetworkModelFactory()
    recovered_run = await AgentRunService(
        db_factory,
        model_gateway_factory=model_factory,
    ).prepare_run(
        context,
        agent_version_id=version_id,
        input_text="recover the declared total-window publication",
    )
    assert model_factory.calls == 0
    assert recovered_run.resolved_spec_hash == version_hash


@pytest.mark.asyncio
async def test_publish_and_recover_freeze_catalog_proof_and_pinned_snapshot_after_drift(
    db_factory: async_sessionmaker[AsyncSession],
    catalog_dir: Path,
) -> None:
    catalog_path = catalog_dir / "context-catalog.json"
    catalog_a_path = catalog_dir / "catalog-a.json"
    catalog_b_path = catalog_dir / "catalog-b.json"
    _write_catalog(
        catalog_path,
        _catalog_document(
            catalog_id="m2-catalog-a",
            source_version="2026-10-27-a",
            source_label="offline catalog A",
            total_context_tokens=128_000,
            deployment_input_limit=96_000,
            resolved_model="m2-model-a",
        ),
    )
    catalog_a_path.write_bytes(catalog_path.read_bytes())
    catalog_a = json.loads(catalog_a_path.read_text(encoding="utf-8"))
    catalog_a_sha = hashlib.sha256(catalog_a_path.read_bytes()).hexdigest()

    async with db_factory() as session:
        base = await _create_workspace(session)
        workspace = base["workspace"]
        assert isinstance(workspace, Workspace)
        knowledge = await _create_knowledge_base(
            session,
            workspace_id=workspace.id,
            label="pinned-a",
        )
        context = base["context"]
        assert isinstance(context, WorkspaceExecutionContext)
        snapshot_a = await KnowledgeSnapshotService().create_current_snapshot(
            session,
            context,
            knowledge["knowledge_base"].id,
        )
        agent, version_a, knowledge_base_id = await _create_published_agent(
            session,
            base,
            knowledge,
            binding_mode="PINNED",
            snapshot_id=snapshot_a.snapshot_id,
            catalog_path=catalog_path,
            reserved_output_tokens=4_096,
        )
        first_run = await AgentRunService(
            db_factory,
            model_gateway_factory=_NoNetworkModelFactory(),
        ).prepare_run(
            context,
            agent_version_id=version_a.id,
            input_text="prepare from catalog A and snapshot A",
        )

        assert version_a.spec_schema_version == 3
        policy = version_a.resolved_spec["runtime"]["context_policy"]
        models = policy["models"]
        assert len(models) == 1
        source = models[0]["source"]
        assert source["declared_max_context_tokens"] == 128_000
        assert source["resolved_model"] == "m2-model-a"
        assert source["source_version"] == "agent-profile-v1+2026-10-27-a"
        assert "m2-catalog-a@2026-10-27-a#catalog-sha256:" in source["catalog_ref"]
        assert source["source_records"]
        kinds = [record["kind"] for record in source["source_records"]]
        assert kinds == ["INTERSECTION"]
        intersection = source["source_records"][-1]
        assert intersection["kind"] == "INTERSECTION"
        assert [item["kind"] for item in intersection["sources"]] == [
            "DECLARED",
            "LOCAL_CATALOG",
        ]
        local_catalog = intersection["sources"][1]
        assert local_catalog["catalog_id"] == "m2-catalog-a"
        assert local_catalog["source_version"] == "2026-10-27-a"
        assert local_catalog["source_label"] == "offline catalog A"
        assert local_catalog["content_sha256"] == catalog_a_sha
        assert local_catalog["provider"] == "offline"
        assert local_catalog["model"] == "m2-exact-model"
        assert local_catalog["deployment_id"] == "deployment-a"
        assert local_catalog["resolved_model"] == "m2-model-a"
        assert local_catalog["limits"]["max_context_tokens"] == 128_000
        assert local_catalog["limits"]["deployment_input_limit"] == 96_000
        assert intersection["resolved_limits"]["max_context_tokens"] == 128_000
        assert intersection["resolved_limits"]["max_input_tokens"] == 128_000
        assert intersection["resolved_limits"]["deployment_input_limit"] == 96_000
        assert policy["model_available_input_tokens"] == 91_200
        assert policy["input_limit_tokens"] == 65_536
        assert policy["thresholds"] == {
            "soft": 45_876,
            "hard": 55_706,
            "target": 36_044,
        }
        assert first_run.effective_knowledge_snapshots == [
            {
                "knowledge_base_id": str(knowledge_base_id),
                "snapshot_id": str(snapshot_a.snapshot_id),
                "snapshot_hash": snapshot_a.content_hash,
            }
        ]
        assert first_run.resolved_spec_hash == version_a.resolved_spec_hash
        version_a_id = version_a.id
        frozen_a_spec = json.loads(json.dumps(version_a.resolved_spec))

    _write_catalog(
        catalog_b_path,
        _catalog_document(
            catalog_id="m2-catalog-b",
            source_version="2026-10-27-b",
            source_label="offline catalog B",
            total_context_tokens=64_000,
            deployment_input_limit=48_000,
            resolved_model="m2-model-b",
        ),
    )
    catalog_b_path.replace(catalog_path)

    async with db_factory() as session:
        fresh_version = await session.get(AgentVersion, version_a_id)
        assert fresh_version is not None
        assert fresh_version.resolved_spec == frozen_a_spec
        fresh_version_id = fresh_version.id

    model_factory = _NoNetworkModelFactory()
    recovered_run = await AgentRunService(
        db_factory,
        model_gateway_factory=model_factory,
    ).prepare_run(
        context,
        agent_version_id=fresh_version_id,
        input_text="recover after catalog and snapshot drift",
    )
    assert model_factory.calls == 0
    assert recovered_run.effective_knowledge_snapshots == first_run.effective_knowledge_snapshots
    assert recovered_run.resolved_spec_hash == version_a.resolved_spec_hash

    async with db_factory() as session:
        stored_agent = await session.get(Agent, agent.id)
        assert stored_agent is not None
        assert stored_agent.knowledge_binding_mode == "PINNED"
        assert (await session.get(AgentVersion, version_a_id)).resolved_spec == frozen_a_spec
        assert catalog_a["catalog_id"] == "m2-catalog-a"


@pytest.mark.asyncio
async def test_latest_publish_freezes_b_then_recovery_keeps_b_after_source_drift(
    db_factory: async_sessionmaker[AsyncSession],
    catalog_dir: Path,
) -> None:
    catalog_path = catalog_dir / "latest-context-catalog.json"
    _write_catalog(
        catalog_path,
        _catalog_document(
            catalog_id="m2-catalog-a",
            source_version="2026-10-27-a",
            source_label="offline catalog A",
            total_context_tokens=128_000,
            deployment_input_limit=96_000,
            resolved_model="m2-model-a",
        ),
    )

    async with db_factory() as session:
        base = await _create_workspace(session)
        workspace = base["workspace"]
        assert isinstance(workspace, Workspace)
        knowledge = await _create_knowledge_base(
            session,
            workspace_id=workspace.id,
            label="latest-source",
        )
        context = base["context"]
        assert isinstance(context, WorkspaceExecutionContext)
        snapshot_service = KnowledgeSnapshotService()
        snapshot_a = await snapshot_service.create_current_snapshot(
            session,
            context,
            knowledge["knowledge_base"].id,
        )
        agent, version_a, knowledge_base_id = await _create_published_agent(
            session,
            base,
            knowledge,
            binding_mode="LATEST",
            snapshot_id=None,
            catalog_path=catalog_path,
            reserved_output_tokens=4_096,
        )
        assert version_a.resolved_spec["retrieval"]["knowledge_bindings"] == [
            {
                "knowledge_base_id": str(knowledge_base_id),
                "binding_mode": "LATEST",
            }
        ]
        version_a_id = version_a.id
        agent_id = agent.id

    _write_catalog(
        catalog_path,
        _catalog_document(
            catalog_id="m2-catalog-b",
            source_version="2026-10-27-b",
            source_label="offline catalog B",
            total_context_tokens=64_000,
            deployment_input_limit=48_000,
            resolved_model="m2-model-b",
        ),
    )
    async with db_factory() as session:
        base = await _create_workspace(session)
        workspace = base["workspace"]
        assert isinstance(workspace, Workspace)
        knowledge_b = await _create_knowledge_base(
            session,
            workspace_id=workspace.id,
            label="latest-b",
        )
        context_b = base["context"]
        assert isinstance(context_b, WorkspaceExecutionContext)
        snapshot_b = await KnowledgeSnapshotService().create_current_snapshot(
            session,
            context_b,
            knowledge_b["knowledge_base"].id,
        )
        _, version_b, _ = await _create_published_agent(
            session,
            base,
            knowledge_b,
            binding_mode="LATEST",
            snapshot_id=None,
            catalog_path=catalog_path,
            reserved_output_tokens=4_096,
        )
        assert (
            version_b.resolved_spec["runtime"]["context_policy"]["models"][0]["source"][
                "resolved_model"
            ]
            == "m2-model-b"
        )
        policy_b = version_b.resolved_spec["runtime"]["context_policy"]
        assert policy_b["input_limit_tokens"] == 45_600
        assert policy_b["thresholds"] == {
            "soft": 31_920,
            "hard": 38_760,
            "target": 25_080,
        }
        version_b_id = version_b.id

    _write_catalog(
        catalog_path,
        _catalog_document(
            catalog_id="m2-catalog-c",
            source_version="2026-10-27-c",
            source_label="offline catalog C",
            total_context_tokens=32_000,
            deployment_input_limit=16_000,
            resolved_model="m2-model-c",
        ),
    )
    model_factory = _NoNetworkModelFactory()
    service = AgentRunService(db_factory, model_gateway_factory=model_factory)
    recovered_a = await service.prepare_run(
        context,
        agent_version_id=version_a_id,
        input_text="recover catalog A and latest from its publication instant",
    )
    recovered_b = await service.prepare_run(
        context_b,
        agent_version_id=version_b_id,
        input_text="recover catalog B after catalog C supersedes it",
    )
    assert model_factory.calls == 0
    assert recovered_a.effective_knowledge_snapshots == [
        {
            "knowledge_base_id": str(knowledge_base_id),
            "snapshot_id": str(snapshot_a.snapshot_id),
            "snapshot_hash": snapshot_a.content_hash,
        }
    ]
    assert recovered_b.effective_knowledge_snapshots == [
        {
            "knowledge_base_id": str(knowledge_b["knowledge_base"].id),
            "snapshot_id": str(snapshot_b.snapshot_id),
            "snapshot_hash": snapshot_b.content_hash,
        }
    ]

    async with db_factory() as session:
        version_a_fresh = await session.get(AgentVersion, version_a_id)
        version_b_fresh = await session.get(AgentVersion, version_b_id)
        assert version_a_fresh is not None
        assert version_b_fresh is not None
        assert (
            version_a_fresh.resolved_spec["runtime"]["context_policy"]["models"][0]["source"][
                "resolved_model"
            ]
            == "m2-model-a"
        )
        assert (
            version_b_fresh.resolved_spec["runtime"]["context_policy"]["models"][0]["source"][
                "resolved_model"
            ]
            == "m2-model-b"
        )
        stored_agent = await session.get(Agent, agent_id)
        assert stored_agent is not None
        assert stored_agent.knowledge_binding_mode == "LATEST"
        stored_runs = list(
            (
                await session.scalars(
                    select(AgentRun)
                    .where(AgentRun.id.in_((recovered_a.id, recovered_b.id)))
                    .order_by(AgentRun.id)
                )
            ).all()
        )
        assert len(stored_runs) == 2


@pytest.mark.asyncio
async def test_pinned_snapshot_survives_revision_retirement_and_latest_advances(
    db_factory: async_sessionmaker[AsyncSession],
    catalog_dir: Path,
) -> None:
    catalog_path = catalog_dir / "pinned-latest-catalog.json"
    _write_catalog(
        catalog_path,
        _catalog_document(
            catalog_id="m2-catalog-a",
            source_version="2026-10-27-a",
            source_label="offline catalog A",
            total_context_tokens=128_000,
            deployment_input_limit=96_000,
            resolved_model="m2-model-a",
        ),
    )

    async with db_factory() as session:
        base = await _create_workspace(session)
        workspace = base["workspace"]
        assert isinstance(workspace, Workspace)
        knowledge = await _create_knowledge_base(
            session,
            workspace_id=workspace.id,
            label="snapshot-a",
        )
        context = base["context"]
        assert isinstance(context, WorkspaceExecutionContext)
        snapshot_a = await KnowledgeSnapshotService().create_current_snapshot(
            session,
            context,
            knowledge["knowledge_base"].id,
        )
        pinned_agent, pinned_version, knowledge_base_id = await _create_published_agent(
            session,
            base,
            knowledge,
            binding_mode="PINNED",
            snapshot_id=snapshot_a.snapshot_id,
            catalog_path=catalog_path,
            reserved_output_tokens=4_096,
        )
        latest_agent, latest_version_a, _ = await _create_published_agent(
            session,
            base,
            knowledge,
            binding_mode="LATEST",
            snapshot_id=None,
            catalog_path=catalog_path,
            reserved_output_tokens=4_096,
        )
        revision_a = knowledge["revision"]
        assert isinstance(revision_a, DocumentRevision)
        revision_a.lifecycle_status = RevisionLifecycleStatus.RETIRED
        revision_b = DocumentRevision(
            workspace_id=workspace.id,
            knowledge_base_id=knowledge_base_id,
            document_id=knowledge["document"].id,
            revision_number=2,
            original_filename="snapshot-a-v2.txt",
            blob_key=f"m2/{uuid4().hex}",
            media_type="text/plain",
            file_size=64,
            ingestion_status=RevisionIngestionStatus.READY,
            lifecycle_status=RevisionLifecycleStatus.ACTIVE,
        )
        session.add(revision_b)
        await session.commit()
        snapshot_b = await KnowledgeSnapshotService().create_current_snapshot(
            session,
            context,
            knowledge_base_id,
        )
        assert snapshot_a.snapshot_id != snapshot_b.snapshot_id
        _, latest_version_b, _ = await _create_published_agent(
            session,
            base,
            knowledge,
            binding_mode="LATEST",
            snapshot_id=None,
            catalog_path=catalog_path,
            reserved_output_tokens=4_096,
        )
        pinned_version_id = pinned_version.id
        latest_a_id = latest_version_a.id
        latest_b_id = latest_version_b.id
        pinned_agent_id = pinned_agent.id
        latest_agent_id = latest_agent.id

    _write_catalog(
        catalog_path,
        _catalog_document(
            catalog_id="m2-catalog-b",
            source_version="2026-10-27-b",
            source_label="offline catalog B",
            total_context_tokens=64_000,
            deployment_input_limit=48_000,
            resolved_model="m2-model-b",
        ),
    )
    model_factory = _NoNetworkModelFactory()
    service = AgentRunService(db_factory, model_gateway_factory=model_factory)
    pinned_run = await service.prepare_run(
        context,
        agent_version_id=pinned_version_id,
        input_text="recover retired revision through pinned snapshot A",
    )
    latest_a_run = await service.prepare_run(
        context,
        agent_version_id=latest_a_id,
        input_text="recover latest publication A",
    )
    latest_b_run = await service.prepare_run(
        context,
        agent_version_id=latest_b_id,
        input_text="recover latest publication B",
    )
    assert model_factory.calls == 0
    assert pinned_run.effective_knowledge_snapshots == [
        {
            "knowledge_base_id": str(knowledge_base_id),
            "snapshot_id": str(snapshot_a.snapshot_id),
            "snapshot_hash": snapshot_a.content_hash,
        }
    ]
    assert latest_a_run.effective_knowledge_snapshots == [
        {
            "knowledge_base_id": str(knowledge_base_id),
            "snapshot_id": str(snapshot_b.snapshot_id),
            "snapshot_hash": snapshot_b.content_hash,
        }
    ]
    assert latest_b_run.effective_knowledge_snapshots == latest_a_run.effective_knowledge_snapshots

    async with db_factory() as session:
        pinned_agent_fresh = await session.get(Agent, pinned_agent_id)
        latest_agent_fresh = await session.get(Agent, latest_agent_id)
        assert pinned_agent_fresh is not None
        assert latest_agent_fresh is not None
        assert pinned_agent_fresh.knowledge_binding_mode == "PINNED"
        assert latest_agent_fresh.knowledge_binding_mode == "LATEST"
        assert (
            pinned_run.resolved_spec_hash
            == (await session.get(AgentVersion, pinned_version_id)).resolved_spec_hash
        )


@pytest.mark.asyncio
async def test_recovery_rejects_rewritten_frozen_catalog_proof(
    db_factory: async_sessionmaker[AsyncSession],
    catalog_dir: Path,
) -> None:
    catalog_path = catalog_dir / "proof-catalog.json"
    _write_catalog(
        catalog_path,
        _catalog_document(
            catalog_id="m2-catalog-a",
            source_version="2026-10-27-a",
            source_label="offline catalog A",
            total_context_tokens=128_000,
            deployment_input_limit=96_000,
            resolved_model="m2-model-a",
        ),
    )
    async with db_factory() as session:
        base = await _create_workspace(session)
        workspace = base["workspace"]
        assert isinstance(workspace, Workspace)
        knowledge = await _create_knowledge_base(
            session,
            workspace_id=workspace.id,
            label="proof-tamper",
        )
        agent, version, _ = await _create_published_agent(
            session,
            base,
            knowledge,
            binding_mode="LATEST",
            snapshot_id=None,
            catalog_path=catalog_path,
            reserved_output_tokens=4_096,
        )
        version_id = version.id
        agent_id = agent.id
        original_spec = json.loads(json.dumps(version.resolved_spec))
        rewritten = json.loads(json.dumps(version.resolved_spec))
        source = rewritten["runtime"]["context_policy"]["models"][0]["source"]
        source["catalog_ref"] = "m2-catalog-forged"
        version.resolved_spec = rewritten
        version.resolved_spec_hash = canonical_json_hash(rewritten)
        await session.commit()

    model_factory = _NoNetworkModelFactory()
    async with db_factory() as session:
        stored = await session.get(AgentVersion, version_id)
        assert stored is not None
        assert stored.resolved_spec == rewritten
        assert stored.resolved_spec["model"] == original_spec["model"]
        assert (
            stored.resolved_spec["runtime"]["context_policy"]["models"][0]["source"]["catalog_ref"]
            == "m2-catalog-forged"
        )
        assert (
            original_spec["runtime"]["context_policy"]["models"][0]["source"]["catalog_ref"]
            != "m2-catalog-forged"
        )
        stored_agent = await session.get(Agent, agent_id)
        assert stored_agent is not None
    with pytest.raises(AgentHubError) as raised:
        parse_frozen_agent_spec(stored.resolved_spec, workspace_id=workspace.id)
    assert raised.value.code == "AGENT_VERSION_MODEL_BINDING_INVALID"
    assert model_factory.calls == 0
