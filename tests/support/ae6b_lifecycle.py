"""Real PostgreSQL fixtures for the AE-6b interactive lifecycle tests."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.models import AgentRun, AgentVersion
from packages.agent_runtime.runtime import AgentRunService
from packages.agent_runtime.runtime_projection import AgentRunExecutionOverrides
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.evaluation.build_identity import StaticBuildIdentityProvider
from packages.evaluation.experiments import ExperimentService
from packages.evaluation.interactive_lifecycle import (
    CaseExecutionIdentity,
    InteractiveLifecycle,
)
from packages.evaluation.interactive_protocol import require_interactive_protocol
from packages.evaluation.models import (
    EvaluationDatasetItem,
    EvaluationExperimentCaseResult,
    EvaluationExperimentRun,
    EvaluationExperimentVariant,
    InteractiveEpisode,
    InteractiveEpisodeTurn,
)
from packages.evaluation.runner import ExperimentRunner
from packages.threads.models import AgentThread, ThreadTurn
from packages.threads.service import ThreadService
from tests.integration.test_m4c_agent_runtime import _seed
from tests.integration.test_m7b_experiments import (
    _manager_context,
    _pricing,
    _published_version,
)

FIXTURE_REF = "support_customer"
SEED = 6


@dataclass(frozen=True, slots=True)
class OwnedLifecycleCase:
    base: dict[str, object]
    context: WorkspaceExecutionContext
    run: EvaluationExperimentRun
    variant: EvaluationExperimentVariant
    item: EvaluationDatasetItem
    case: EvaluationExperimentCaseResult
    identity: CaseExecutionIdentity


@dataclass(frozen=True, slots=True)
class BoundEpisodeTurn:
    thread: AgentThread
    turn: ThreadTurn
    reservation: InteractiveEpisodeTurn
    run: AgentRun


@dataclass(frozen=True, slots=True)
class InteractiveExperimentSetup:
    base: dict[str, object]
    context: WorkspaceExecutionContext
    run: EvaluationExperimentRun
    variant: EvaluationExperimentVariant
    item: EvaluationDatasetItem


def interactive_item(
    case_key: str,
    *,
    ordinal: int = 0,
    assertions: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build one valid dataset item without borrowing the non-interactive helper."""

    return {
        "case_key": case_key,
        "split": "DEV",
        "category": "INTERACTIVE",
        "input": {
            "task": "Resolve the synthetic support request.",
            "environment_ref": FIXTURE_REF,
            "simulator": {
                "private_facts": {"customer_ref": "__CUSTOMER_REF__"},
                "triggers": {"customer_ref": ["customer reference"]},
                "fallback": "I do not have more information.",
                "max_turns": 3,
            },
            "max_turns": 3,
            "max_tokens": 4_096,
        },
        "expected": {
            "assertions": dict(assertions or {"ticket_count": 0}),
        },
        "tags": ["ae6b", "interactive", "synthetic"],
        "source_provenance": {
            "source_kind": "synthetic_fixture",
            "source_id": f"AE-6b:{case_key}",
        },
        "ordinal": ordinal,
    }


async def create_interactive_driver_experiment(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    assertions: Mapping[str, object] | None = None,
) -> InteractiveExperimentSetup:
    """Create one frozen interactive dataset and an unclaimed queued run."""

    async with session_factory() as session:
        base = await _seed(session, label=f"ae6b-driver-{uuid4().hex}")
        _, dataset_version = await _published_version(
            session,
            base,
            items=[
                interactive_item(
                    f"interactive-driver-{uuid4().hex}",
                    assertions=assertions,
                )
            ],
        )
        pricing = await _pricing(session, base)
        context = _manager_context(base)
        experiments = ExperimentService(StaticBuildIdentityProvider("d" * 40))
        experiment = await experiments.create_experiment(
            session,
            context=context,
            name=f"AE-6b interactive driver {uuid4().hex}",
            description=None,
            dataset_version_id=dataset_version.id,
            split="DEV",
            purpose="DEVELOPMENT",
            repetitions=1,
        )
        require_interactive_protocol(experiment.evaluator_manifest)
        variant = await experiments.add_variant(
            session,
            context=context,
            experiment_id=experiment.id,
            label="interactive",
            agent_version_id=UUID(str(base["version"].id)),
            pricing_snapshot_id=pricing.id,
            ordinal=0,
        )
        await experiments.finalize_experiment(
            session,
            context=context,
            experiment_id=experiment.id,
        )
        run, _ = await experiments.create_run(
            session,
            context=context,
            experiment_id=experiment.id,
        )
        item = await session.scalar(
            select(EvaluationDatasetItem).where(
                EvaluationDatasetItem.workspace_id == run.workspace_id,
                EvaluationDatasetItem.dataset_version_id == run.dataset_version_id,
                EvaluationDatasetItem.split == run.split,
            )
        )
        if item is None:
            raise RuntimeError("AE6B_INTERACTIVE_DATASET_ITEM_MISSING")
        return InteractiveExperimentSetup(
            base=base,
            context=context,
            run=run,
            variant=variant,
            item=item,
        )


