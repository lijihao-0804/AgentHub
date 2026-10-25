"""Durable Wiki jobs: lease fencing, member reauthorization and broker reconciliation."""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, or_, select, update

from apps.worker.celery_app import celery_app
from packages.control_plane.services import TenantService
from packages.core.config.settings import get_settings
from packages.core.database import create_database
from packages.core.execution_context.models import PrincipalContext
from packages.model_gateway.gateway import SqlAlchemyModelGateway
from packages.model_gateway.models import ProviderCredential
from packages.wiki.contracts import thaw_plan
from packages.wiki.models import WikiCompilation
from packages.wiki.service import MAX_ATTEMPTS, claim_job, execute_job, renew_job


async def run_wiki_job(job_id):
    from packages.wiki.compiler import GatewayWikiCompiler, compiler_manifest_hash

    engine, factory = create_database(get_settings().database_url)
    renewal = None
    claim = None
    try:
        async with factory() as session:
            claim = await claim_job(session, UUID(job_id))
            if claim is None:
                return "not_claimed"
            token = claim.lease_token
            access = await TenantService().get_workspace_access(
                session,
                principal=PrincipalContext(
                    request_id=f"wiki:{job_id}",
                    trace_id=f"wiki:{job_id}",
                    user_id=claim.requested_by,
                ),
                workspace_id=claim.workspace_id,
            )
            context = access.context
            plan = thaw_plan(claim.compiler_spec["plan"])
            if claim.compiler_spec.get("manifest_hash") != compiler_manifest_hash():
                from packages.wiki.service import fail

                fail("WIKI_COMPILER_VERSION_UNAVAILABLE", "The frozen compiler is unavailable.")

        async def keep_lease():
            while True:
                await asyncio.sleep(45)
                try:
                    async with factory() as session:
                        if not await renew_job(session, claim.id, token):
                            return
                except Exception:
                    # Do not claim success from a failed renewal. Final installation is fenced.
                    return

        renewal = asyncio.create_task(keep_lease())

        class ScopedGateway:
            async def generate_resolved(self, context, plan, request):
                async with factory() as session:
                    current = await TenantService().get_workspace_access(
                        session,
                        principal=context.organization.principal,
                        workspace_id=UUID(context.workspace_id),
                    )
                    from packages.wiki.service import require

                    require(current.context, "knowledge_edit")
                    from packages.wiki.service import WikiService, fail

                    await WikiService().sources(session, current.context, claim)
                    for profile in plan.chain:
                        credential = await session.scalar(
                            select(ProviderCredential).where(
                                ProviderCredential.workspace_id == UUID(context.workspace_id),
                                ProviderCredential.id == profile.provider_credential_id,
                            )
                        )
                        expected = claim.compiler_spec["provider_routes"].get(
                            str(profile.provider_credential_id)
                        )
                        if credential is None or credential.base_url != expected:
                            fail("WIKI_MODEL_ROUTE_CHANGED", "The frozen model route changed.")
                    if not await renew_job(session, claim.id, token):
                        fail("WIKI_LEASE_LOST", "The compilation lease is no longer owned.")
                    # Resolve/decrypt in a short transaction; no DB connection spans LLM latency.
                    gateway = SqlAlchemyModelGateway(session)
                    prepared = await gateway.prepare_resolved(
                        context, plan, request, operation="generate"
                    )
                return await prepared.generate_resolved(context, plan, request)

        compiler = GatewayWikiCompiler(
            ScopedGateway(), plan, output_mode=claim.compiler_spec["output_mode"]
        )
        return await execute_job(factory, context, claim.id, token, compiler)
    except Exception as error:
        if claim is not None:
            # Persist a safe error code only; provider exceptions can contain source text/secrets.
            code = getattr(error, "code", "WIKI_COMPILATION_FAILED")
            code = str(getattr(code, "value", code))[:64]
            async with factory() as session:
                await session.execute(
                    update(WikiCompilation)
                    .where(
                        WikiCompilation.id == claim.id,
                        WikiCompilation.status == "PROCESSING",
                        WikiCompilation.lease_token == claim.lease_token,
                    )
                    .values(
                        status="FAILED", error_code=code, lease_token=None, lease_expires_at=None
                    )
                )
                await session.commit()
        return "failed"
    finally:
        if renewal is not None:
            renewal.cancel()
            try:
                await renewal
            except asyncio.CancelledError:
                pass
        await engine.dispose()


@celery_app.task(name="agenthub.compile_wiki")
def compile_wiki(job_id):
    return asyncio.run(run_wiki_job(job_id))


async def reconcile():
    engine, factory = create_database(get_settings().database_url)
    try:
        async with factory() as session:
            await session.execute(
                update(WikiCompilation)
                .where(
                    WikiCompilation.status == "PROCESSING",
                    WikiCompilation.lease_expires_at < func.now(),
                    WikiCompilation.attempt_count >= MAX_ATTEMPTS,
                )
                .values(
                    status="FAILED",
                    error_code="WIKI_MAX_ATTEMPTS",
                    lease_token=None,
                    lease_expires_at=None,
                )
            )
            ids = list(
                await session.scalars(
                    select(WikiCompilation.id)
                    .where(
                        or_(
                            WikiCompilation.status == "PENDING",
                            (WikiCompilation.status == "PROCESSING")
                            & (WikiCompilation.lease_expires_at < datetime.now(UTC)),
                        ),
                        WikiCompilation.attempt_count < MAX_ATTEMPTS,
                    )
                    .order_by(WikiCompilation.created_at)
                    .limit(100)
                )
            )
            await session.commit()
        for job_id in ids:
            try:
                compile_wiki.delay(str(job_id))
            except Exception:
                break
        return len(ids)
    finally:
        await engine.dispose()


@celery_app.task(name="agenthub.reconcile_wiki")
def reconcile_wiki():
    return asyncio.run(reconcile())
