"""Verify editor conflict curation on real compiled evidence; forbid new LLM calls."""

import argparse
import asyncio
import json
import subprocess
from pathlib import Path
from uuid import UUID

from dotenv import load_dotenv
from sqlalchemy import update

from packages.agent_runtime.adapters.langgraph import configure_windows_asyncio_policy
from packages.control_plane.services import TenantService
from packages.core.database import create_database
from packages.core.execution_context.models import PrincipalContext
from packages.wiki.answer import answer_from_evidence
from packages.wiki.compiler import GatewayWikiCompiler
from packages.wiki.contracts import thaw_plan
from packages.wiki.models import WikiCompilation
from packages.wiki.query import query_wiki
from packages.wiki.service import WikiService, claim_job, execute_job


async def run(args, result):
    load_dotenv(args.env_file)
    original = json.loads(Path(args.original).read_text(encoding="utf-8"))
    source_claims = [c for p in original["pages"] for c in p["claims"]]
    refs = [
        {"chunk_id": c["chunk_id"], "quote": c["quote"]}
        for c in (
            next(
                c
                for c in source_claims
                if "7 years" in c["quote"] and "retention" in c["quote"].lower()
            ),
            next(
                c
                for c in source_claims
                if "5 years" in c["quote"] and "retention" in c["quote"].lower()
            ),
        )
    ]
    result["editor_declared_evidence"] = refs
    engine, factory = create_database(args.database_url)
    service = WikiService()
    claim = None

    class NoModelCalls:
        async def generate_resolved(self, *args):
            raise AssertionError("This curation diagnostic must not call a model")

    try:
        async with factory() as session:
            prior = await session.get(WikiCompilation, UUID(original["binding"]["compilation_id"]))
            context = (
                await TenantService().get_workspace_access(
                    session,
                    principal=PrincipalContext(
                        request_id="wiki-editor-diagnostic",
                        trace_id="wiki-editor-diagnostic",
                        user_id=prior.requested_by,
                    ),
                    workspace_id=prior.workspace_id,
                )
            ).context
            row = await service.request(
                session,
                context,
                prior.knowledge_base_id,
                UUID(prior.compiler_spec["plan"]["primary"]["id"]),
                review_mode="BASIC",
                manual_conflict_groups=[refs],
            )
            claim = await claim_job(session, row.id)
            kb_id = row.knowledge_base_id
        plan = thaw_plan(row.compiler_spec["plan"])
        compiler = GatewayWikiCompiler(NoModelCalls(), plan, output_mode="PROMPT_JSON")
        result["execution"] = await execute_job(
            factory, context, row.id, claim.lease_token, compiler
        )
        async with factory() as session:
            publication = await service.publish(session, context, kb_id, row.id)
            result["telemetry"] = publication.telemetry
            result["binding"] = {
                "compilation_id": str(publication.id),
                "compilation_hash": publication.content_hash,
                "source_snapshot_id": str(publication.source_snapshot_id),
                "source_snapshot_hash": publication.source_snapshot_hash,
            }
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
            result["lint"] = await service.lint(session, context, kb_id, row.id)
        result["answer"] = await answer_from_evidence(
            NoModelCalls(), plan, context, case["query"], retrieval
        )
        result["status"] = (
            "passed"
            if (
                publication.telemetry["compiled_revisions"] == 0
                and publication.telemetry["reused_revisions"] == 6
                and result["answer"]["insufficient_evidence"]
                and len({c["document_revision_id"] for c in retrieval["conflicts"]}) >= 2
            )
            else "failed"
        )
        result["new_gateway_requests"] = 0
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
                        error_code=str(getattr(error, "code", "CURATION_DIAGNOSTIC_FAILED")),
                        lease_token=None,
                        lease_expires_at=None,
                    )
                )
                await session.commit()
        raise
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser()
    for key in ("env-file", "original", "output", "database-url"):
        parser.add_argument("--" + key, required=True)
    args = parser.parse_args()
    result = {
        "scope": "EDITOR_CURATION_ON_REAL_EXTRACTED_EVIDENCE_NOT_AUTOMATIC_DETECTION",
        "build_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
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
    print(result["status"])
    if result["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
