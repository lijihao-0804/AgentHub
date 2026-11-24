"""Thread summary orchestration mixin and helpers extracted from runtime_graph.py."""

from __future__ import annotations

import hashlib
import logging
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from packages.agent_runtime.analysis_table import (
    analysis_turn_instruction,
)
from packages.agent_runtime.context_budget import (
    ApproxTokenEstimator,
    ContextBudgetPolicy,
)
from packages.agent_runtime.frozen import (
    FrozenAgentSpec,
    frozen_summary_request_identity,
    parse_frozen_agent_spec,
)
from packages.agent_runtime.history_planner import PlanPressure
from packages.agent_runtime.history_runtime import _namespace_tool_ids, plan_history_view
from packages.agent_runtime.kv_prefix import append_turn_reminders
from packages.agent_runtime.local_summary_experiment import LocalSummaryExperimentPermit
from packages.agent_runtime.models import AgentRun, AgentVersion, RunStep
from packages.agent_runtime.runtime_projection import (
    _RUNTIME_POLICY,
    AgentRunState,
)
from packages.agent_runtime.summary_budget_adapter import SummaryRunBudgetReserver
from packages.agent_runtime.summary_models import ThreadSummaryJob, ThreadSummaryJobSource
from packages.agent_runtime.summary_prompt import (
    SummaryInputContext,
    SummaryPromptSource,
    SummaryProtectedFact,
)
from packages.agent_runtime.summary_service import (
    FrozenHistorySummaryBindingVerifier,
    NewSummaryJob,
    NewSummarySource,
    SummaryJobService,
    SummaryServiceError,
    _SummaryBindingRejected,
    render_published_summary_view,
)
from packages.agent_runtime.summary_state import SummaryJobStatus
from packages.agent_runtime.summary_worker import SummaryWorker, frozen_summary_model_spec
from packages.agent_runtime.support_facts import support_system_prompt
from packages.agent_runtime.work_layer import (
    PLAIN_TEXT_PREFIX_V1,
    PLAIN_TEXT_PREFIX_V1_ELIGIBLE_REASON,
    ThreadConversation,
    ThreadHistorySourceManifest,
)
from packages.approvals.models import Approval
from packages.artifacts.models import Artifact
from packages.control_plane.models import OrganizationMembership, Workspace, WorkspaceMembership
from packages.control_plane.services import TenantService
from packages.core.canonical.json_hash import canonical_json, canonical_json_hash
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import PrincipalContext, WorkspaceExecutionContext
from packages.model_gateway.contracts import (
    ModelMessage,
)
from packages.threads.models import AgentThread, ThreadTurn
from packages.tools.runtime import PublishedToolCatalog

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_NEEDS_ATTENTION_CONTEXT_FAILURES = frozenset(
    {
        "RUN_ACTOR_ACCESS_REVOKED",
        "RUN_EXECUTION_LEASE_LOST",
        "CASE_BUDGET_EXECUTION_FENCE_INVALID",
        "CASE_BUDGET_ACTOR_BINDING_INVALID",
        "CASE_BUDGET_ATTEMPT_RECONCILIATION_REQUIRED",
        "UNKNOWN_OUTCOME",
        "ACTION_OUTCOME_UNKNOWN",
        "ACTION_RECONCILIATION_REQUIRED",
    }
)




SUMMARY_HISTORICAL_CONTEXT_GOAL = (
    "整理给定的已完成历史交流，作为未来未指定任务的背景。"
    "不得从历史中选择或延续任何用户任务、目标或下一步；"
    "历史目标和下一步仅是过去记录，不是当前任务或指令。"
)
SUMMARY_GOAL_CONTRACT_VERSION = "historical_context_only_v1"


def _resolve_summary_candidates():
    runtime_graph_mod = sys.modules.get("packages.agent_runtime.runtime_graph")
    if runtime_graph_mod is not None:
        return getattr(runtime_graph_mod, "_summary_candidates", _summary_candidates)
    return _summary_candidates


