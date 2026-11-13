"""Bind a finite interactive item to its published model and server billing evidence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.frozen import parse_frozen_agent_spec
from packages.agent_runtime.models import AgentRun, AgentVersion
from packages.core.errors.exceptions import AgentHubError
from packages.evaluation.case_budget_adapter import CaseBudgetAttemptAdapter
from packages.evaluation.case_budget_admission import (
    CaseBudgetAdmissionRejection,
    validate_interactive_case_budget_support,
)
from packages.evaluation.case_budget_ledger import CaseBudgetLedger
from packages.evaluation.case_budget_pricing import (
    BillingAttestation,
    BillingAttestationRegistry,
    FrozenProviderRoute,
    InteractiveCaseAttemptPriceSource,
    conservative_case_attempt_maximum,
)
from packages.evaluation.interactive_lifecycle import CaseExecutionIdentity
from packages.evaluation.models import (
    EvaluationDatasetItem,
    EvaluationExperimentVariant,
    PricingSnapshot,
)
from packages.evaluation.reproducibility import pricing_snapshot_content_hash
from packages.model_gateway.contracts import AliyunDeepSeekRequestMode
from packages.model_gateway.egress import ProviderEgressError
from packages.model_gateway.errors import ModelGatewayError
from packages.tools.runtime import PublishedToolCatalog


def reservation_unavailable(reason: str = "CASE_BUDGET_EXECUTOR_UNAVAILABLE") -> AgentHubError:
    return AgentHubError(
        "INTERACTIVE_COST_RESERVATION_UNAVAILABLE",
        f"The interactive case cannot obtain a cost reservation ({reason}).",
        409,
    )


@dataclass(frozen=True, slots=True)
class AttestedCaseBudget:
    limit: Decimal
    proof: BillingAttestation
    registry: BillingAttestationRegistry

    async def create_account(self, session: AsyncSession, identity: CaseExecutionIdentity) -> None:
        proof = self.proof
        account, _ = await CaseBudgetLedger().create_case_account(
            session,
            workspace_id=identity.workspace_id,
            case_id=identity.case_id,
            currency=proof.currency,
            limit_amount=self.limit,
            provider_egress_policy_version=proof.route.policy_version,
            provider_base_url=proof.route.base_url,
        )
        if (
            account.experiment_variant_id != identity.variant_id
            or account.workspace_id != proof.workspace_id
            or account.pricing_snapshot_id != proof.pricing_snapshot_id
            or account.pricing_snapshot_hash != proof.pricing_snapshot_hash
            or account.provider != proof.route.provider
            or account.model != proof.route.model
        ):
            raise reservation_unavailable("BILLING_ACCOUNT_BINDING_MISMATCH")


@dataclass(frozen=True, slots=True)
class InteractiveTurnBudget:
    session_factory: async_sessionmaker[AsyncSession]
    identity: CaseExecutionIdentity
    turn_index: int
    account: AttestedCaseBudget

    @property
    def case_id(self) -> UUID:
        return self.identity.case_id

    async def create_run_account(self, session: AsyncSession, run: AgentRun) -> None:
        await CaseBudgetLedger().create_account(
            session,
            workspace_id=self.identity.workspace_id,
            run_id=run.id,
            case_id=self.case_id,
            currency=self.account.proof.currency,
            limit_amount=self.account.limit,
        )

    def attempt_budget(
        self, run: AgentRun, model_round: int, execution_owner: str
    ) -> CaseBudgetAttemptAdapter:
        return CaseBudgetAttemptAdapter(
            self.session_factory,
            identity=self.identity,
            turn_index=self.turn_index,
            agent_run_id=run.id,
            model_round=model_round,
            execution_owner=execution_owner,
            attestation=self.account.proof,
            registry=self.account.registry,
        )


async def attest_interactive_case(
    session: AsyncSession,
    *,
    registry: BillingAttestationRegistry,
    variant: EvaluationExperimentVariant,
    item: EvaluationDatasetItem,
    evaluator_manifest: Mapping[str, object] | None,
    memory_writer: object | None,
) -> AttestedCaseBudget:
    version = await session.get(AgentVersion, variant.agent_version_id)
    price = await session.get(PricingSnapshot, variant.pricing_snapshot_id)
    if (
        version is None
        or price is None
        or variant.workspace_id != item.workspace_id
        or version.workspace_id != variant.workspace_id
        or version.resolved_spec_hash != variant.resolved_spec_hash
        or price.workspace_id != variant.workspace_id
        or price.currency != "CNY"
        or price.content_hash != variant.pricing_snapshot_hash
        or pricing_snapshot_content_hash(price) != price.content_hash
        or variant.effective_knowledge_snapshots
        or variant.effective_wiki_publications
    ):
        raise reservation_unavailable("BILLING_FROZEN_BINDING_INVALID")
    try:
        spec = parse_frozen_agent_spec(version.resolved_spec, workspace_id=variant.workspace_id)
        tools = await PublishedToolCatalog(session).list(
            workspace_id=variant.workspace_id, agent_version_id=version.id
        )
        candidate = validate_interactive_case_budget_support(
            category=item.category,
            input_contract=item.input,
            expected=item.expected,
            evaluator_manifest=evaluator_manifest,
            resolved_spec=version.resolved_spec,
            frozen_spec=spec,
            resolved_tools=tools,
            memory_writer=memory_writer,
            request_mode="generate",
        )
    except (AgentHubError, ValueError, TypeError):
        raise reservation_unavailable("BILLING_FROZEN_SPEC_INVALID") from None
    if isinstance(candidate, CaseBudgetAdmissionRejection):
        raise reservation_unavailable(candidate.code)
    try:
        route = FrozenProviderRoute.from_provider_egress(
            provider=candidate.provider,
            model=candidate.model,
            provider_egress=candidate.provider_egress,
        )
    except (AgentHubError, ValueError, TypeError):
        raise reservation_unavailable("BILLING_ROUTE_MISMATCH") from None
    match = registry.match_binding(
        workspace_id=variant.workspace_id,
        route=route,
        pricing_snapshot_id=price.id,
        pricing_snapshot_hash=price.content_hash,
    )
    proof = match.proof
    if proof is None:
        raise reservation_unavailable(match.refusal or "BILLING_PROOF_MISSING")
    if candidate.resolved_spec_hash != variant.resolved_spec_hash:
        raise reservation_unavailable("BILLING_FROZEN_BINDING_MISMATCH")
    if (
        candidate.max_output_tokens > proof.request_evidence.max_billable_output_tokens
        or proof.request_evidence.request_mode is not AliyunDeepSeekRequestMode.PROVIDER_DEFAULT
    ):
        raise reservation_unavailable("BILLING_REQUEST_ENVELOPE_MISMATCH")
    maximum = conservative_case_attempt_maximum(proof)
    if maximum is None:
        raise reservation_unavailable("BILLING_QUOTE_UNAVAILABLE")
    if maximum > candidate.max_cost_cny:
        raise reservation_unavailable("BILLING_FIRST_QUOTE_EXCEEDS_CASE_LIMIT")
    try:
        current = await InteractiveCaseAttemptPriceSource.current_binding_matches(
            session, spec.model_plan.primary, proof, candidate.provider
        )
    except (TypeError, ValueError, ProviderEgressError, ModelGatewayError):
        raise reservation_unavailable("BILLING_ROUTE_OR_PRICE_MISMATCH") from None
    if not current:
        raise reservation_unavailable("BILLING_ROUTE_OR_PRICE_MISMATCH")
    return AttestedCaseBudget(candidate.max_cost_cny, proof, registry)
