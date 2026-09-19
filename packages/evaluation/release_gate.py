"""Immutable, explicit release-gate policy and decision evaluation."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.control_plane.rbac import EVALUATION_READ, EVALUATION_RUN
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.evaluation.metrics import EvaluatorRegistry, MetricDirection
from packages.evaluation.models import (
    EvaluationExperimentComparison,
    EvaluationExperimentRun,
    EvaluationReleaseGateDecision,
    EvaluationReleaseGatePolicy,
)

_RULES = frozenset(
    {
        "NO_REGRESSION",
        "MAX_ABSOLUTE_REGRESSION",
        "MAX_RELATIVE_REGRESSION",
        "MIN_VALUE",
        "MAX_VALUE",
        "TRADEOFF",
    }
)
_GUARD_RULES = frozenset({"NO_REGRESSION", "MIN_VALUE", "MAX_VALUE"})
_BASE_RULE_KEYS = frozenset({"metric", "rule", "required", "safety"})
_TOLERANCE_RULES = frozenset({"MAX_ABSOLUTE_REGRESSION", "MAX_RELATIVE_REGRESSION", "TRADEOFF"})


class EvaluationReleaseGateService:
    def __init__(self, registry: EvaluatorRegistry | None = None) -> None:
        self.registry = registry or EvaluatorRegistry()

    async def create_policy(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
        name: str,
        description: str | None,
        policy_json: Mapping[str, Any],
    ) -> EvaluationReleaseGatePolicy:
        self._require_mutation(context)
        workspace_id = self._workspace_id(context)
        normalized = self._validate_policy(policy_json)
        policy_hash = canonical_json_hash(normalized)
        existing = await session.scalar(
            select(EvaluationReleaseGatePolicy).where(
                EvaluationReleaseGatePolicy.workspace_id == workspace_id,
                EvaluationReleaseGatePolicy.name == name,
            )
        )
        if existing is not None:
            self._verify_policy(existing)
            if existing.policy_hash != policy_hash:
                raise AgentHubError(
                    "RELEASE_GATE_POLICY_CONFLICT",
                    "A policy with this name already exists.",
                    409,
                ) from None
            return existing
        policy = EvaluationReleaseGatePolicy(
            workspace_id=workspace_id,
            name=name,
            description=description,
            policy_json=normalized,
            policy_hash=policy_hash,
            created_by=self._user_id(context),
        )
        session.add(policy)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            existing = await session.scalar(
                select(EvaluationReleaseGatePolicy).where(
                    EvaluationReleaseGatePolicy.workspace_id == workspace_id,
                    EvaluationReleaseGatePolicy.name == name,
                )
            )
            if existing is None:
                raise
            self._verify_policy(existing)
            if existing.policy_hash != policy_hash:
                raise AgentHubError(
                    "RELEASE_GATE_POLICY_CONFLICT",
                    "A policy with this name already exists.",
                    409,
                ) from None
            return existing
        return policy

    async def list_policies(
        self, session: AsyncSession, *, context: WorkspaceExecutionContext
    ) -> list[EvaluationReleaseGatePolicy]:
        self._require_read(context)
        workspace_id = self._workspace_id(context)
        policies = list(
            await session.scalars(
                select(EvaluationReleaseGatePolicy)
                .where(EvaluationReleaseGatePolicy.workspace_id == workspace_id)
                .order_by(EvaluationReleaseGatePolicy.created_at, EvaluationReleaseGatePolicy.id)
            )
        )
        for policy in policies:
            self._verify_policy(policy)
        return policies

    async def get_policy(
        self, session: AsyncSession, *, context: WorkspaceExecutionContext, policy_id: UUID
    ) -> EvaluationReleaseGatePolicy:
        self._require_read(context)
        policy = await self._load_policy(session, self._workspace_id(context), policy_id)
        self._verify_policy(policy)
        return policy

    async def create_decision(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
        run_id: UUID,
        comparison_id: UUID,
        policy_id: UUID,
    ) -> EvaluationReleaseGateDecision:
        self._require_mutation(context)
        workspace_id = self._workspace_id(context)
        run = await session.scalar(
            select(EvaluationExperimentRun).where(
                EvaluationExperimentRun.workspace_id == workspace_id,
                EvaluationExperimentRun.id == run_id,
            )
        )
        comparison = await session.scalar(
            select(EvaluationExperimentComparison).where(
                EvaluationExperimentComparison.workspace_id == workspace_id,
                EvaluationExperimentComparison.id == comparison_id,
                EvaluationExperimentComparison.experiment_run_id == run_id,
            )
        )
        policy = await self._load_policy(session, workspace_id, policy_id)
        if run is None or comparison is None:
            raise AgentHubError(
                "EVALUATION_COMPARISON_NOT_FOUND", "The comparison was not found.", 404
            )
        self._verify_comparison(comparison)
        self._verify_policy(policy)
        if run.purpose != "RELEASE_GATE" or run.split != "HOLDOUT":
            raise AgentHubError(
                "RELEASE_GATE_REQUIRES_HOLDOUT",
                "Release gates require a HOLDOUT RELEASE_GATE run.",
                422,
            )
        existing = await session.scalar(
            select(EvaluationReleaseGateDecision).where(
                EvaluationReleaseGateDecision.workspace_id == workspace_id,
                EvaluationReleaseGateDecision.comparison_id == comparison_id,
                EvaluationReleaseGateDecision.policy_id == policy_id,
            )
        )
        if existing is not None:
            self._verify_decision(existing, comparison, policy)
            return existing

        status, rule_results, reasons = self._evaluate(comparison, policy.policy_json)
        decision_hash = canonical_json_hash(
            {
                "comparison_hash": comparison.comparison_hash,
                "policy_hash": policy.policy_hash,
                "status": status,
                "rule_results": rule_results,
                "reasons": reasons,
            }
        )
        decision = EvaluationReleaseGateDecision(
            workspace_id=workspace_id,
            comparison_id=comparison_id,
            policy_id=policy_id,
            status=status,
            rule_results=rule_results,
            reasons=reasons,
            comparison_hash=comparison.comparison_hash,
            policy_hash=policy.policy_hash,
            decision_hash=decision_hash,
            created_by=self._user_id(context),
        )
        session.add(decision)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            existing = await session.scalar(
                select(EvaluationReleaseGateDecision).where(
                    EvaluationReleaseGateDecision.workspace_id == workspace_id,
                    EvaluationReleaseGateDecision.comparison_id == comparison_id,
                    EvaluationReleaseGateDecision.policy_id == policy_id,
                )
            )
            if existing is None:
                raise
            self._verify_decision(existing, comparison, policy)
            return existing
        return decision

    async def list_decisions(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
        run_id: UUID,
        comparison_id: UUID,
    ) -> list[EvaluationReleaseGateDecision]:
        self._require_read(context)
        workspace_id = self._workspace_id(context)
        comparison = await self._load_comparison(session, workspace_id, run_id, comparison_id)
        decisions = list(
            await session.scalars(
                select(EvaluationReleaseGateDecision)
                .where(
                    EvaluationReleaseGateDecision.workspace_id == workspace_id,
                    EvaluationReleaseGateDecision.comparison_id == comparison_id,
                )
                .order_by(EvaluationReleaseGateDecision.created_at)
            )
        )
        for decision in decisions:
            policy = await self._load_policy(session, workspace_id, decision.policy_id)
            self._verify_decision(decision, comparison, policy)
        return decisions

    async def get_decision(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
        run_id: UUID,
        comparison_id: UUID,
        policy_id: UUID,
    ) -> EvaluationReleaseGateDecision:
        self._require_read(context)
        workspace_id = self._workspace_id(context)
        comparison = await self._load_comparison(session, workspace_id, run_id, comparison_id)
        policy = await self._load_policy(session, workspace_id, policy_id)
        decision = await session.scalar(
            select(EvaluationReleaseGateDecision).where(
                EvaluationReleaseGateDecision.workspace_id == workspace_id,
                EvaluationReleaseGateDecision.comparison_id == comparison_id,
                EvaluationReleaseGateDecision.policy_id == policy_id,
            )
        )
        if decision is None:
            raise AgentHubError(
                "RELEASE_GATE_DECISION_NOT_FOUND", "The gate decision was not found.", 404
            )
        self._verify_decision(decision, comparison, policy)
        return decision

    def _validate_policy(self, policy: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(policy, Mapping) or set(policy) != {"rules"}:
            raise AgentHubError(
                "INVALID_RELEASE_GATE_POLICY", "The release gate policy is invalid.", 422
            )
        rules = policy["rules"]
        if not isinstance(rules, list) or not rules:
            raise AgentHubError(
                "INVALID_RELEASE_GATE_POLICY", "The release gate policy is invalid.", 422
            )
        normalized = []
        seen: set[str] = set()
        for raw in rules:
            if not isinstance(raw, Mapping):
                self._invalid_policy()
            metric = raw.get("metric")
            rule = raw.get("rule")
            if not isinstance(metric, str) or metric in seen or not isinstance(rule, str):
                self._invalid_policy()
            if rule not in _RULES:
                self._invalid_policy()
            allowed = _BASE_RULE_KEYS | ({"tolerance"} if rule in _TOLERANCE_RULES else set())
            if rule in {"MIN_VALUE", "MAX_VALUE"}:
                allowed.add("threshold")
            if rule == "TRADEOFF":
                allowed |= {"guard_metric", "guard_rule", "guard_threshold"}
            if not set(raw).issubset(allowed):
                self._invalid_policy()
            try:
                self.registry.definition_for(metric)
            except ValueError:
                self._invalid_policy()
            seen.add(metric)
            item = {
                "metric": metric,
                "rule": rule,
                "required": raw.get("required", True),
                "safety": raw.get("safety", False),
            }
            if not isinstance(item["required"], bool) or not isinstance(item["safety"], bool):
                self._invalid_policy()
            for key in ("tolerance", "threshold", "guard_threshold"):
                if key in raw:
                    value = raw[key]
                    if (
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(float(value))
                    ):
                        self._invalid_policy()
                    if key == "tolerance" and value < 0:
                        self._invalid_policy()
                    item[key] = value
            if rule in _TOLERANCE_RULES and "tolerance" not in item:
                self._invalid_policy()
            if rule in {"MIN_VALUE", "MAX_VALUE"} and "threshold" not in item:
                self._invalid_policy()
            if rule == "TRADEOFF":
                guard_metric = raw.get("guard_metric")
                guard_rule = raw.get("guard_rule")
                if (
                    guard_metric == metric
                    or not isinstance(guard_metric, str)
                    or guard_rule not in _GUARD_RULES
                ):
                    self._invalid_policy()
                try:
                    self.registry.definition_for(guard_metric)
                except ValueError:
                    self._invalid_policy()
                item.update({"guard_metric": guard_metric, "guard_rule": guard_rule})
                if guard_rule in {"MIN_VALUE", "MAX_VALUE"} and "guard_threshold" not in item:
                    self._invalid_policy()
            normalized.append(item)
        return {"rules": normalized}

    def _evaluate(
        self, comparison: EvaluationExperimentComparison, policy: Mapping[str, Any]
    ) -> tuple[str, list[dict[str, Any]], list[str]]:
        if comparison.status == "NOT_COMPARABLE":
            return "INCONCLUSIVE", [], ["comparison_not_comparable"]
        rule_results: list[dict[str, Any]] = []
        reasons: list[str] = []
        hard_fail = False
        inconclusive = False
        for rule in policy["rules"]:
            metric = comparison.metrics.get(rule["metric"])
            result = self._evaluate_rule(rule, metric)
            if rule["rule"] == "TRADEOFF" and result["status"] == "PASS":
                guard_rule = {
                    "metric": rule["guard_metric"],
                    "rule": rule["guard_rule"],
                    "threshold": rule.get("guard_threshold"),
                }
                guard_result = self._evaluate_rule(
                    guard_rule, comparison.metrics.get(rule["guard_metric"])
                )
                result["guard"] = guard_result
                if guard_result["status"] != "PASS":
                    result["status"] = guard_result["status"]
                    result["reason"] = "guard_metric_not_satisfied"
            rule_results.append(result)
            if result["status"] == "FAIL":
                if rule.get("safety", False):
                    hard_fail = True
                elif rule.get("required", True):
                    hard_fail = True
                reasons.append(f"{rule['metric']}:{result['status']}")
            elif result["status"] == "INCONCLUSIVE" and rule.get("required", True):
                inconclusive = True
                reasons.append(f"{rule['metric']}:{result['status']}")
        if comparison.status == "INCOMPLETE" and any(
            item.get("required", True) for item in policy["rules"]
        ):
            inconclusive = True
            reasons.append("comparison_incomplete")
        return (
            ("FAIL" if hard_fail else "INCONCLUSIVE" if inconclusive else "PASS"),
            rule_results,
            reasons,
        )

    def _evaluate_rule(self, rule: Mapping[str, Any], metric: Any) -> dict[str, Any]:
        if not isinstance(metric, Mapping):
            return {
                "metric": rule["metric"],
                "rule": rule["rule"],
                "status": "INCONCLUSIVE",
                "reason": "missing_metric",
            }
        baseline = metric.get("baseline", {})
        candidate = metric.get("candidate", {})
        if (
            metric.get("status") not in {None, "COMPLETE"}
            or baseline.get("status") != "AVAILABLE"
            or candidate.get("status") != "AVAILABLE"
            or metric.get("reason") in {"currency_mismatch", "evaluator_version_mismatch"}
        ):
            return {
                "metric": rule["metric"],
                "rule": rule["rule"],
                "status": "INCONCLUSIVE",
                "reason": metric.get("reason", "metric_unavailable"),
            }
        before = _number(baseline.get("value"))
        after = _number(candidate.get("value"))
        if before is None or after is None:
            return {
                "metric": rule["metric"],
                "rule": rule["rule"],
                "status": "INCONCLUSIVE",
                "reason": "metric_unavailable",
            }
        direction = metric.get("direction")
        if rule["rule"] in {
            "NO_REGRESSION",
            "MAX_ABSOLUTE_REGRESSION",
            "MAX_RELATIVE_REGRESSION",
            "TRADEOFF",
        } and direction not in {
            MetricDirection.HIGHER_IS_BETTER.value,
            MetricDirection.LOWER_IS_BETTER.value,
        }:
            return {
                "metric": rule["metric"],
                "rule": rule["rule"],
                "status": "INCONCLUSIVE",
                "reason": "unknown_metric_direction",
            }
        improvement = (
            after - before
            if direction == MetricDirection.HIGHER_IS_BETTER.value
            else before - after
        )
        regression = max(0.0, -improvement)
        if rule["rule"] == "NO_REGRESSION":
            passed = regression <= 0
        elif rule["rule"] == "MAX_ABSOLUTE_REGRESSION":
            passed = regression <= float(rule["tolerance"])
        elif rule["rule"] == "MAX_RELATIVE_REGRESSION":
            passed = _relative_regression(before, after, direction) <= float(rule["tolerance"])
        elif rule["rule"] == "MIN_VALUE":
            passed = after >= float(rule["threshold"])
        elif rule["rule"] == "MAX_VALUE":
            passed = after <= float(rule["threshold"])
        else:
            passed = None
        if rule["rule"] == "TRADEOFF":
            passed = regression <= float(rule["tolerance"])
        return {
            "metric": rule["metric"],
            "rule": rule["rule"],
            "status": "PASS" if passed else "FAIL",
            "baseline": before,
            "candidate": after,
        }

    @staticmethod
    def _invalid_policy() -> None:
        raise AgentHubError(
            "INVALID_RELEASE_GATE_POLICY", "The release gate policy is invalid.", 422
        )

    @staticmethod
    async def _load_policy(
        session: AsyncSession, workspace_id: UUID, policy_id: UUID
    ) -> EvaluationReleaseGatePolicy:
        policy = await session.scalar(
            select(EvaluationReleaseGatePolicy).where(
                EvaluationReleaseGatePolicy.workspace_id == workspace_id,
                EvaluationReleaseGatePolicy.id == policy_id,
            )
        )
        if policy is None:
            raise AgentHubError(
                "RELEASE_GATE_POLICY_NOT_FOUND", "The release gate policy was not found.", 404
            )
        return policy

    @staticmethod
    async def _load_comparison(
        session: AsyncSession, workspace_id: UUID, run_id: UUID, comparison_id: UUID
    ) -> EvaluationExperimentComparison:
        comparison = await session.scalar(
            select(EvaluationExperimentComparison).where(
                EvaluationExperimentComparison.workspace_id == workspace_id,
                EvaluationExperimentComparison.experiment_run_id == run_id,
                EvaluationExperimentComparison.id == comparison_id,
            )
        )
        if comparison is None:
            raise AgentHubError(
                "EVALUATION_COMPARISON_NOT_FOUND", "The comparison was not found.", 404
            )
        EvaluationReleaseGateService._verify_comparison(comparison)
        return comparison

    @staticmethod
    def _verify_policy(policy: EvaluationReleaseGatePolicy) -> None:
        expected = canonical_json_hash(policy.policy_json)
        if expected != policy.policy_hash:
            raise AgentHubError(
                "RELEASE_GATE_POLICY_INTEGRITY_ERROR",
                "The release gate policy integrity check failed.",
                409,
            )

    @staticmethod
    def _verify_comparison(comparison: EvaluationExperimentComparison) -> None:
        if not comparison.comparison_hash:
            raise AgentHubError(
                "EVALUATION_COMPARISON_INTEGRITY_ERROR", "The comparison is incomplete.", 409
            )

    @staticmethod
    def _verify_decision(decision, comparison, policy) -> None:
        EvaluationReleaseGateService._verify_policy(policy)
        if decision.comparison_hash != comparison.comparison_hash:
            raise AgentHubError(
                "RELEASE_GATE_DECISION_INTEGRITY_ERROR",
                "The release gate decision binding is invalid.",
                409,
            )
        if decision.policy_hash != policy.policy_hash:
            raise AgentHubError(
                "RELEASE_GATE_DECISION_INTEGRITY_ERROR",
                "The release gate decision policy binding is invalid.",
                409,
            )
        expected = canonical_json_hash(
            {
                "comparison_hash": decision.comparison_hash,
                "policy_hash": decision.policy_hash,
                "status": decision.status,
                "rule_results": decision.rule_results,
                "reasons": decision.reasons,
            }
        )
        if expected != decision.decision_hash:
            raise AgentHubError(
                "RELEASE_GATE_DECISION_INTEGRITY_ERROR",
                "The release gate decision integrity check failed.",
                409,
            )

    @staticmethod
    def _require_read(context: WorkspaceExecutionContext) -> None:
        if (
            EVALUATION_READ not in context.permissions
            and "workspace_read" not in context.permissions
        ):
            raise AgentHubError("FORBIDDEN", "You do not have permission.", 403)

    @staticmethod
    def _require_mutation(context: WorkspaceExecutionContext) -> None:
        if EVALUATION_RUN not in context.permissions:
            raise AgentHubError("FORBIDDEN", "You do not have permission.", 403)

    @staticmethod
    def _workspace_id(context: WorkspaceExecutionContext) -> UUID:
        try:
            return UUID(context.workspace_id)
        except ValueError:
            raise AgentHubError("INVALID_WORKSPACE", "Workspace context is invalid.", 500) from None

    @staticmethod
    def _user_id(context: WorkspaceExecutionContext) -> UUID:
        try:
            if context.user_id is None:
                raise ValueError
            return UUID(context.user_id)
        except ValueError:
            raise AgentHubError(
                "AUTHENTICATION_REQUIRED", "Authentication is required.", 401
            ) from None


def _number(value: Any) -> float | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        return None
    return float(value)


def _relative_regression(before: float, after: float, direction: str) -> float:
    if before == 0:
        return 0.0 if after >= before else math.inf
    improvement = (
        after - before if direction == MetricDirection.HIGHER_IS_BETTER.value else before - after
    )
    return max(0.0, -improvement / abs(before))


__all__ = ["EvaluationReleaseGateService"]
