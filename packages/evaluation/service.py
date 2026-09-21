"""Application services for workspace-scoped M7-A evaluation data."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, NoReturn
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.agent_runtime.models import AgentRun
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.evaluation.models import (
    EvaluationDataset,
    EvaluationDatasetItem,
    EvaluationDatasetVersion,
    EvaluationDatasetVersionStatus,
    PricingSnapshot,
)
from packages.evaluation.reproducibility import pricing_snapshot_content_hash
from packages.evaluation.validation import dataset_content_hash, validate_dataset_items

_RUN_INPUT_FIELDS = {
    "RETRIEVAL": "query",
    "KNOWLEDGE_QA": "question",
    "TOOL": "request",
    "NO_ANSWER": "question",
    "APPROVAL": "action",
    "MULTI_STEP": "task",
    "FAILURE": "scenario",
}


class EvaluationDatasetService:
    async def create_dataset(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
        name: str,
        description: str | None = None,
    ) -> EvaluationDataset:
        self._require_permission(context, "evaluation_manage")
        workspace_id = self._workspace_id(context)
        normalized_name = name.strip()
        if not normalized_name:
            raise AgentHubError("EVALUATION_DATASET_INVALID", "Dataset name is required.", 422)
        dataset = EvaluationDataset(
            workspace_id=workspace_id,
            name=normalized_name,
            description=description.strip() if description else None,
            created_by=self._user_id(context),
        )
        session.add(dataset)
        await session.commit()
        return dataset

    async def list_datasets(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
    ) -> list[EvaluationDataset]:
        self._require_permission(context, "workspace_read")
        workspace_id = self._workspace_id(context)
        result = await session.scalars(
            select(EvaluationDataset)
            .where(EvaluationDataset.workspace_id == workspace_id)
            .order_by(EvaluationDataset.created_at, EvaluationDataset.id)
        )
        return list(result)

    async def get_dataset(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
        dataset_id: UUID,
    ) -> EvaluationDataset:
        self._require_permission(context, "workspace_read")
        dataset = await session.scalar(
            select(EvaluationDataset).where(
                EvaluationDataset.workspace_id == self._workspace_id(context),
                EvaluationDataset.id == dataset_id,
            )
        )
        if dataset is None:
            self._not_found()
        return dataset

    async def create_version(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
        dataset_id: UUID,
        items: Sequence[Mapping[str, Any]],
        schema_version: int = 1,
    ) -> EvaluationDatasetVersion:
        self._require_permission(context, "evaluation_manage")
        if schema_version < 1:
            raise AgentHubError("EVALUATION_DATASET_INVALID", "schema_version is invalid.", 422)
        normalized_items = validate_dataset_items(items)
        content_hash = dataset_content_hash(normalized_items, schema_version=schema_version)
        workspace_id = self._workspace_id(context)
        dataset = await session.scalar(
            select(EvaluationDataset)
            .where(
                EvaluationDataset.workspace_id == workspace_id,
                EvaluationDataset.id == dataset_id,
            )
            .with_for_update()
        )
        if dataset is None:
            self._not_found()
        version_number = (
            await session.scalar(
                select(func.coalesce(func.max(EvaluationDatasetVersion.version_number), 0)).where(
                    EvaluationDatasetVersion.workspace_id == workspace_id,
                    EvaluationDatasetVersion.dataset_id == dataset_id,
                )
            )
        ) + 1
        version = EvaluationDatasetVersion(
            workspace_id=workspace_id,
            dataset_id=dataset_id,
            version_number=version_number,
            schema_version=schema_version,
            content_hash=content_hash,
            status=EvaluationDatasetVersionStatus.DRAFT,
            created_by=self._user_id(context),
        )
        session.add(version)
        await session.flush()
        session.add_all(
            [
                EvaluationDatasetItem(
                    workspace_id=workspace_id,
                    dataset_version_id=version.id,
                    case_key=item["case_key"],
                    split=item["split"],
                    category=item["category"],
                    input=item["input"],
                    expected=item["expected"],
                    tags=item["tags"],
                    source_provenance=item["source_provenance"],
                    ordinal=item["ordinal"],
                )
                for item in normalized_items
            ]
        )
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise AgentHubError(
                "EVALUATION_DATASET_VERSION_CREATE_FAILED",
                "The dataset version could not be recorded.",
                409,
            ) from exc
        return version

    async def create_version_from_run(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
        dataset_id: UUID,
        base_version_id: UUID,
        run_id: UUID,
        case_key: str,
        split: str,
        category: str,
        expected: Mapping[str, Any],
        tags: Sequence[str],
    ) -> EvaluationDatasetVersion:
        """Create a draft version by importing one observed AgentRun safely.

        The run is evidence only.  The caller owns ``expected`` and the
        server derives the input and provenance from the persisted run.
        """

        self._require_permission(context, "evaluation_manage")
        workspace_id = self._workspace_id(context)
        dataset = await session.scalar(
            select(EvaluationDataset)
            .where(
                EvaluationDataset.workspace_id == workspace_id,
                EvaluationDataset.id == dataset_id,
            )
            .with_for_update()
        )
        if dataset is None:
            self._not_found()

        run = await session.scalar(
            select(AgentRun).where(
                AgentRun.workspace_id == workspace_id,
                AgentRun.id == run_id,
            )
        )
        if run is None:
            raise AgentHubError("AGENT_RUN_NOT_FOUND", "The agent run was not found.", 404)

        base_version = await session.scalar(
            select(EvaluationDatasetVersion).where(
                EvaluationDatasetVersion.workspace_id == workspace_id,
                EvaluationDatasetVersion.dataset_id == dataset_id,
                EvaluationDatasetVersion.id == base_version_id,
            )
        )
        if base_version is None:
            self._not_found()

        item_rows = await session.scalars(
            select(EvaluationDatasetItem)
            .where(
                EvaluationDatasetItem.workspace_id == workspace_id,
                EvaluationDatasetItem.dataset_version_id == base_version_id,
            )
            .order_by(EvaluationDatasetItem.ordinal, EvaluationDatasetItem.id)
        )
        base_items = [
            {
                "case_key": item.case_key,
                "split": item.split,
                "category": item.category,
                "input": item.input,
                "expected": item.expected,
                "tags": item.tags,
                "source_provenance": item.source_provenance,
                "ordinal": item.ordinal,
            }
            for item in item_rows
        ]
        if any(
            item["source_provenance"].get("source_kind") == "agent_run"
            and item["source_provenance"].get("source_id") == str(run.id)
            for item in base_items
        ):
            raise AgentHubError(
                "EVALUATION_DATASET_SOURCE_DUPLICATE",
                "This agent run is already imported into the base dataset version.",
                409,
            )

        source_provenance = {
            "source_kind": "agent_run",
            "source_id": str(run.id),
            "agent_version_id": str(run.agent_version_id),
            "resolved_spec_hash": run.resolved_spec_hash,
            "observed_status": run.status,
            "observed_failure_code": run.failure_code,
        }
        imported_item = {
            "case_key": case_key,
            "split": split,
            "category": category,
            "input": derive_run_input(category, run.input_text),
            "expected": dict(expected),
            "tags": list(tags),
            "source_provenance": source_provenance,
            "ordinal": max((item["ordinal"] for item in base_items), default=-1) + 1,
        }
        return await self.create_version(
            session,
            context=context,
            dataset_id=dataset_id,
            items=[*base_items, imported_item],
            schema_version=base_version.schema_version,
        )

    async def list_versions(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
        dataset_id: UUID,
    ) -> list[EvaluationDatasetVersion]:
        self._require_permission(context, "workspace_read")
        workspace_id = self._workspace_id(context)
        await self._require_dataset(session, workspace_id, dataset_id)
        result = await session.scalars(
            select(EvaluationDatasetVersion)
            .where(
                EvaluationDatasetVersion.workspace_id == workspace_id,
                EvaluationDatasetVersion.dataset_id == dataset_id,
            )
            .order_by(EvaluationDatasetVersion.version_number.desc())
        )
        return list(result)

    async def get_version(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
        dataset_id: UUID,
        version_id: UUID,
    ) -> EvaluationDatasetVersion:
        self._require_permission(context, "workspace_read")
        version = await session.scalar(
            select(EvaluationDatasetVersion).where(
                EvaluationDatasetVersion.workspace_id == self._workspace_id(context),
                EvaluationDatasetVersion.dataset_id == dataset_id,
                EvaluationDatasetVersion.id == version_id,
            )
        )
        if version is None:
            self._not_found()
        return version

    async def list_version_items(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
        dataset_id: UUID,
        version_id: UUID,
    ) -> list[EvaluationDatasetItem]:
        await self.get_version(
            session, context=context, dataset_id=dataset_id, version_id=version_id
        )
        result = await session.scalars(
            select(EvaluationDatasetItem)
            .where(
                EvaluationDatasetItem.workspace_id == self._workspace_id(context),
                EvaluationDatasetItem.dataset_version_id == version_id,
            )
            .order_by(EvaluationDatasetItem.ordinal, EvaluationDatasetItem.id)
        )
        return list(result)

    async def publish_version(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
        dataset_id: UUID,
        version_id: UUID,
    ) -> EvaluationDatasetVersion:
        self._require_permission(context, "evaluation_manage")
        workspace_id = self._workspace_id(context)
        version = await session.scalar(
            select(EvaluationDatasetVersion)
            .where(
                EvaluationDatasetVersion.workspace_id == workspace_id,
                EvaluationDatasetVersion.dataset_id == dataset_id,
                EvaluationDatasetVersion.id == version_id,
            )
            .with_for_update()
        )
        if version is None:
            self._not_found()
        if version.status == EvaluationDatasetVersionStatus.PUBLISHED:
            raise AgentHubError(
                "EVALUATION_DATASET_IMMUTABLE",
                "Published dataset versions are immutable.",
                409,
            )
        item_rows = await session.scalars(
            select(EvaluationDatasetItem)
            .where(
                EvaluationDatasetItem.workspace_id == workspace_id,
                EvaluationDatasetItem.dataset_version_id == version_id,
            )
            .order_by(EvaluationDatasetItem.ordinal, EvaluationDatasetItem.id)
        )
        items = [
            {
                "case_key": item.case_key,
                "split": item.split,
                "category": item.category,
                "input": item.input,
                "expected": item.expected,
                "tags": item.tags,
                "source_provenance": item.source_provenance,
                "ordinal": item.ordinal,
            }
            for item in item_rows
        ]
        if (
            dataset_content_hash(items, schema_version=version.schema_version)
            != version.content_hash
        ):
            raise AgentHubError(
                "EVALUATION_DATASET_INTEGRITY_ERROR",
                "The dataset version content hash does not match its items.",
                409,
            )
        version.status = EvaluationDatasetVersionStatus.PUBLISHED
        version.published_at = datetime.now(UTC)
        await session.commit()
        return version

    async def create_pricing_snapshot(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
        name: str,
        provider: str,
        model: str,
        currency: str,
        input_price_per_1m: Decimal | str | int,
        output_price_per_1m: Decimal | str | int,
        cached_input_price_per_1m: Decimal | str | int | None,
        effective_at: datetime,
        source_note: str,
    ) -> PricingSnapshot:
        self._require_permission(context, "evaluation_manage")
        workspace_id = self._workspace_id(context)
        normalized_name = self._required_text(name, "name")
        normalized_provider = self._required_text(provider, "provider")
        normalized_model = self._required_text(model, "model")
        normalized_note = self._required_text(source_note, "source_note")
        normalized_currency = currency.strip().upper()
        if len(normalized_currency) != 3 or not normalized_currency.isalpha():
            raise AgentHubError(
                "EVALUATION_PRICING_INVALID", "currency must be a 3-letter code.", 422
            )
        if effective_at.tzinfo is None or effective_at.utcoffset() is None:
            raise AgentHubError(
                "EVALUATION_PRICING_INVALID",
                "effective_at must include a timezone.",
                422,
            )
        input_price = self._money(input_price_per_1m, "input_price_per_1m")
        output_price = self._money(output_price_per_1m, "output_price_per_1m")
        cached_price = (
            self._money(cached_input_price_per_1m, "cached_input_price_per_1m")
            if cached_input_price_per_1m is not None
            else None
        )
        snapshot = PricingSnapshot(
            workspace_id=workspace_id,
            name=normalized_name,
            provider=normalized_provider,
            model=normalized_model,
            currency=normalized_currency,
            input_price_per_1m=input_price,
            output_price_per_1m=output_price,
            cached_input_price_per_1m=cached_price,
            effective_at=effective_at,
            source_note=normalized_note,
            created_by=self._user_id(context),
        )
        snapshot.content_hash = pricing_snapshot_content_hash(snapshot)
        session.add(snapshot)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise AgentHubError(
                "EVALUATION_PRICING_CREATE_FAILED",
                "The pricing snapshot could not be recorded.",
                409,
            ) from exc
        return snapshot

    async def list_pricing_snapshots(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
    ) -> list[PricingSnapshot]:
        self._require_permission(context, "workspace_read")
        result = await session.scalars(
            select(PricingSnapshot)
            .where(PricingSnapshot.workspace_id == self._workspace_id(context))
            .order_by(PricingSnapshot.effective_at.desc(), PricingSnapshot.id)
        )
        return list(result)

    @staticmethod
    async def _require_dataset(
        session: AsyncSession, workspace_id: UUID, dataset_id: UUID
    ) -> EvaluationDataset:
        dataset = await session.scalar(
            select(EvaluationDataset).where(
                EvaluationDataset.workspace_id == workspace_id,
                EvaluationDataset.id == dataset_id,
            )
        )
        if dataset is None:
            EvaluationDatasetService._not_found()
        return dataset

    @staticmethod
    def _money(value: Decimal | str | int, field: str) -> Decimal:
        if isinstance(value, float):
            raise AgentHubError("EVALUATION_PRICING_INVALID", f"{field} must not be a float.", 422)
        try:
            decimal_value = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise AgentHubError("EVALUATION_PRICING_INVALID", f"{field} is invalid.", 422) from None
        if decimal_value.is_nan() or decimal_value.is_infinite() or decimal_value < 0:
            raise AgentHubError("EVALUATION_PRICING_INVALID", f"{field} is invalid.", 422)
        try:
            quantized = decimal_value.quantize(Decimal("0.00000001"))
        except InvalidOperation:
            raise AgentHubError("EVALUATION_PRICING_INVALID", f"{field} is invalid.", 422) from None
        if decimal_value != quantized:
            raise AgentHubError(
                "EVALUATION_PRICING_INVALID", f"{field} supports at most 8 decimals.", 422
            )
        return quantized

    @staticmethod
    def _required_text(value: str, field: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise AgentHubError("EVALUATION_PRICING_INVALID", f"{field} is required.", 422)
        return normalized

    @staticmethod
    def _require_permission(context: WorkspaceExecutionContext, permission: str) -> None:
        if permission not in context.permissions:
            raise AgentHubError("FORBIDDEN", "You do not have permission.", 403)

    @staticmethod
    def _workspace_id(context: WorkspaceExecutionContext) -> UUID:
        try:
            return UUID(context.workspace_id)
        except ValueError:
            raise AgentHubError("INVALID_WORKSPACE", "Workspace context is invalid.", 500) from None

    @staticmethod
    def _user_id(context: WorkspaceExecutionContext) -> UUID:
        if context.user_id is None:
            raise AgentHubError("AUTHENTICATION_REQUIRED", "Authentication is required.", 401)
        try:
            return UUID(context.user_id)
        except ValueError:
            raise AgentHubError(
                "AUTHENTICATION_REQUIRED", "Authentication is required.", 401
            ) from None

    @staticmethod
    def _not_found() -> NoReturn:
        raise AgentHubError("RESOURCE_NOT_FOUND", "Resource was not found.", 404)


__all__ = ["EvaluationDatasetService"]


def derive_run_input(category: str, input_text: str) -> dict[str, str]:
    field = _RUN_INPUT_FIELDS.get(category, "question")
    return {field: input_text}