async def create_owned_lifecycle_cases(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    workspace_count: int = 1,
    repetitions: int = 2,
) -> tuple[OwnedLifecycleCase, ...]:
    """Create genuine workspace/version/dataset/experiment/case/run records."""

    if workspace_count < 1 or repetitions < 1:
        raise ValueError("workspace_count and repetitions must be positive")

    located: list[OwnedLifecycleCase] = []
    for workspace_index in range(workspace_count):
        async with session_factory() as session:
            label = f"ae6b-lifecycle-{workspace_index}-{uuid4().hex}"
            base = await _seed(session, label=label)
            _, dataset_version = await _published_version(
                session,
                base,
                items=[interactive_item(f"interactive-{uuid4().hex}")],
            )
            pricing = await _pricing(session, base)
            context = _manager_context(base)
            experiments = ExperimentService(
                StaticBuildIdentityProvider(f"{workspace_index + 1}" * 40)
            )
            experiment = await experiments.create_experiment(
                session,
                context=context,
                name=f"AE-6b lifecycle {uuid4().hex}",
                description=None,
                dataset_version_id=dataset_version.id,
                split="DEV",
                purpose="DEVELOPMENT",
                repetitions=repetitions,
            )
            require_interactive_protocol(experiment.evaluator_manifest)
            variant = await experiments.add_variant(
                session,
                context=context,
                experiment_id=experiment.id,
                label="interactive",
                agent_version_id=UUID(str(base["version"].id)),
                pricing_snapshot_id=pricing.id,
                ordinal=0,
            )
            await experiments.finalize_experiment(
                session,
                context=context,
                experiment_id=experiment.id,
            )
            run, _ = await experiments.create_run(
                session,
                context=context,
                experiment_id=experiment.id,
            )
            runner = ExperimentRunner(session_factory)
            prepared = await runner.prepare_run(session, run_id=run.id)
            if prepared != repetitions:
                raise RuntimeError("AE6B_LIFECYCLE_CASE_PREPARE_COUNT_MISMATCH")
            owner = f"ae6b-owner-{workspace_index}-{uuid4().hex}"
            if not await runner.claim_run(
                session,
                run_id=run.id,
                owner=owner,
                lease_seconds=1_800,
            ):
                raise RuntimeError("AE6B_LIFECYCLE_RUN_CLAIM_FAILED")

            for _ in range(repetitions):
                case = await runner.claim_case(session, run_id=run.id, owner=owner)
                if case is None:
                    raise RuntimeError("AE6B_LIFECYCLE_CASE_CLAIM_FAILED")
                generation = case.lease_generation
                if generation is None:
                    raise RuntimeError("AE6B_LIFECYCLE_CASE_GENERATION_MISSING")
                identity = CaseExecutionIdentity(
                    workspace_id=case.workspace_id,
                    run_id=case.experiment_run_id,
                    case_id=case.id,
                    variant_id=case.experiment_variant_id,
                    item_id=case.dataset_item_id,
                    repetition_index=case.repetition_index,
                    owner=owner,
                    generation=generation,
                )
                located.append(
                    OwnedLifecycleCase(
                        base=base,
                        context=context,
                        run=run,
                        variant=variant,
                        item=await _required(session, EvaluationDatasetItem, case.dataset_item_id),
                        case=case,
                        identity=identity,
                    )
                )
    return tuple(located)


async def reserve_episode(
    lifecycle: InteractiveLifecycle,
    owned: OwnedLifecycleCase,
    *,
    seed: int = SEED,
) -> InteractiveEpisode:
    return await lifecycle.reserve(
        owned.identity,
        seed=seed,
        fixture_ref=FIXTURE_REF,
        input_hash=canonical_json_hash(owned.item.input),
        policy_hash=canonical_json_hash(dict(owned.item.input["simulator"]["triggers"])),
    )