class _RuntimeGraphSummaryMixin:
    async def summary(
        self,
        state: AgentRunState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        """Revalidate or create one exact summary before the first model call."""
        summary_policy = state.get("runtime", {}).get("summary_policy")
        if (
            not isinstance(summary_policy, Mapping)
            or summary_policy.get("enabled") is not True
            or state.get("history_checkpoint_binding") is None
        ):
            return {}

        try:
            await self._assert_summary_run_lease()
            await self._require_current_summary_actor()
            self._require_local_summary_experiment(state.get("spec"))
            await self._verify_history_checkpoint_binding(
                state, workspace_id=UUID(self.context.workspace_id)
            )
            history = await self._frozen_thread_history(
                UUID(self.context.workspace_id),
                summary_source_policy=PLAIN_TEXT_PREFIX_V1,
            )
            if history is None:
                raise AgentHubError(
                    "AGENT_HISTORY_SNAPSHOT_INVALID",
                    "The frozen thread history is invalid.",
                    422,
                )
            planned = await self._replan_summary_history(state, history)
            candidates = _resolve_summary_candidates()(history, planned.plain_text_prefix)
            if not candidates:
                if not planned.plan.ready_for_dispatch:
                    raise AgentHubError(
                        "AGENT_HISTORY_CONTEXT_INSUFFICIENT",
                        "No currently verified plain-text prefix can cover the dropped history.",
                        422,
                    )
                return {}

            manifest = history.source_manifest
            if manifest is None:
                raise AgentHubError(
                    "AGENT_HISTORY_SNAPSHOT_INVALID", "The frozen source manifest is missing.", 422
                )
            permission_snapshot = _permission_snapshot(self.context)
            permission_hash = hashlib.sha256(permission_snapshot.encode("utf-8")).hexdigest()
            source_scope = _summary_source_scope(
                self.run,
                state["spec"],
                manifest,
                local_summary_experiment=self.service.local_summary_experiment,
            )
            request_identity = frozen_summary_request_identity(summary_policy)
            model_spec = frozen_summary_model_spec(
                state["spec"].model_plan.primary,
                request_strategy=request_identity,
            )
            job_service = self._summary_job_service(state)
            revision = await job_service.find_verified_summary_prefix(
                workspace_id=UUID(self.context.workspace_id),
                thread_id=UUID(str(self.run.thread_id)),
                sources=candidates,
                policy_version=str(summary_policy["policy_version"]),
                prompt_version=str(summary_policy["prompt_version"]),
                tokenizer_id=str(summary_policy["tokenizer_id"]),
                tokenizer_version=str(summary_policy["tokenizer_version"]),
                model_spec=model_spec,
                permission_snapshot_hash=permission_hash,
                source_scope=source_scope,
                request_mode=request_identity["request_mode"],
            )

            triggered = not planned.plan.ready_for_dispatch or planned.plan.pre_trim_pressure in {
                PlanPressure.SOFT,
                PlanPressure.HARD,
            }
            if revision is None and triggered:
                if self.service.model_attempt_budget_factory is not None:
                    revision = await self._create_or_recover_summary(
                        state=state,
                        config=config,
                        history=history,
                        planned=planned,
                        candidates=candidates,
                        source_scope=source_scope,
                        permission_snapshot=permission_snapshot,
                        permission_hash=permission_hash,
                        model_spec=model_spec,
                        job_service=job_service,
                    )

            if revision is None:
                if not planned.plan.ready_for_dispatch:
                    raise AgentHubError(
                        "AGENT_HISTORY_CONTEXT_INSUFFICIENT",
                        "A validated summary revision is required to admit this history.",
                        422,
                    )
                return {}

            await self._assert_summary_run_lease()
            await self._require_current_summary_actor()
            update = self._apply_summary_revision(
                state=state,
                history=history,
                planned=planned,
                candidates=candidates,
                revision=revision,
                summary_policy=summary_policy,
                permission_snapshot=permission_snapshot,
                permission_hash=permission_hash,
                model_spec=model_spec,
            )
            await self.step(
                "SUMMARY",
                "SUCCEEDED",
                {
                    "summary_revision_hash": update["summary_revision_binding"]["revision_hash"],
                    "summary_source_count": len(update["summary_revision_binding"]["source_ids"]),
                    "summary_context_tokens_after": update["summary_context_tokens_after"],
                },
            )
            return update
        except AgentHubError as error:
            await self.step("SUMMARY", "FAILED", {"error_code": error.code})
            if error.code == "RUN_EXECUTION_LEASE_LOST":
                return {
                    "failure_code": "RUN_EXECUTION_LEASE_LOST",
                    "run_status": "NEEDS_ATTENTION",
                }
            if error.code in {
                "RUN_ACTOR_ACCESS_REVOKED",
                "SUMMARY_CURRENT_PERMISSION_CHANGED",
                "FORBIDDEN",
                "SUMMARY_EXPERIMENT_SCOPE_REJECTED",
            }:
                return {
                    "failure_code": error.code,
                    "run_status": "NEEDS_ATTENTION",
                }
            return {
                "failure_code": (
                    "AGENT_HISTORY_CONTEXT_INSUFFICIENT"
                    if error.code not in {"FORBIDDEN", "AGENT_HISTORY_SNAPSHOT_INVALID"}
                    else error.code
                )
            }
        except SummaryServiceError as error:
            logger.info("agent_summary_not_available", extra={"error_code": error.code})
            await self.step("SUMMARY", "FAILED", {"error_code": error.code})
            if error.code == "RUN_EXECUTION_LEASE_LOST":
                return {
                    "failure_code": "RUN_EXECUTION_LEASE_LOST",
                    "run_status": "NEEDS_ATTENTION",
                }
            if error.code in {
                "RUN_ACTOR_ACCESS_REVOKED",
                "SUMMARY_CURRENT_PERMISSION_CHANGED",
                "SUMMARY_TOOL_HISTORY_PERMISSION_REVOKED",
                "SUMMARY_ARTIFACT_PERMISSION_REVOKED",
                "SUMMARY_EVIDENCE_PERMISSION_REVOKED",
            }:
                return {
                    "failure_code": "RUN_ACTOR_ACCESS_REVOKED",
                    "run_status": "NEEDS_ATTENTION",
                }
            if error.code == "SUMMARY_UNKNOWN_OUTCOME":
                return {
                    "failure_code": "SUMMARY_DISPATCH_OUTCOME_UNKNOWN",
                    "run_status": "NEEDS_ATTENTION",
                }
            if state.get("summary_plan_blocked"):
                return {"failure_code": "AGENT_HISTORY_CONTEXT_INSUFFICIENT"}
            return {}
        except SQLAlchemyError:
            logger.warning("agent_summary_database_failed", exc_info=True)
            await self.step("SUMMARY", "FAILED", {"error_code": "SUMMARY_DATABASE_FAILURE"})
            if state.get("summary_plan_blocked"):
                return {"failure_code": "AGENT_HISTORY_CONTEXT_INSUFFICIENT"}
            return {}
        except Exception:
            logger.warning("agent_summary_failed", exc_info=True)
            await self.step("SUMMARY", "FAILED", {"error_code": "SUMMARY_EXECUTION_FAILED"})
            if state.get("summary_plan_blocked"):
                return {"failure_code": "AGENT_HISTORY_CONTEXT_INSUFFICIENT"}
            return {}

    async def _assert_summary_run_lease(
        self,
        *,
        session=None,
        run_row: AgentRun | None = None,
        lock: bool = False,
    ) -> None:
        """Require this graph's lease token, never an arbitrary stored owner."""
        if run_row is None:
            if session is None:
                async with self.service.session_factory() as owned_session:
                    return await self._assert_summary_run_lease(
                        session=owned_session,
                        lock=lock,
                    )
            statement = select(AgentRun).where(
                AgentRun.workspace_id == UUID(self.context.workspace_id),
                AgentRun.id == self.run.id,
            )
            if lock:
                statement = statement.with_for_update().execution_options(populate_existing=True)
            run_row = await session.scalar(statement)

        token = self.execution_owner_token
        expiry = getattr(run_row, "execution_lease_expires_at", None)
        now = datetime.now(UTC)
        if (
            not isinstance(token, str)
            or not token
            or run_row is None
            or run_row.status != "RUNNING"
            or getattr(run_row, "execution_owner", None) != token
            or expiry is None
            or (expiry if expiry.tzinfo else expiry.replace(tzinfo=UTC)) <= now
        ):
            raise AgentHubError(
                "RUN_EXECUTION_LEASE_LOST",
                "The Run execution lease is not active for this executor.",
                409,
            )

    async def _require_current_summary_actor(self) -> None:
        resolver = getattr(self.service, "_authoritative_run_context", None)
        if resolver is None:
            raise AgentHubError("FORBIDDEN", "Current Run permissions cannot be revalidated.", 403)
        current = await resolver(self.context, self.run)
        if (
            current.user_id != self.context.user_id
            or current.permissions != self.context.permissions
        ):
            raise AgentHubError("FORBIDDEN", "The Run permissions changed.", 403)

    async def _replan_summary_history(self, state: AgentRunState, history: ThreadConversation):
        memory_count = int(state.get("memory_message_count", 0))
        memory_messages = state["messages"][2 : 2 + memory_count]
        mandatory = (
            state["messages"][0],
            state["messages"][1],
            ModelMessage("user", self.run.input_text),
        )
        return plan_history_view(
            policy=ContextBudgetPolicy(state["spec"]),
            conversation=history,
            mandatory_messages=mandatory,
            memory_messages=memory_messages,
            tools=self._model_tool_definitions(state["tool_definitions"]),
            permission_snapshot=_permission_snapshot(self.context),
            return_blocked_plan=True,
            summary_source_policy=PLAIN_TEXT_PREFIX_V1,
        )

    def _summary_job_service(self, state: AgentRunState) -> SummaryJobService:
        async def load_context(session, binding):
            return await self._load_summary_validation_context(session, binding, state)

        budget_reserver = (
            SummaryRunBudgetReserver()
            if self.service.model_attempt_budget_factory is not None
            else None
        )
        return SummaryJobService(
            self.service.session_factory,
            checkpoint_adapter=self.service.checkpoint_adapter,
            verifier=FrozenHistorySummaryBindingVerifier(load_context),
            budget_reserver=budget_reserver,
            local_summary_experiment=self.service.local_summary_experiment,
            current_run_id=self.run.id,
        )

    def _require_local_summary_experiment(self, spec: FrozenAgentSpec | None) -> None:
        if spec is None or not isinstance(getattr(spec, "runtime", None), Mapping):
            return
        summary_policy = spec.runtime.get("summary_policy")
        if not isinstance(summary_policy, Mapping):
            return
        identity = frozen_summary_request_identity(summary_policy)
        if identity["request_mode"] != "non_thinking":
            return
        permit = self.service.local_summary_experiment
        if permit is None or not permit.authorizes_run(
            session_factory=self.service.session_factory,
            workspace_id=UUID(self.context.workspace_id),
            thread_id=getattr(self.run, "thread_id", None),
            run_id=self.run.id,
            agent_version_id=self.run.agent_version_id,
            resolved_spec_hash=getattr(self.run, "resolved_spec_hash", None),
            spec=spec,
        ):
            raise AgentHubError(
                "SUMMARY_EXPERIMENT_SCOPE_REJECTED",
                "This frozen summary strategy is available only in its bound local experiment.",
                403,
            )

    async def _load_summary_validation_context(self, session, binding, state):
        """Rebuild job sources and current permissions for dispatch/recovery gates."""
        persisted = await session.scalar(
            select(AgentRun).where(
                AgentRun.workspace_id == binding.workspace_id,
                AgentRun.id == binding.trigger_run_id,
            )
        )
        version = await session.scalar(
            select(AgentVersion).where(
                AgentVersion.workspace_id == binding.workspace_id,
                AgentVersion.id == getattr(persisted, "agent_version_id", None),
            )
        )
        if (
            persisted is None
            or version is None
            or persisted.thread_id != binding.thread_id
            or persisted.created_by != binding.created_by
            or persisted.agent_version_id != self.run.agent_version_id
            or persisted.resolved_spec_hash != version.resolved_spec_hash
            or version.resolved_spec_hash != self.run.resolved_spec_hash
            or canonical_json_hash(version.resolved_spec) != version.resolved_spec_hash
            or version.spec_schema_version != version.resolved_spec.get("spec_schema_version")
        ):
            raise _SummaryBindingRejected("SUMMARY_TRIGGER_RUN_BINDING_CHANGED")

        resolver = getattr(self.service, "_authoritative_run_context", None)
        if resolver is None:
            raise _SummaryBindingRejected("SUMMARY_CURRENT_PERMISSION_UNAVAILABLE")
        current_context = await resolver(self.context, persisted)
        if (
            current_context.user_id != str(persisted.created_by)
            or current_context.user_id != self.context.user_id
            or current_context.permissions != self.context.permissions
        ):
            raise _SummaryBindingRejected("SUMMARY_CURRENT_PERMISSION_CHANGED")

        frozen_spec = parse_frozen_agent_spec(
            version.resolved_spec, workspace_id=binding.workspace_id
        )
        summary_policy = frozen_spec.runtime.get("summary_policy")
        if (
            not isinstance(summary_policy, Mapping)
            or summary_policy.get("enabled") is not True
            or summary_policy.get("policy_version") != binding.policy_version
            or summary_policy.get("prompt_version") != binding.prompt_version
        ):
            raise _SummaryBindingRejected("SUMMARY_FROZEN_POLICY_CHANGED")
        try:
            request_identity = frozen_summary_request_identity(summary_policy)
            expected_model_spec = frozen_summary_model_spec(
                frozen_spec.model_plan.primary,
                request_strategy=request_identity,
            )
        except (AgentHubError, TypeError, ValueError):
            raise _SummaryBindingRejected("SUMMARY_FROZEN_POLICY_CHANGED") from None
        if canonical_json_hash(expected_model_spec) != binding.model_spec_hash:
            raise _SummaryBindingRejected("SUMMARY_FROZEN_MODEL_REQUEST_CHANGED")

        snapshot = persisted.effective_thread_history_snapshot
        if not isinstance(snapshot, Mapping) or not isinstance(snapshot.get("source"), Mapping):
            raise _SummaryBindingRejected("SUMMARY_FROZEN_SOURCE_MISSING")
        try:
            manifest = ThreadHistorySourceManifest.from_manifest(snapshot["source"])
        except (TypeError, ValueError):
            raise _SummaryBindingRejected("SUMMARY_FROZEN_SOURCE_INVALID") from None
        if (
            manifest.scope.workspace_id != binding.workspace_id
            or manifest.scope.thread_id != binding.thread_id
            or manifest.scope.before_run_id != binding.trigger_run_id
            or manifest.scope.before_sequence != binding.source_watermark
            or not isinstance(snapshot.get("plan"), Mapping)
        ):
            raise _SummaryBindingRejected("SUMMARY_FROZEN_SOURCE_SCOPE_CHANGED")

        locked_runs, current_context = await self._lock_summary_validation_records(
            session,
            binding=binding,
            manifest=manifest,
            trigger_run=persisted,
        )
        persisted = locked_runs.get(binding.trigger_run_id)
        if persisted is None:
            raise _SummaryBindingRejected("SUMMARY_TRIGGER_RUN_BINDING_CHANGED")
        if (
            persisted.thread_id != binding.thread_id
            or persisted.created_by != binding.created_by
            or persisted.agent_version_id != version.id
            or persisted.resolved_spec_hash != version.resolved_spec_hash
            or persisted.resolved_spec_hash != self.run.resolved_spec_hash
            or canonical_json_hash(version.resolved_spec) != version.resolved_spec_hash
            or version.spec_schema_version != version.resolved_spec.get("spec_schema_version")
            or persisted.effective_thread_history_snapshot != snapshot
        ):
            raise _SummaryBindingRejected("SUMMARY_TRIGGER_RUN_BINDING_CHANGED")

        expected_scope = _summary_source_scope(
            persisted,
            frozen_spec,
            manifest,
            local_summary_experiment=self.service.local_summary_experiment,
        )
        if canonical_json_hash(expected_scope) != canonical_json_hash(dict(binding.source_scope)):
            raise _SummaryBindingRejected("SUMMARY_SOURCE_SCOPE_CHANGED")

        provider = self.service.thread_context_provider
        if provider is None or not callable(getattr(provider, "replay_history_sources", None)):
            raise _SummaryBindingRejected("SUMMARY_SOURCE_PROVIDER_UNAVAILABLE")
        try:
            conversation = await provider.replay_history_sources(
                manifest=snapshot["source"],
                summary_source_policy=PLAIN_TEXT_PREFIX_V1,
            )
        except Exception:
            raise _SummaryBindingRejected("SUMMARY_SOURCE_REPLAY_FAILED") from None
        try:
            await self._assert_summary_run_lease(run_row=locked_runs.get(self.run.id))
        except AgentHubError as error:
            raise _SummaryBindingRejected(error.code) from None
        self._authorize_history_for_current_actor(conversation)
        permission_snapshot = _permission_snapshot(current_context)
        definitions = await PublishedToolCatalog(session).list(
            workspace_id=binding.workspace_id,
            agent_version_id=version.id,
        )
        ordered_definitions = tuple(sorted(definitions, key=lambda definition: definition.identity))
        summary_reminders = [
            rule
            for rule in (analysis_turn_instruction(frozen_spec.runtime, persisted.input_text),)
            if rule is not None
        ]
        planned = plan_history_view(
            policy=ContextBudgetPolicy(frozen_spec),
            conversation=conversation,
            mandatory_messages=(
                ModelMessage("system", _RUNTIME_POLICY),
                ModelMessage(
                    "system",
                    support_system_prompt(
                        frozen_spec.system_prompt, frozen_spec.runtime, ordered_definitions
                    ),
                ),
                ModelMessage(
                    "user", append_turn_reminders(persisted.input_text, summary_reminders)
                ),
            ),
            memory_messages=(),
            tools=self._model_tool_definitions(
                {definition.identity: definition for definition in definitions}
            ),
            permission_snapshot=permission_snapshot,
            return_blocked_plan=True,
            summary_source_policy=PLAIN_TEXT_PREFIX_V1,
        )
        candidates = _summary_candidates(conversation, planned.plain_text_prefix)
        job_sources = tuple(
            await session.scalars(
                select(ThreadSummaryJobSource)
                .where(
                    ThreadSummaryJobSource.workspace_id == binding.workspace_id,
                    ThreadSummaryJobSource.thread_id == binding.thread_id,
                    ThreadSummaryJobSource.job_id == binding.job_id,
                )
                .order_by(ThreadSummaryJobSource.ordinal)
            )
        )
        if (
            not job_sources
            or len(job_sources) > len(candidates)
            or tuple(source.source_id for source in job_sources)
            != tuple(source.source_id for source in candidates[: len(job_sources)])
            or any(
                not _summary_source_row_matches(candidate, stored)
                for candidate, stored in zip(candidates, job_sources, strict=True)
            )
        ):
            raise _SummaryBindingRejected("SUMMARY_CURRENT_SOURCE_PREFIX_CHANGED")

        job = await session.scalar(
            select(ThreadSummaryJob).where(
                ThreadSummaryJob.workspace_id == binding.workspace_id,
                ThreadSummaryJob.thread_id == binding.thread_id,
                ThreadSummaryJob.id == binding.job_id,
            )
        )
        if job is None:
            raise _SummaryBindingRejected("SUMMARY_JOB_SOURCE_MISSING")
        try:
            job_request_identity = frozen_summary_request_identity(job.policy_spec)
        except (AgentHubError, TypeError, ValueError):
            raise _SummaryBindingRejected("SUMMARY_JOB_REQUEST_STRATEGY_INVALID") from None
        if (
            job_request_identity != request_identity
            or dict(job.model_spec) != expected_model_spec
            or canonical_json_hash(dict(job.model_spec)) != binding.model_spec_hash
        ):
            raise _SummaryBindingRejected("SUMMARY_JOB_REQUEST_STRATEGY_CHANGED")
        try:
            context = _build_summary_input_context(
                conversation=conversation,
                sources=candidates[: len(job_sources)],
                task_goal=SUMMARY_HISTORICAL_CONTEXT_GOAL,
                permission_snapshot=permission_snapshot,
                final_view_budget_tokens=_summary_final_view_budget(frozen_spec),
                target_budget_tokens=job.target_budget_tokens,
            )
        except (TypeError, ValueError, AgentHubError):
            raise _SummaryBindingRejected("SUMMARY_CURRENT_SOURCE_CONTEXT_INVALID") from None
        return context

    async def _lock_summary_validation_records(
        self,
        session,
        *,
        binding,
        manifest: ThreadHistorySourceManifest,
        trigger_run: AgentRun,
    ) -> tuple[dict[UUID, AgentRun], WorkspaceExecutionContext]:
        """Lock mutable authorization/source rows through the DB dispatch fence.

        Summary operations hold their job row first.  This loader then locks
        current/trigger/source Runs by UUID, membership rows, the Thread, its
        selected Turns and Artifacts, then existing Approval and blocking step
        rows.  Provider replay uses a separate read-only REPEATABLE READ
        session while these rows are locked; its SELECTs do not wait on the
        caller's locks.  The outer transaction commits at mark_dispatched,
        before the network request begins.
        """
        sources = manifest.selected_sources
        run_ids = {
            self.run.id,
            trigger_run.id,
            *(source.run_id for source in sources),
        }
        run_rows = tuple(
            await session.scalars(
                select(AgentRun)
                .where(
                    AgentRun.workspace_id == binding.workspace_id,
                    AgentRun.id.in_(run_ids),
                )
                .order_by(AgentRun.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        locked_runs = {row.id: row for row in run_rows}
        if set(locked_runs) != run_ids:
            raise _SummaryBindingRejected("SUMMARY_FROZEN_SOURCE_CHANGED")
        try:
            await self._assert_summary_run_lease(run_row=locked_runs.get(self.run.id))
        except AgentHubError as error:
            raise _SummaryBindingRejected(error.code) from None

        workspace = await session.scalar(
            select(Workspace).where(Workspace.id == binding.workspace_id).with_for_update()
        )
        if workspace is None:
            raise _SummaryBindingRejected("RUN_ACTOR_ACCESS_REVOKED")
        organization_membership = await session.scalar(
            select(OrganizationMembership)
            .where(
                OrganizationMembership.organization_id == workspace.organization_id,
                OrganizationMembership.user_id == trigger_run.created_by,
            )
            .with_for_update()
        )
        if organization_membership is None:
            raise _SummaryBindingRejected("RUN_ACTOR_ACCESS_REVOKED")
        await session.scalar(
            select(WorkspaceMembership)
            .where(
                WorkspaceMembership.workspace_id == binding.workspace_id,
                WorkspaceMembership.user_id == trigger_run.created_by,
            )
            .with_for_update()
        )

        principal = PrincipalContext(
            request_id=self.context.request_id,
            trace_id=self.context.organization.principal.trace_id,
            user_id=str(trigger_run.created_by),
        )
        try:
            current_context = (
                await TenantService().get_workspace_access(
                    session,
                    principal=principal,
                    workspace_id=binding.workspace_id,
                )
            ).context
        except AgentHubError as error:
            if error.status_code in {403, 404}:
                raise _SummaryBindingRejected("RUN_ACTOR_ACCESS_REVOKED") from None
            raise _SummaryBindingRejected("SUMMARY_CURRENT_PERMISSION_UNAVAILABLE") from None
        if (
            current_context.user_id != str(trigger_run.created_by)
            or current_context.user_id != self.context.user_id
        ):
            raise _SummaryBindingRejected("RUN_ACTOR_ACCESS_REVOKED")
        if current_context.permissions != self.context.permissions:
            raise _SummaryBindingRejected("SUMMARY_CURRENT_PERMISSION_CHANGED")

        thread_row = await session.scalar(
            select(AgentThread.id)
            .where(
                AgentThread.workspace_id == binding.workspace_id,
                AgentThread.id == binding.thread_id,
            )
            .with_for_update()
        )
        if thread_row is None:
            raise _SummaryBindingRejected("SUMMARY_FROZEN_SOURCE_CHANGED")
        turn_ids = tuple(sorted(source.turn_id for source in sources))
        if turn_ids:
            locked_turn_ids = set(
                await session.scalars(
                    select(ThreadTurn.id)
                    .where(
                        ThreadTurn.workspace_id == binding.workspace_id,
                        ThreadTurn.thread_id == binding.thread_id,
                        ThreadTurn.id.in_(turn_ids),
                    )
                    .order_by(ThreadTurn.id)
                    .with_for_update()
                )
            )
            if locked_turn_ids != set(turn_ids):
                raise _SummaryBindingRejected("SUMMARY_FROZEN_SOURCE_CHANGED")

        artifact_ids = tuple(
            sorted(
                {
                    artifact.artifact_id
                    for source in sources
                    for artifact in source.artifact_identities
                }
            )
        )
        if artifact_ids:
            locked_artifact_ids = set(
                await session.scalars(
                    select(Artifact.id)
                    .where(
                        Artifact.workspace_id == binding.workspace_id,
                        Artifact.thread_id == binding.thread_id,
                        Artifact.id.in_(artifact_ids),
                    )
                    .order_by(Artifact.id)
                    .with_for_update()
                )
            )
            if locked_artifact_ids != set(artifact_ids):
                raise _SummaryBindingRejected("SUMMARY_FROZEN_SOURCE_CHANGED")

        source_run_ids = tuple(sorted({source.run_id for source in sources}))
        if source_run_ids:
            # Existing rows are locked against removal or mutation.  New rows
            # require a key-share lock on the already locked parent Run.
            tuple(
                await session.scalars(
                    select(Approval.id)
                    .where(
                        Approval.workspace_id == binding.workspace_id,
                        Approval.run_id.in_(source_run_ids),
                    )
                    .order_by(Approval.run_id, Approval.id)
                    .with_for_update()
                )
            )
            tuple(
                await session.scalars(
                    select(RunStep.id)
                    .where(
                        RunStep.workspace_id == binding.workspace_id,
                        RunStep.agent_run_id.in_(source_run_ids),
                        RunStep.kind.not_in({"PREPARE", "MODEL", "GUARD", "FINISH"}),
                    )
                    .order_by(RunStep.agent_run_id, RunStep.sequence_number, RunStep.id)
                    .with_for_update()
                )
            )
        return locked_runs, current_context

    def _authorize_history_for_current_actor(self, conversation: ThreadConversation) -> None:
        if (
            any(message.role == "tool" for turn in conversation.turns for message in turn.messages)
            and "tool_run" not in self.context.permissions
        ):
            raise _SummaryBindingRejected("SUMMARY_TOOL_HISTORY_PERMISSION_REVOKED")
        if any(turn.artifact_ids for turn in conversation.turns) and (
            "workspace_read" not in self.context.permissions
        ):
            raise _SummaryBindingRejected("SUMMARY_ARTIFACT_PERMISSION_REVOKED")
        if any(turn.evidence_registry for turn in conversation.turns) and (
            "knowledge_run" not in self.context.permissions
        ):
            raise _SummaryBindingRejected("SUMMARY_EVIDENCE_PERMISSION_REVOKED")

    async def _create_or_recover_summary(
        self,
        *,
        state: AgentRunState,
        config: Mapping[str, Any] | None,
        history: ThreadConversation,
        planned,
        candidates: tuple[NewSummarySource, ...],
        source_scope: Mapping[str, Any],
        permission_snapshot: str,
        permission_hash: str,
        model_spec: Mapping[str, Any],
        job_service: SummaryJobService,
    ):
        attempt_factory = self.service.model_attempt_budget_factory
        if attempt_factory is None or self.service.checkpoint_adapter is None:
            return None
        next_round = int(state.get("model_round_count", 0)) + 1
        attempt_budget = attempt_factory(self.run, next_round)
        if (
            not all(
                callable(getattr(attempt_budget, name, None))
                for name in ("reserve_attempt", "mark_unknown_outcome", "is_bound_to_run")
            )
            or attempt_budget.is_bound_to_run(
                UUID(self.context.workspace_id), self.run.id, f"round-{next_round}"
            )
            is not True
        ):
            return None

        adapter = self.service.checkpoint_adapter
        configurable = config.get("configurable") if isinstance(config, Mapping) else None
        expected_config = (
            adapter.config_for_run(self.run.workspace_id, self.run.id)
            if adapter is not None and callable(getattr(adapter, "config_for_run", None))
            else None
        )
        expected_configurable = (
            expected_config.get("configurable") if isinstance(expected_config, Mapping) else None
        )
        expected_checkpoint_ns = (
            expected_configurable.get("checkpoint_ns")
            if isinstance(expected_configurable, Mapping)
            else None
        )
        checkpoint_map = (
            configurable.get("checkpoint_map") if isinstance(configurable, Mapping) else None
        )
        # LangGraph injects the running node's task namespace and leaves its
        # checkpoint_id unset. The checkpoint_map selects the parent's exact
        # checkpoint by the graph's root namespace; that is the frozen summary
        # boundary verified by SummaryJobService.create_job.
        checkpoint_id = (
            checkpoint_map.get(expected_checkpoint_ns)
            if isinstance(checkpoint_map, Mapping) and isinstance(expected_checkpoint_ns, str)
            else None
        )
        checkpoint_ns = expected_checkpoint_ns
        if (
            not isinstance(configurable, Mapping)
            or not isinstance(expected_configurable, Mapping)
            or configurable.get("thread_id") != expected_configurable.get("thread_id")
            or not isinstance(checkpoint_map, Mapping)
            or not isinstance(checkpoint_id, str)
            or not checkpoint_id
            or not isinstance(checkpoint_ns, str)
        ):
            return None

        summary_policy = state["runtime"]["summary_policy"]
        request_identity = frozen_summary_request_identity(summary_policy)
        context = _build_summary_input_context(
            conversation=history,
            sources=candidates,
            task_goal=SUMMARY_HISTORICAL_CONTEXT_GOAL,
            permission_snapshot=permission_snapshot,
            final_view_budget_tokens=_summary_final_view_budget(state["spec"]),
            target_budget_tokens=_summary_target_budget(
                history,
                candidates,
                permission_snapshot=permission_snapshot,
                task_goal=SUMMARY_HISTORICAL_CONTEXT_GOAL,
                final_view_budget_tokens=_summary_final_view_budget(state["spec"]),
                target_percent=int(summary_policy["target_percent"]),
            ),
        )
        job_policy = {
            **dict(summary_policy),
            **request_identity,
        }
        command = NewSummaryJob(
            workspace_id=UUID(self.context.workspace_id),
            thread_id=UUID(str(self.run.thread_id)),
            trigger_run_id=self.run.id,
            source_watermark=history.source_manifest.scope.before_sequence,
            source_scope=dict(source_scope),
            policy_spec=job_policy,
            policy_version=str(summary_policy["policy_version"]),
            prompt_version=str(summary_policy["prompt_version"]),
            budget_version="summary-budget-v1",
            model_spec=dict(model_spec),
            tokenizer_id=str(summary_policy["tokenizer_id"]),
            tokenizer_version=str(summary_policy["tokenizer_version"]),
            target_budget_tokens=context.target_budget_tokens,
            permission_snapshot_hash=permission_hash,
            created_by=UUID(self.context.user_id),
            sources=candidates,
            parent_artifact_id=None,
            parent_revision_hash=None,
            checkpoint_id=checkpoint_id,
            checkpoint_ns=checkpoint_ns,
            model_round=next_round,
        )
        created = await job_service.create_job(command)
        settlement = await job_service.get_settlement(created.key)
        claimed = None
        if settlement.status == SummaryJobStatus.QUEUED.value:
            claimed = await job_service.claim(created.key)
            if claimed is not None:
                try:
                    price_id = await job_service.resolve_applicable_pricing(
                        workspace_id=created.key.workspace_id,
                        provider=str(model_spec["provider"]),
                        model=str(model_spec["model"]),
                        effective_before=datetime.now(UTC),
                    )
                except Exception:
                    await job_service.fail_before_dispatch(
                        created.key,
                        lease_token=claimed.lease_token,
                        failure_code="SUMMARY_PRICING_UNAVAILABLE",
                    )
                    raise

                async def prepare_gateway(context_value, plan, request):
                    async with self.service.session_factory() as session:
                        gateway = self.service.model_gateway_factory(session)
                        prepare = getattr(gateway, "prepare_resolved", None)
                        if prepare is None:
                            if frozen_summary_request_identity(summary_policy)[
                                "request_mode"
                            ] == "non_thinking":
                                raise SummaryServiceError(
                                    "SUMMARY_EXPERIMENT_GATEWAY_UNVERIFIABLE",
                                    "A local v2 experiment requires detached "
                                    "credential-route verification.",
                                )
                            return gateway
                        prepared = await prepare(
                            context_value,
                            plan,
                            request,
                            operation="generate",
                        )
                        is_non_thinking = (
                            frozen_summary_request_identity(summary_policy)["request_mode"]
                            == "non_thinking"
                        )
                        experiment = self.service.local_summary_experiment
                        if is_non_thinking and not experiment.authorizes_prepared_gateway(
                            prepared, profile_id=plan.primary.id
                        ):
                            raise SummaryServiceError(
                                "SUMMARY_EXPERIMENT_ROUTE_MISMATCH",
                                "The resolved credential endpoint differs from the local permit.",
                            )
                        return prepared

                async with self.service.session_factory() as session:
                    gateway = self.service.model_gateway_factory(session)
                worker = SummaryWorker(
                    job_service,
                    gateway,
                    gateway_preparer=prepare_gateway,
                    request_counter=self.service.summary_request_counter,
                )

                async def verify_summary_network_boundary() -> None:
                    await self._require_current_summary_actor()
                    await self._assert_summary_run_lease(lock=True)

                async def publish_validated_summary(key, lease_token):
                    return await job_service.publish(
                        key,
                        lease_token=lease_token,
                        published_by=f"run:{self.run.id}",
                    )

                try:
                    settlement = await worker.run_claimed(
                        claimed=claimed,
                        context=self.context,
                        frozen_spec=state["spec"],
                        pricing_snapshot_id=price_id,
                        before_network_call=verify_summary_network_boundary,
                        on_validated=publish_validated_summary,
                    )
                except Exception as error:
                    settlement = await job_service.get_settlement(created.key)
                    safe_pre_dispatch_rejections = {
                        "RUN_EXECUTION_LEASE_LOST",
                        "RUN_ACTOR_ACCESS_REVOKED",
                        "SUMMARY_CURRENT_PERMISSION_CHANGED",
                        "SUMMARY_TOOL_HISTORY_PERMISSION_REVOKED",
                        "SUMMARY_ARTIFACT_PERMISSION_REVOKED",
                        "SUMMARY_EVIDENCE_PERMISSION_REVOKED",
                    }
                    if (
                        isinstance(error, SummaryServiceError)
                        and error.code in safe_pre_dispatch_rejections
                    ):
                        if settlement.status == SummaryJobStatus.CLAIMED.value:
                            await job_service.fail_before_dispatch(
                                created.key,
                                lease_token=claimed.lease_token,
                                failure_code=error.code,
                            )
                        # DISPATCH_INTENT is deliberately left fenced. The
                        # worker raised before its network call, so no retry is
                        # allowed and recovery will reconcile the durable intent.
                        raise
                    if settlement.status == SummaryJobStatus.CLAIMED.value:
                        await job_service.fail_before_dispatch(
                            created.key,
                            lease_token=claimed.lease_token,
                            failure_code="SUMMARY_PRE_DISPATCH_FAILED",
                        )
                        settlement = await job_service.get_settlement(created.key)
                    elif settlement.status == SummaryJobStatus.DISPATCH_INTENT.value:
                        try:
                            settlement = await job_service.mark_dispatch_unknown(
                                created.key,
                                lease_token=claimed.lease_token,
                            )
                        except Exception:
                            settlement = await job_service.get_settlement(created.key)
                    elif settlement.status == SummaryJobStatus.RESPONSE_STORED.value:
                        settlement = await worker.validate_stored(
                            created.key,
                            on_validated=publish_validated_summary,
                        )
                # store_provider_response releases the dispatch token. The
                # worker validates and publishes under a fresh local recovery
                # lease; never pass the spent dispatch token to publication.
                claimed = None
        elif settlement.status in {
            SummaryJobStatus.RESPONSE_STORED.value,
            SummaryJobStatus.VALIDATED.value,
        }:
            claimed = await job_service.claim_recovery(created.key)
            if claimed is not None:
                if settlement.status == SummaryJobStatus.RESPONSE_STORED.value:
                    settlement = await job_service.validate_stored_response(
                        created.key,
                        lease_token=claimed.lease_token,
                    )

        if settlement.status == SummaryJobStatus.UNKNOWN_OUTCOME.value:
            raise SummaryServiceError(
                "SUMMARY_UNKNOWN_OUTCOME",
                "The summary dispatch outcome is unknown and cannot be retried.",
            )
        if settlement.status == SummaryJobStatus.DISPATCH_INTENT.value:
            raise SummaryServiceError(
                "SUMMARY_UNKNOWN_OUTCOME",
                "A prior summary dispatch has no confirmed response.",
            )
        if settlement.status == SummaryJobStatus.VALIDATED.value and claimed is not None:
            await job_service.publish(
                created.key,
                lease_token=claimed.lease_token,
                published_by=f"run:{self.run.id}",
            )
        if settlement.status not in {
            SummaryJobStatus.VALIDATED.value,
            SummaryJobStatus.PUBLISHED.value,
        }:
            return None

        # A persisted response can be recovered locally after a crash. Its
        # current permission, source proof and exact checkpoint were rechecked
        # by validate_stored_response/publish before this lookup.
        return await job_service.find_verified_summary_prefix(
            workspace_id=UUID(self.context.workspace_id),
            thread_id=UUID(str(self.run.thread_id)),
            sources=candidates,
            policy_version=str(summary_policy["policy_version"]),
            prompt_version=str(summary_policy["prompt_version"]),
            tokenizer_id=str(summary_policy["tokenizer_id"]),
            tokenizer_version=str(summary_policy["tokenizer_version"]),
            model_spec=model_spec,
            permission_snapshot_hash=permission_hash,
            source_scope=source_scope,
            request_mode=request_identity["request_mode"],
        )

    def _apply_summary_revision(
        self,
        *,
        state: AgentRunState,
        history: ThreadConversation,
        planned,
        candidates: tuple[NewSummarySource, ...],
        revision,
        summary_policy: Mapping[str, Any],
        permission_snapshot: str,
        permission_hash: str,
        model_spec: Mapping[str, Any],
    ) -> dict[str, Any]:
        covered_ids = tuple(str(entry.get("source_id", "")) for entry in revision.source_index)
        if not covered_ids or covered_ids != tuple(
            source.source_id for source in candidates[: len(covered_ids)]
        ):
            raise AgentHubError(
                "AGENT_HISTORY_CONTEXT_INSUFFICIENT",
                "The fixed summary revision does not cover a current source prefix.",
                422,
            )
        context = _build_summary_input_context(
            conversation=history,
            sources=candidates[: len(covered_ids)],
            task_goal=SUMMARY_HISTORICAL_CONTEXT_GOAL,
            permission_snapshot=permission_snapshot,
            final_view_budget_tokens=_summary_final_view_budget(state["spec"]),
            target_budget_tokens=_summary_target_budget(
                history,
                candidates[: len(covered_ids)],
                permission_snapshot=permission_snapshot,
                task_goal=SUMMARY_HISTORICAL_CONTEXT_GOAL,
                final_view_budget_tokens=_summary_final_view_budget(state["spec"]),
                target_percent=int(summary_policy["target_percent"]),
            ),
        )
        summary_message = ModelMessage(
            "assistant",
            render_published_summary_view(revision, context),
        )
        summary_exchange_id = f"summary:{revision.artifact_id}:{revision.revision_hash}"
        turns_by_source = {
            source.source_id: turn
            for source, turn in zip(
                history.source_manifest.selected_sources,
                history.turns,
                strict=True,
            )
        }
        decisions = {item.group_id: item for item in planned.plan.decisions}
        raw_messages: list[ModelMessage] = []
        exchange_ids: list[str] = []
        for source in history.source_manifest.selected_sources:
            turn = turns_by_source[source.source_id]
            if source.source_id in covered_ids:
                continue
            decision = decisions.get(source.source_id)
            if decision is None or not decision.retained:
                raise AgentHubError(
                    "AGENT_HISTORY_CONTEXT_INSUFFICIENT",
                    "The summary leaves historical sources that the planner would drop.",
                    422,
                )
            pair = _thread_turn_messages(turn)
            raw_messages.extend(pair)
            exchange_ids.extend([source.source_id] * len(pair))

        recent_ids = {source.source_id for source in history.source_manifest.selected_sources[-2:]}
        if not recent_ids.issubset(set(exchange_ids)):
            raise AgentHubError(
                "AGENT_HISTORY_CONTEXT_INSUFFICIENT",
                "The last two complete historical turns were not retained.",
                422,
            )
        memory_count = int(state.get("memory_message_count", 0))
        original_messages = state["messages"]
        prefix_messages = original_messages[: 2 + memory_count]
        messages = [
            *prefix_messages,
            summary_message,
            *raw_messages,
            ModelMessage("user", self.run.input_text),
        ]
        updated_exchange_ids = [summary_exchange_id, *exchange_ids]
        candidate_state = dict(state)
        candidate_state.update(
            {
                "messages": messages,
                "history_message_count": len(updated_exchange_ids),
                "history_exchange_ids": updated_exchange_ids,
                "summary_plan_blocked": False,
            }
        )
        admission = self._admit_context(candidate_state)
        if (
            admission.messages != tuple(messages)
            or admission.usage.truncated
            or admission.usage.dropped_exchange_count
        ):
            raise AgentHubError(
                "AGENT_HISTORY_CONTEXT_INSUFFICIENT",
                "The final model context cannot admit the fixed summary and complete tail.",
                422,
            )
        binding = {
            "job_id": str(revision.key.job_id),
            "artifact_id": str(revision.artifact_id),
            "revision_hash": revision.revision_hash,
            "content_hash": revision.content_hash,
            "source_ids": list(covered_ids),
            "permission_snapshot_hash": permission_hash,
            "model_spec_hash": canonical_json_hash(dict(model_spec)),
            "policy_version": revision.policy_version,
            "prompt_version": revision.prompt_version,
            "request_mode": frozen_summary_request_identity(summary_policy)["request_mode"],
            "exchange_id": summary_exchange_id,
        }
        return {
            **candidate_state,
            "summary_revision_binding": binding,
            "summary_context_tokens_after": admission.usage.estimated_input_after,
        }

    async def _revalidate_summary_revision(self, state: AgentRunState) -> None:
        """Reprove permissions, source prefix and exact revision before every model call."""
        self._require_local_summary_experiment(state["spec"])
        binding = state.get("summary_revision_binding")
        if not isinstance(binding, Mapping):
            raise AgentHubError(
                "AGENT_HISTORY_SNAPSHOT_INVALID", "The summary revision binding is invalid.", 422
            )
        await self._require_current_summary_actor()
        history = await self._frozen_thread_history(
            UUID(self.context.workspace_id),
            summary_source_policy=PLAIN_TEXT_PREFIX_V1,
        )
        if history is None:
            raise AgentHubError(
                "AGENT_HISTORY_SNAPSHOT_INVALID", "The frozen thread history is invalid.", 422
            )
        planned = await self._replan_summary_history(state, history)
        candidates = _summary_candidates(history, planned.plain_text_prefix)
        if not candidates:
            raise AgentHubError(
                "AGENT_HISTORY_CONTEXT_INSUFFICIENT", "The summary prefix lost eligibility.", 422
            )
        permission_snapshot = _permission_snapshot(self.context)
        permission_hash = hashlib.sha256(permission_snapshot.encode("utf-8")).hexdigest()
        if binding.get("permission_snapshot_hash") != permission_hash:
            raise AgentHubError("FORBIDDEN", "Summary permission binding changed.", 403)
        source_scope = _summary_source_scope(
            self.run,
            state["spec"],
            history.source_manifest,
            local_summary_experiment=self.service.local_summary_experiment,
        )
        request_identity = frozen_summary_request_identity(state["runtime"]["summary_policy"])
        model_spec = frozen_summary_model_spec(
            state["spec"].model_plan.primary,
            request_strategy=request_identity,
        )
        if binding.get("model_spec_hash") != canonical_json_hash(dict(model_spec)):
            raise AgentHubError(
                "AGENT_HISTORY_SNAPSHOT_INVALID", "The summary model binding changed.", 422
            )
        policy = state["runtime"]["summary_policy"]
        job_service = self._summary_job_service(state)
        revision = await job_service.find_verified_summary_prefix(
            workspace_id=UUID(self.context.workspace_id),
            thread_id=UUID(str(self.run.thread_id)),
            sources=candidates,
            policy_version=str(policy["policy_version"]),
            prompt_version=str(policy["prompt_version"]),
            tokenizer_id=str(policy["tokenizer_id"]),
            tokenizer_version=str(policy["tokenizer_version"]),
            model_spec=model_spec,
            permission_snapshot_hash=permission_hash,
            source_scope=source_scope,
            request_mode=request_identity["request_mode"],
        )
        expected_ids = tuple(str(value) for value in binding.get("source_ids", ()))
        if (
            revision is None
            or str(revision.key.job_id) != binding.get("job_id")
            or str(revision.artifact_id) != binding.get("artifact_id")
            or revision.revision_hash != binding.get("revision_hash")
            or revision.content_hash != binding.get("content_hash")
            or revision.policy_version != binding.get("policy_version")
            or revision.prompt_version != binding.get("prompt_version")
            or tuple(str(item.get("source_id", "")) for item in revision.source_index)
            != expected_ids
            or binding.get("request_mode") != request_identity["request_mode"]
        ):
            raise AgentHubError(
                "AGENT_HISTORY_SNAPSHOT_INVALID",
                "The exact summary revision no longer matches current history.",
                422,
            )
        expected = self._apply_summary_revision(
            state=state,
            history=history,
            planned=planned,
            candidates=candidates,
            revision=revision,
            summary_policy=policy,
            permission_snapshot=permission_snapshot,
            permission_hash=permission_hash,
            model_spec=model_spec,
        )
        memory_count = int(state.get("memory_message_count", 0))
        history_start = 2 + memory_count
        history_count = int(state.get("history_message_count", 0))
        if tuple(state.get("history_exchange_ids", ())) != tuple(
            expected.get("history_exchange_ids", ())
        ) or tuple(state["messages"][history_start : history_start + history_count]) != tuple(
            expected["messages"][history_start : history_start + history_count]
        ):
            raise AgentHubError(
                "AGENT_HISTORY_SNAPSHOT_INVALID",
                "The checkpoint does not contain the exact validated summary view.",
                422,
            )



def _permission_snapshot(context: WorkspaceExecutionContext) -> str:
    return f"user:{context.user_id};permissions:" + ",".join(sorted(context.permissions))


def _summary_source_scope(
    run: AgentRun,
    spec: FrozenAgentSpec,
    manifest,
    *,
    local_summary_experiment: LocalSummaryExperimentPermit | None = None,
) -> dict[str, Any]:
    policy = spec.runtime.get("summary_policy")
    if not isinstance(policy, Mapping) or policy.get("enabled") is not True:
        raise ValueError("summary policy is not explicitly enabled")
    resolved_spec_hash = getattr(run, "resolved_spec_hash", None)
    if not isinstance(resolved_spec_hash, str) or len(resolved_spec_hash) != 64:
        raise ValueError("the Run has no frozen resolved spec hash")
    result = {
        "version": "thread_summary_runtime_scope_v2",
        "summary_source_policy": PLAIN_TEXT_PREFIX_V1,
        "agent_version_id": str(run.agent_version_id),
        "resolved_spec_hash": resolved_spec_hash,
        "summary_policy": dict(policy),
        "summary_goal_contract": {
            "version": SUMMARY_GOAL_CONTRACT_VERSION,
            "goal_scope": "HISTORICAL_BACKGROUND_ONLY",
            "goal_is_current_instruction": False,
            "current_user_task_in_summary_request": False,
        },
        **frozen_summary_request_identity(policy),
        # This exact Run watermark is checked during dispatch. The stable-scope
        # comparator omits it when looking for a reusable cross-Run revision.
        "source_manifest": {"manifest_sha256": manifest.manifest_sha256},
    }
    request_identity = frozen_summary_request_identity(policy)
    if request_identity["request_mode"] == "non_thinking":
        if local_summary_experiment is None:
            raise ValueError("non-thinking summary requires its local experiment permit")
        result["local_summary_experiment"] = local_summary_experiment.source_scope_payload()
    return result


def _summary_candidates(history: ThreadConversation, prefix) -> tuple[NewSummarySource, ...]:
    manifest = history.source_manifest
    if (
        manifest is None
        or prefix is None
        or prefix.policy != PLAIN_TEXT_PREFIX_V1
        or len(manifest.selected_sources) != len(history.turns)
    ):
        return ()
    identities = {source.source_id: source for source in manifest.selected_sources}
    turns = {f"thread-turn:{turn.turn_id}": turn for turn in history.turns}
    result: list[NewSummarySource] = []
    for selected in prefix.sources:
        identity = identities.get(selected.source_id)
        turn = turns.get(selected.source_id)
        proof = getattr(turn, "plain_text_prefix_v1_proof", None)
        if (
            identity is None
            or turn is None
            or proof is None
            or str(proof.status) != "ELIGIBLE"
            or proof.reason != PLAIN_TEXT_PREFIX_V1_ELIGIBLE_REASON
            or selected.turn_id != identity.turn_id
            or selected.run_id != identity.run_id
            or selected.sequence != identity.sequence
            or selected.revision_sha256 != identity.revision_sha256
        ):
            return ()
        artifact_identities = tuple(item.to_manifest() for item in identity.artifact_identities)
        stable_identity_hash = canonical_json_hash(
            {
                "version": "plain_text_prefix_source_identity_v1",
                "source_id": identity.source_id,
                "workspace_id": str(identity.workspace_id),
                "thread_id": str(identity.thread_id),
                "turn_id": str(identity.turn_id),
                "run_id": str(identity.run_id),
                "sequence": identity.sequence,
                "original_ref": identity.original_ref,
                "user_input_sha256": identity.user_input_sha256,
                "final_output_sha256": identity.final_output_sha256,
                "artifact_identities": list(artifact_identities),
            }
        )
        result.append(
            NewSummarySource(
                source_id=identity.source_id,
                turn_id=identity.turn_id,
                run_id=identity.run_id,
                sequence=identity.sequence,
                original_ref=identity.original_ref,
                source_identity_hash=stable_identity_hash,
                revision_sha256=identity.revision_sha256,
                user_input_sha256=identity.user_input_sha256,
                final_output_sha256=identity.final_output_sha256,
                summary_eligibility="ELIGIBLE",
                eligibility_reason=proof.reason,
                artifact_identities=artifact_identities,
            )
        )
    return tuple(result)


def _summary_source_row_matches(
    candidate: NewSummarySource, stored: ThreadSummaryJobSource
) -> bool:
    return (
        stored.source_id == candidate.source_id
        and stored.turn_id == candidate.turn_id
        and stored.run_id == candidate.run_id
        and stored.sequence == candidate.sequence
        and stored.original_ref == candidate.original_ref
        and stored.source_identity_hash == candidate.source_identity_hash
        and stored.revision_sha256 == candidate.revision_sha256
        and stored.user_input_sha256 == candidate.user_input_sha256
        and stored.final_output_sha256 == candidate.final_output_sha256
        and list(stored.artifact_identities or [])
        == [dict(item) for item in candidate.artifact_identities]
        and stored.summary_eligibility == candidate.summary_eligibility
        and stored.eligibility_reason == candidate.eligibility_reason
    )


def _summary_final_view_budget(spec: FrozenAgentSpec) -> int:
    policy = ContextBudgetPolicy(spec).context_policy
    if not isinstance(policy, Mapping):
        raise ValueError("frozen context policy is unavailable")
    value = policy.get("model_available_input_tokens")
    if type(value) is not int or value <= 1:
        raise ValueError("frozen model input budget is unavailable")
    return value


def _build_summary_input_context(
    *,
    conversation: ThreadConversation,
    sources: tuple[NewSummarySource, ...] | list[NewSummarySource],
    task_goal: str,
    permission_snapshot: str,
    final_view_budget_tokens: int,
    target_budget_tokens: int,
) -> SummaryInputContext:
    manifest = conversation.source_manifest
    if manifest is None or not sources:
        raise ValueError("summary context requires a fixed non-empty source prefix")
    identities = {item.source_id: item for item in manifest.selected_sources}
    turns = {f"thread-turn:{turn.turn_id}": turn for turn in conversation.turns}
    prompt_sources: list[SummaryPromptSource] = []
    protected: list[SummaryProtectedFact] = []
    for source in sources:
        identity = identities.get(source.source_id)
        turn = turns.get(source.source_id)
        if identity is None or turn is None:
            raise ValueError("summary source is outside the frozen conversation")
        proof = getattr(turn, "plain_text_prefix_v1_proof", None)
        if (
            proof is None
            or str(proof.status) != "ELIGIBLE"
            or proof.reason != PLAIN_TEXT_PREFIX_V1_ELIGIBLE_REASON
        ):
            raise ValueError("summary source no longer has current eligibility proof")
        content = f"User: {turn.user_input}\nAssistant: {turn.final_output}"
        prompt_sources.append(
            SummaryPromptSource(
                source_id=source.source_id,
                workspace_id=str(identity.workspace_id),
                thread_id=str(identity.thread_id),
                identity=source.source_identity_hash,
                revision=source.revision_sha256,
                sequence=source.sequence,
                watermark=manifest.scope.before_sequence,
                role="user",
                speaker="user",
                occurred_at=f"sequence:{source.sequence}",
                content=content,
                original_ref=source.original_ref,
                summary_eligible=True,
                exchange_complete=True,
            )
        )
        protected.append(
            SummaryProtectedFact(
                fact_ref=f"original-user-text:{source.source_id}",
                text=turn.user_input,
                source_ids=(source.source_id,),
            )
        )
    budget_payload = {
        "task_goal": task_goal,
        "sources": [
            {"source_id": item.source_id, "content": item.content} for item in prompt_sources
        ],
        "protected_facts": [item.payload() for item in protected],
    }
    source_budget_tokens = ApproxTokenEstimator().estimate(canonical_json(budget_payload))
    if (
        type(target_budget_tokens) is not int
        or type(final_view_budget_tokens) is not int
        or target_budget_tokens >= source_budget_tokens
        or final_view_budget_tokens <= target_budget_tokens
    ):
        raise ValueError("summary source, target and final-view budgets are inconsistent")
    return SummaryInputContext(
        workspace_id=str(manifest.scope.workspace_id),
        thread_id=str(manifest.scope.thread_id),
        permission_snapshot=permission_snapshot,
        task_goal=task_goal,
        prefix_first_sequence=sources[0].sequence,
        prefix_last_sequence=sources[-1].sequence,
        watermark=manifest.scope.before_sequence,
        sources=tuple(prompt_sources),
        protected_facts=tuple(protected),
        completed_actions=(),
        required_references=(),
        source_budget_tokens=source_budget_tokens,
        target_budget_tokens=target_budget_tokens,
        final_view_budget_tokens=final_view_budget_tokens,
        prefix_complete=True,
        protected_manifest_complete=True,
        action_manifest_complete=True,
        reference_manifest_complete=True,
    )


def _summary_target_budget(
    history: ThreadConversation,
    sources: tuple[NewSummarySource, ...],
    *,
    permission_snapshot: str,
    task_goal: str,
    final_view_budget_tokens: int,
    target_percent: int,
) -> int:
    if type(target_percent) is not int or not 1 <= target_percent < 100:
        raise ValueError("summary target percentage is invalid")
    source_budget = _build_summary_input_context(
        conversation=history,
        sources=sources,
        task_goal=task_goal,
        permission_snapshot=permission_snapshot,
        final_view_budget_tokens=final_view_budget_tokens,
        target_budget_tokens=1,
    ).source_budget_tokens
    if source_budget <= 1 or final_view_budget_tokens <= 1:
        raise ValueError("summary source budget is too small")
    return max(
        1,
        min(
            source_budget * target_percent // 100,
            source_budget - 1,
            final_view_budget_tokens - 1,
        ),
    )


def _thread_turn_messages(turn) -> tuple[ModelMessage, ...]:
    if turn.messages:
        return _namespace_tool_ids(turn.turn_id.hex, turn.messages)
    assistant_content = turn.final_output
    if turn.artifact_refs:
        assistant_content = "\n".join((assistant_content, *turn.artifact_refs))
    return (
        ModelMessage("user", turn.user_input),
        ModelMessage("assistant", assistant_content),
    )
