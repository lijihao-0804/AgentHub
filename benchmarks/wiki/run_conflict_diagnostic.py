"""One bounded real diagnostic after the preserved v1 comparison.

Reuse the same workspace and all six existing revision extraction caches;
perform one new cross-source review. Never overwrite the original experiment.
"""

import argparse
import asyncio
import json
import subprocess
from pathlib import Path
from uuid import UUID

from dotenv import load_dotenv
from sqlalchemy import update

from benchmarks.wiki.run_live import MeasuredGateway
from packages.agent_runtime.adapters.langgraph import configure_windows_asyncio_policy
from packages.control_plane.services import TenantService
from packages.core.database import create_database
from packages.core.execution_context.models import PrincipalContext
from packages.model_gateway.models import ModelProfile
from packages.wiki.answer import answer_from_evidence
from packages.wiki.compiler import GatewayWikiCompiler
from packages.wiki.contracts import thaw_plan
from packages.wiki.models import WikiCompilation
from packages.wiki.query import query_wiki
from packages.wiki.service import WikiService, claim_job, execute_job


async def run(args, result):
    load_dotenv(args.env_file)
    original = json.loads(Path(args.original).read_text(encoding="utf-8"))
    engine, factory = create_database(args.database_url)
    gateway = MeasuredGateway(factory)
    result["calls"] = gateway.calls
    service = WikiService()
    claim = None
    try:
        async with factory() as session:
            prior = await session.get(WikiCompilation, UUID(original["binding"]["compilation_id"]))
            context = (
                await TenantService().get_workspace_access(
                    session,
                    principal=PrincipalContext(
                        request_id="wiki-conflict-diagnostic",
                        trace_id="wiki-conflict-diagnostic",
                        user_id=prior.requested_by,
                    ),
                    workspace_id=prior.workspace_id,
                )
            ).context
            profile_id = UUID(prior.compiler_spec["plan"]["primary"]["id"])
            review_profile_id = None
            if args.review_max_tokens:
                profile = await session.get(ModelProfile, profile_id)
                review_profile = ModelProfile(
                    workspace_id=profile.workspace_id,
                    provider_credential_id=profile.provider_credential_id,
                    model=profile.model,
                    temperature=profile.temperature,
                    max_tokens=args.review_max_tokens,
                    timeout_seconds=profile.timeout_seconds,
                    capabilities=dict(profile.capabilities),
                    fallback_profile_id=profile.fallback_profile_id,
                )
                session.add(review_profile)
                await session.commit()
                review_profile_id = review_profile.id
            row = await service.request(
                session,
                context,
                prior.knowledge_base_id,
                profile_id,
                review_model_profile_id=review_profile_id,
            )
            kb_id = row.knowledge_base_id
            claim = await claim_job(session, row.id)
        if claim is None:
            raise RuntimeError("Diagnostic job was already claimed")
        plan = thaw_plan(row.compiler_spec["plan"])
        review_plan = thaw_plan(row.compiler_spec.get("review_plan", row.compiler_spec["plan"]))
        result["review_config"] = {
            "model": review_plan.primary.model,
            "max_tokens": review_plan.primary.max_tokens,
        }
        result["job_id"] = str(row.id)
        compiler = GatewayWikiCompiler(
            gateway, plan, output_mode="PROMPT_JSON", review_plan=review_plan
        )
        result["execution"] = await execute_job(
            factory, context, row.id, claim.lease_token, compiler
        )
        async with factory() as session:
            ready = await service.get(session, context, kb_id, row.id)
            result["telemetry"] = ready.telemetry
            result["lint"] = await service.lint(session, context, kb_id, row.id)
            publication = await service.publish(session, context, kb_id, row.id)
            result["binding"] = {
                "compilation_id": str(publication.id),
                "compilation_hash": publication.content_hash,
                "source_snapshot_id": str(publication.source_snapshot_id),
                "source_snapshot_hash": publication.source_snapshot_hash,
                "workflow_version": publication.compiler_spec["workflow_version"],
                "review_manifest_hash": publication.compiler_spec["review_manifest_hash"],
            }
            result["pages"] = await service.pages(session, context, kb_id, row.id)
            case = next(c for c in original["cases"]["cases"] if c["id"] == "conflict")
            retrieval = await query_wiki(
                session,
                context,
                kb_id,
                row.id,
                publication.source_snapshot_id,
                case["query"],
                mode="WIKI",
                intent=case["intent"],
            )
            result["query"] = retrieval
        before = len(gateway.calls)
        result["answer"] = await answer_from_evidence(
            gateway, plan, context, case["query"], retrieval
        )
        result["answer_gateway_calls"] = len(gateway.calls) - before
        result["preserved_original_build"] = original["build_sha"]
        result["status"] = (
            "passed"
            if (
                ready.telemetry["compiled_revisions"] == 0
                and ready.telemetry["reused_revisions"] == 6
                and ready.telemetry["conflict_group_count"] > 0
                and retrieval["conflicts"]
                and retrieval["insufficient_evidence"]
                and result["answer"]["insufficient_evidence"]
                and result["answer_gateway_calls"] == 0
            )
            else "failed"
        )
    except Exception as error:
        if claim is not None:
            async with factory() as session:
                await session.execute(
                    update(WikiCompilation)
                    .where(
                        WikiCompilation.id == claim.id,
                        WikiCompilation.status == "PROCESSING",
                        WikiCompilation.lease_token == claim.lease_token,
                    )
                    .values(
                        status="FAILED",
                        error_code=str(getattr(error, "code", "DIAGNOSTIC_FAILED")),
                        lease_token=None,
                        lease_expires_at=None,
                        telemetry={"usage_status": "UNKNOWN", "cost_status": "UNKNOWN"},
                    )
                )
                await session.commit()
        raise
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser()
    for arg in ("env-file", "original", "output", "database-url"):
        parser.add_argument("--" + arg, required=True)
    parser.add_argument("--review-max-tokens", type=int)
    args = parser.parse_args()
    result = {
        "scope": "DEV_TARGETED_DIAGNOSTIC_NOT_PAIRED_BENCHMARK",
        "build_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "pricing": "UNKNOWN",
        "source_count": 6,
    }
    configure_windows_asyncio_policy()
    try:
        asyncio.run(run(args, result))
    except Exception as error:
        result["status"] = "failed"
        result["error_code"] = str(getattr(error, "code", type(error).__name__))
        raise
    finally:
        Path(args.output).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(result["status"], "gateway_requests", len(result["calls"]))
    if result["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