async def create_bound_episode_turn(
    session_factory: async_sessionmaker[AsyncSession],
    owned: OwnedLifecycleCase,
    episode: InteractiveEpisode,
    *,
    opening: str,
    turn_index: int = 0,
) -> BoundEpisodeTurn:
    """Create and bind a real ThreadTurn and prepared AgentRun without dispatch."""

    lifecycle = InteractiveLifecycle(session_factory)
    reservation = await lifecycle.reserve_turn(
        owned.identity,
        turn_index=turn_index,
        input_hash=_text_hash(opening),
    )
    threads = ThreadService(session_factory)
    version = owned.base["version"]
    if not isinstance(version, AgentVersion):
        raise RuntimeError("AE6B_LIFECYCLE_AGENT_VERSION_MISSING")
    thread = await threads.create_thread(
        owned.context,
        agent_id=version.agent_id,
        title=f"AE-6B {owned.identity.case_id.hex}",
        thread_id=episode.reserved_thread_id,
    )
    await lifecycle.bind_thread(owned.identity, thread_id=thread.id)
    turn = await threads.open_turn(
        owned.context,
        thread.id,
        user_input=opening,
        client_token=reservation.client_token,
    )
    prepared = await AgentRunService(session_factory).prepare_run(
        owned.context,
        agent_version_id=owned.variant.agent_version_id,
        input_text=opening,
        thread_id=thread.id,
        thread_turn_id=turn.id,
        execution_overrides=AgentRunExecutionOverrides.for_evaluation(
            owned.variant.effective_knowledge_snapshots,
            wiki_publications=owned.variant.effective_wiki_publications,
        ),
    )
    await threads.attach_run(owned.context, turn_id=turn.id, run_id=prepared.id)
    await lifecycle.bind_turn(
        owned.identity,
        turn_index=turn_index,
        turn_id=turn.id,
        run_id=prepared.id,
    )
    return BoundEpisodeTurn(
        thread=thread,
        turn=turn,
        reservation=reservation,
        run=prepared,
    )


@asynccontextmanager
async def fail_customer_mutation(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    workspace_id: UUID,
    operation: str,
) -> AsyncIterator[None]:
    """Fail customer mutations only inside one workspace."""

    if operation not in {"insert", "delete"}:
        raise ValueError("operation must be insert or delete")
    if not isinstance(workspace_id, UUID):
        raise TypeError("workspace_id must be a UUID")
    suffix = uuid4().hex[:16]
    function_name = f"ae6b_fail_customer_{operation}_{suffix}"
    trigger_name = f"ae6b_fail_customer_{operation}_trigger_{suffix}"
    returned = "NEW" if operation == "insert" else "OLD"
    event = "INSERT" if operation == "insert" else "DELETE"

    async with session_factory() as session:
        await session.execute(
            text(
                f"""
                CREATE FUNCTION {function_name}() RETURNS trigger
                LANGUAGE plpgsql AS $$
                BEGIN
                  RAISE EXCEPTION 'injected customer {operation} failure';
                  RETURN {returned};
                END;
                $$
                """
            )
        )
        await session.execute(
            text(
                f"""
                CREATE TRIGGER {trigger_name}
                BEFORE {event} ON customers
                FOR EACH ROW
                WHEN ({returned}.workspace_id = '{workspace_id}'::uuid)
                EXECUTE FUNCTION {function_name}()
                """
            )
        )
        await session.commit()
    try:
        yield
    finally:
        async with session_factory() as session:
            await session.execute(text(f"DROP TRIGGER IF EXISTS {trigger_name} ON customers"))
            await session.execute(text(f"DROP FUNCTION IF EXISTS {function_name}()"))
            await session.commit()


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def _required(session: AsyncSession, model, identifier: UUID):
    row = await session.get(model, identifier)
    if row is None:
        raise RuntimeError(f"AE6B_LIFECYCLE_{model.__name__.upper()}_MISSING")
    return row


__all__ = [
    "BoundEpisodeTurn",
    "FIXTURE_REF",
    "InteractiveExperimentSetup",
    "OwnedLifecycleCase",
    "SEED",
    "create_bound_episode_turn",
    "create_interactive_driver_experiment",
    "create_owned_lifecycle_cases",
    "fail_customer_mutation",
    "interactive_item",
    "reserve_episode",
]
