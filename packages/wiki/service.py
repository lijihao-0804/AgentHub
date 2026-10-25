"""Deterministic authorization, provenance validation and atomic Wiki publication."""

from __future__ import annotations

import html
import re
from collections import defaultdict
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from packages.control_plane.audit import append_audit
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.errors.exceptions import AgentHubError
from packages.knowledge.models import (
    Document,
    DocumentChunk,
    DocumentRevision,
    KnowledgeBase,
    KnowledgeSnapshotItem,
)
from packages.knowledge.snapshots import KnowledgeSnapshotService
from packages.model_gateway.contracts import (
    CapabilityRequirements,
    ResolvedModelExecutionPlan,
    ResolvedModelExecutionProfile,
)
from packages.model_gateway.models import ProviderCredential
from packages.model_gateway.profile_resolution import ModelProfileResolver
from packages.model_gateway.repositories import SqlAlchemyModelGatewayRepository
from packages.wiki.contracts import COMPILER_VERSION, freeze_plan, page_key
from packages.wiki.models import WikiCompilation, WikiExtraction, WikiPage, WikiSourceRevocation

LEASE_SECONDS = 180
MAX_ATTEMPTS = 3


def require(context, permission):
    if permission not in context.permissions or not context.user_id:
        raise AgentHubError("FORBIDDEN", "You do not have permission.", 403)


def fail(code, message, status=409):
    raise AgentHubError(code, message, status)


def _audit(session, context, action, row):
    append_audit(
        session,
        action=action,
        resource_type="wiki_compilation",
        resource_id=str(row.id),
        request_id=context.request_id,
        actor_user_id=UUID(context.user_id),
        organization_id=UUID(context.organization.organization_id),
        workspace_id=UUID(context.workspace_id),
        safe_metadata={"status": row.status},
    )


def _chunk_data(c):
    return {
        "chunk_id": c.chunk_id,
        "text": c.text,
        "document_id": str(c.document_id),
        "document_revision_id": str(c.document_revision_id),
        "locator": c.locator,
    }


def validate_extraction(payload: dict, chunks: list[dict]) -> list[dict]:
    # Validate again at the persistence boundary, even for injected compiler adapters.
    from packages.wiki.compiler import Extraction

    parsed = Extraction.model_validate(payload)
    by_id = {c["chunk_id"]: c for c in chunks}
    result = []
    for proposal in parsed.claims:
        claim = proposal.model_dump(mode="json")
        source = by_id.get(claim["chunk_id"])
        if source is None or not claim["quote"].strip() or claim["quote"] not in source["text"]:
            fail("WIKI_INVALID_PROVENANCE", "A proposed claim has invalid source evidence.", 422)
        if not claim["topic"].strip() or not claim["predicate"].strip():
            fail("WIKI_INVALID_PROPOSAL", "A proposed topic or predicate is blank.", 422)
        claim.update({k: source[k] for k in ("document_id", "document_revision_id", "locator")})
        result.append(claim)
    return result


def organize(claims: list[dict]) -> list[dict]:
    """Materialize extractive topic/entity summaries without recursive model synthesis."""
    grouped = defaultdict(list)
    for c in claims:
        grouped[page_key(c["topic"])].append(dict(c))
    pages = []
    for key, items in sorted(grouped.items()):
        items.sort(key=lambda c: (c["document_revision_id"], c["chunk_id"], c["quote"]))
        seen = set()
        unique = []
        for c in items:
            identity = (c["chunk_id"], c["quote"], c["predicate"], c["condition"])
            if identity not in seen:
                unique.append(c)
                seen.add(identity)
        groups = defaultdict(set)
        for c in unique:
            groups[(c["predicate"].casefold(), c["condition"].casefold())].add(
                c["value"].casefold()
            )
        relations = set()
        for c in unique:
            c["conflict"] = (
                c.get("conflict", False)
                or len(groups[(c["predicate"].casefold(), c["condition"].casefold())]) > 1
            )
            for related in c.pop("related_topics", []):
                target = page_key(related)
                if target in grouped and target != key:
                    relations.add(target)
        title = " ".join(unique[0]["topic"].split())

        # Display derived data as escaped literal text. No model-authored executable links/HTML.
        def literal(value):
            return re.sub(r"([\\`*_{}\[\]()#+.!|>-])", r"\\\1", html.escape(value))

        lines = ["# " + literal(title), "", "Evidence-backed extractive summary.", ""]
        for c in unique:
            marker = " [POSSIBLE CONFLICT]" if c["conflict"] else ""
            quoted = literal(c["quote"]).replace("\n", "\n> ")
            lines.extend(["> " + quoted, "", f"Source: `{c['chunk_id']}`{marker}", ""])
        pages.append(
            {
                "page_key": key,
                "title": title,
                "kind": unique[0]["kind"],
                "markdown": "\n".join(lines),
                "claims": unique,
                "relations": sorted(relations),
            }
        )
    # Related-topic links are hints. Same-source co-occurrence adds navigable reciprocal edges.
    for p in pages:
        sources = {c["document_revision_id"] for c in p["claims"]}
        p["relations"] = sorted(
            set(p["relations"])
            | {
                q["page_key"]
                for q in pages
                if q != p and sources.intersection(c["document_revision_id"] for c in q["claims"])
            }
        )
        peer_chunks = {key for c in p["claims"] for key in c.get("conflict_peers", [])}
        p["relations"] = sorted(
            set(p["relations"])
            | {
                q["page_key"]
                for q in pages
                if q["page_key"] != p["page_key"]
                and peer_chunks.intersection(c["chunk_id"] for c in q["claims"])
            }
        )
    return pages


def publication_hash(row, pages):
    return canonical_json_hash(
        {"schema_version": 1, "identity_hash": row.identity_hash, "pages": pages}
    )


def extraction_spec_hash(spec):
    """Source exclusions and global review do not change per-revision extraction."""
    return canonical_json_hash(
        {
            k: v
            for k, v in spec.items()
            if k
            not in {
                "excluded_revision_ids",
                "review_manifest",
                "review_manifest_hash",
                "workflow_version",
                "review_plan",
                "review_provider_routes",
            }
        }
    )


def apply_review_groups(claims, groups):
    """A model may only relate existing authorized evidence, never add claims."""
    if not isinstance(groups, list) or len(groups) > 50:
        fail("WIKI_INVALID_CONFLICT_REVIEW", "The conflict review exceeds its group limit.")
    for group in groups:
        if (
            not isinstance(group, list)
            or not 2 <= len(group) <= 8
            or any(
                isinstance(i, bool) or not isinstance(i, int) or not 1 <= i <= len(claims)
                for i in group
            )
            or len(group) != len(set(group))
        ):
            fail(
                "WIKI_INVALID_CONFLICT_REVIEW", "A conflict group has invalid evidence references."
            )
        peers = sorted({claims[i - 1]["chunk_id"] for i in group})
        for index in group:
            claim = claims[index - 1]
            claim["conflict"] = True
            claim["conflict_peers"] = sorted(set(claim.get("conflict_peers", [])) | set(peers))


async def resolve_model_plan(session, context, profile_id, *, structured=False):
    chain = await ModelProfileResolver(SqlAlchemyModelGatewayRepository(session)).resolve_chain(
        context,
        profile_id,
        required_capabilities=CapabilityRequirements(
            frozenset({"structured_output"}) if structured else frozenset()
        ),
    )
    repository = SqlAlchemyModelGatewayRepository(session)
    profiles = []
    for p in chain:
        cred = await repository.get_provider_credential(context, p.provider_credential_id)
        if cred is None or not cred.enabled:
            fail("WIKI_MODEL_UNAVAILABLE", "The selected model is unavailable.")
        profiles.append(
            ResolvedModelExecutionProfile(
                **{
                    k: v
                    for k, v in asdict(p).items()
                    if k not in {"fallback_profile_id", "capabilities"}
                },
                provider=cred.provider,
                capabilities=p.capabilities,
            )
        )
    return ResolvedModelExecutionPlan(profiles[0], tuple(profiles[1:]))


class WikiService:
    async def revoke_source(self, session, context, kb_id, revision_id):
        require(context, "knowledge_edit")
        revision = await session.scalar(
            select(DocumentRevision)
            .where(
                DocumentRevision.workspace_id == UUID(context.workspace_id),
                DocumentRevision.knowledge_base_id == kb_id,
                DocumentRevision.id == revision_id,
            )
            .with_for_update()
        )
        if revision is None:
            fail("DOCUMENT_REVISION_NOT_FOUND", "The source revision was not found.", 404)
        await session.execute(
            insert(WikiSourceRevocation)
            .values(
                workspace_id=UUID(context.workspace_id),
                knowledge_base_id=kb_id,
                document_revision_id=revision_id,
                revoked_by=context.user_id,
            )
            .on_conflict_do_nothing()
        )
        append_audit(
            session,
            action="wiki_source_revoke",
            resource_type="document_revision",
            resource_id=str(revision.id),
            request_id=context.request_id,
            actor_user_id=UUID(context.user_id),
            workspace_id=UUID(context.workspace_id),
            organization_id=UUID(context.organization.organization_id),
            safe_metadata={"scope": "WIKI", "status": "REVOKED"},
        )
        await session.commit()

    async def request(
        self,
        session,
        context,
        kb_id,
        model_profile_id,
        selector="LATEST",
        output_mode="PROMPT_JSON",
        review_model_profile_id=None,
    ):
        require(context, "knowledge_edit")
        if output_mode not in {"JSON_SCHEMA", "PROMPT_JSON"}:
            fail("WIKI_INVALID_OUTPUT_MODE", "Invalid compiler output mode.", 422)
        snapshot = await KnowledgeSnapshotService().resolve_snapshot(
            session, context, kb_id, selector
        )
        plan = await resolve_model_plan(
            session, context, model_profile_id, structured=output_mode == "JSON_SCHEMA"
        )
        spec = {
            "compiler_version": COMPILER_VERSION,
            "output_mode": output_mode,
            "plan": freeze_plan(plan),
        }
        from packages.wiki.compiler import compiler_manifest, compiler_manifest_hash

        spec["manifest"] = compiler_manifest()
        spec["manifest_hash"] = compiler_manifest_hash()
        routes = {}
        for profile in plan.chain:
            credential = await session.scalar(
                select(ProviderCredential).where(
                    ProviderCredential.workspace_id == UUID(context.workspace_id),
                    ProviderCredential.id == profile.provider_credential_id,
                )
            )
            routes[str(profile.provider_credential_id)] = credential.base_url
        spec["provider_routes"] = routes
        excluded = await session.scalars(
            select(WikiSourceRevocation.document_revision_id)
            .join(
                KnowledgeSnapshotItem,
                (
                    KnowledgeSnapshotItem.document_revision_id
                    == WikiSourceRevocation.document_revision_id
                )
                & (KnowledgeSnapshotItem.workspace_id == WikiSourceRevocation.workspace_id)
                & (
                    KnowledgeSnapshotItem.knowledge_base_id
                    == WikiSourceRevocation.knowledge_base_id
                ),
            )
            .where(
                WikiSourceRevocation.workspace_id == UUID(context.workspace_id),
                WikiSourceRevocation.knowledge_base_id == kb_id,
                KnowledgeSnapshotItem.snapshot_id == snapshot.snapshot_id,
            )
            .order_by(WikiSourceRevocation.document_revision_id)
        )
        spec["excluded_revision_ids"] = [str(value) for value in excluded]
        from packages.wiki.conflict_review import review_manifest

        spec["workflow_version"] = "wiki-workflow-v2"
        spec["review_manifest"] = review_manifest()
        spec["review_manifest_hash"] = canonical_json_hash(spec["review_manifest"])
        review_plan = (
            await resolve_model_plan(session, context, review_model_profile_id)
            if review_model_profile_id is not None
            else plan
        )
        spec["review_plan"] = freeze_plan(review_plan)
        review_routes = {}
        for profile in review_plan.chain:
            credential = await session.scalar(
                select(ProviderCredential).where(
                    ProviderCredential.workspace_id == UUID(context.workspace_id),
                    ProviderCredential.id == profile.provider_credential_id,
                )
            )
            review_routes[str(profile.provider_credential_id)] = credential.base_url
        spec["review_provider_routes"] = review_routes
        identity = canonical_json_hash(
            {
                "schema_version": 1,
                "spec": spec,
                "source_snapshot_id": str(snapshot.snapshot_id),
                "source_hash": snapshot.content_hash,
            }
        )
        new_id = uuid4()
        await session.execute(
            insert(WikiCompilation)
            .values(
                id=new_id,
                workspace_id=UUID(context.workspace_id),
                knowledge_base_id=kb_id,
                source_snapshot_id=snapshot.snapshot_id,
                source_snapshot_hash=snapshot.content_hash,
                identity_hash=identity,
                compiler_spec=spec,
                requested_by=context.user_id,
                status="PENDING",
                attempt_count=0,
                telemetry={},
            )
            .on_conflict_do_nothing()
        )
        row = await session.scalar(
            select(WikiCompilation).where(
                WikiCompilation.workspace_id == UUID(context.workspace_id),
                WikiCompilation.knowledge_base_id == kb_id,
                WikiCompilation.identity_hash == identity,
            )
        )
        if row.id == new_id:
            _audit(session, context, "wiki_compile_request", row)
        await session.commit()
        return row

    async def get(self, session, context, kb_id, compilation_id, permission="knowledge_run"):
        require(context, permission)
        row = await session.scalar(
            select(WikiCompilation).where(
                WikiCompilation.id == compilation_id,
                WikiCompilation.workspace_id == UUID(context.workspace_id),
                WikiCompilation.knowledge_base_id == kb_id,
            )
        )
        if row is None:
            fail("WIKI_NOT_FOUND", "The Wiki compilation was not found.", 404)
        return row

    async def sources(self, session, context, row, require_current=False):
        snapshot = await KnowledgeSnapshotService().resolve_snapshot(
            session, context, row.knowledge_base_id, row.source_snapshot_id
        )
        if snapshot.content_hash != row.source_snapshot_hash:
            fail("WIKI_SNAPSHOT_MISMATCH", "The Wiki source snapshot has changed.")
        items = list(
            await session.scalars(
                select(KnowledgeSnapshotItem.document_revision_id).where(
                    KnowledgeSnapshotItem.workspace_id == row.workspace_id,
                    KnowledgeSnapshotItem.snapshot_id == row.source_snapshot_id,
                    KnowledgeSnapshotItem.knowledge_base_id == row.knowledge_base_id,
                )
            )
        )
        all_members = set(items)
        exclusions = {UUID(value) for value in row.compiler_spec.get("excluded_revision_ids", [])}
        if not exclusions.issubset(all_members):
            fail("WIKI_SOURCE_SCOPE_INVALID", "The frozen Wiki source scope is invalid.")
        revoked = set(
            await session.scalars(
                select(WikiSourceRevocation.document_revision_id).where(
                    WikiSourceRevocation.workspace_id == row.workspace_id,
                    WikiSourceRevocation.knowledge_base_id == row.knowledge_base_id,
                )
            )
        )
        if (revoked & all_members) - exclusions:
            fail("WIKI_SOURCE_REVOKED", "A Wiki source has been permanently revoked.")
        items = [value for value in items if value not in exclusions]
        revisions = list(
            await session.scalars(
                select(DocumentRevision)
                .where(
                    DocumentRevision.workspace_id == row.workspace_id,
                    DocumentRevision.knowledge_base_id == row.knowledge_base_id,
                    DocumentRevision.id.in_(items),
                )
                .with_for_update(read=True)
            )
        )
        if len(items) != len(revisions) or any(
            r.lifecycle_status == "DELETED" or r.ingestion_status != "READY" for r in revisions
        ):
            fail("WIKI_SOURCE_REVOKED", "A Wiki source is unavailable or revoked.")
        if require_current:
            current = await KnowledgeSnapshotService()._current_revisions(
                session, row.workspace_id, row.knowledge_base_id
            )
            superseded = await session.scalar(
                select(Document.id)
                .where(
                    Document.workspace_id == row.workspace_id,
                    Document.knowledge_base_id == row.knowledge_base_id,
                    Document.id.in_([r.document_id for r in revisions]),
                    Document.superseded_by_document_id.is_not(None),
                )
                .limit(1)
            )
            if set(items) != ({r.id for r in current} - revoked) or superseded:
                fail("WIKI_STALE", "Sources changed; compile and publish a new Wiki version.")
        return revisions

    async def pages(self, session, context, kb_id, compilation_id):
        row = await self.get(session, context, kb_id, compilation_id)
        if row.status != "PUBLISHED":
            require(context, "knowledge_edit")
        revisions = await self.sources(session, context, row)
        pages = await self._page_rows(session, row)
        by_id = {
            c["chunk_id"]: c for c in await self._chunks(session, row, [r.id for r in revisions])
        }
        for page in pages:
            for claim in page["claims"]:
                source = by_id.get(claim["chunk_id"])
                if (
                    not source
                    or claim["quote"] not in source["text"]
                    or any(
                        claim[k] != source[k]
                        for k in ("document_id", "document_revision_id", "locator")
                    )
                ):
                    fail("WIKI_INVALID_PROVENANCE", "A Wiki claim lost its source evidence.")
        if row.content_hash and publication_hash(row, pages) != row.content_hash:
            fail("WIKI_CONTENT_INCONSISTENT", "The Wiki failed its integrity check.", 500)
        return pages

    @staticmethod
    async def _page_rows(session, row):
        pages = await session.scalars(
            select(WikiPage)
            .where(
                WikiPage.workspace_id == row.workspace_id,
                WikiPage.knowledge_base_id == row.knowledge_base_id,
                WikiPage.compilation_id == row.id,
            )
            .order_by(WikiPage.page_key)
        )
        return [
            {
                k: getattr(p, k)
                for k in ("page_key", "title", "kind", "markdown", "claims", "relations")
            }
            for p in pages
        ]

    async def validate_binding(
        self, session, context, kb_id, compilation_id, source_snapshot_id, require_current=True
    ):
        row = await self.get(session, context, kb_id, compilation_id)
        if row.status != "PUBLISHED" or row.source_snapshot_id != source_snapshot_id:
            fail(
                "WIKI_BINDING_MISMATCH",
                "A published Wiki and matching source snapshot are required.",
            )
        await self.sources(session, context, row, require_current=require_current)
        await self.pages(session, context, kb_id, compilation_id)
        return row

    async def lint(self, session, context, kb_id, compilation_id):
        row = await self.get(session, context, kb_id, compilation_id)
        issues = []
        try:
            revisions = await self.sources(session, context, row, require_current=True)
        except AgentHubError as e:
            issues.append({"code": e.code, "severity": "error"})
            if e.code != "WIKI_STALE":
                return issues
            revisions = await self.sources(session, context, row)
        if row.status != "PUBLISHED":
            require(context, "knowledge_edit")
        pages = await self._page_rows(session, row)
        chunks = await self._chunks(session, row, [r.id for r in revisions])
        by_id = {c["chunk_id"]: c for c in chunks}
        keys = {p["page_key"] for p in pages}
        seen = set()
        for p in pages:
            for link in p["relations"]:
                if link not in keys:
                    issues.append(
                        {"code": "BROKEN_LINK", "page_key": p["page_key"], "severity": "error"}
                    )
            if not p["claims"]:
                issues.append({"code": "NO_SOURCE", "page_key": p["page_key"], "severity": "error"})
            for c in p["claims"]:
                source = by_id.get(c["chunk_id"])
                if (
                    not source
                    or c["quote"] not in source["text"]
                    or any(
                        c[k] != source[k]
                        for k in ("document_id", "document_revision_id", "locator")
                    )
                ):
                    issues.append(
                        {"code": "INVALID_SOURCE", "page_key": p["page_key"], "severity": "error"}
                    )
                identity = (p["page_key"], c["chunk_id"], c["quote"])
                if identity in seen:
                    issues.append(
                        {
                            "code": "DUPLICATE_CLAIM",
                            "page_key": p["page_key"],
                            "severity": "warning",
                        }
                    )
                seen.add(identity)
                if c["conflict"]:
                    issues.append(
                        {
                            "code": "POSSIBLE_CONFLICT",
                            "page_key": p["page_key"],
                            "severity": "warning",
                        }
                    )
        if row.content_hash and publication_hash(row, pages) != row.content_hash:
            issues.append({"code": "CONTENT_HASH_MISMATCH", "severity": "error"})
        return issues

    async def publish(self, session, context, kb_id, compilation_id):
        require(context, "knowledge_edit")
        row = await self.get(session, context, kb_id, compilation_id)
        # Serialize publication; source revision shared locks prevent mid-read revocation.
        await session.scalar(
            select(KnowledgeBase)
            .where(KnowledgeBase.id == kb_id, KnowledgeBase.workspace_id == row.workspace_id)
            .with_for_update()
        )
        row = await session.scalar(
            select(WikiCompilation)
            .where(WikiCompilation.id == row.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row.status == "PUBLISHED":
            return row
        if row.status != "READY":
            fail("WIKI_NOT_READY", "The compilation is not ready for publication.")
        await self.sources(session, context, row, require_current=True)
        issues = await self.lint(session, context, kb_id, compilation_id)
        if any(i["severity"] == "error" for i in issues):
            fail("WIKI_LINT_FAILED", "Resolve Wiki lint errors before publication.")
        row.status = "PUBLISHED"
        row.published_at = datetime.now(UTC)
        row.published_by = context.user_id
        _audit(session, context, "wiki_publish", row)
        await session.commit()
        return row

    async def retry(self, session, context, kb_id, compilation_id):
        require(context, "knowledge_edit")
        row = await self.get(session, context, kb_id, compilation_id)
        result = await session.execute(
            update(WikiCompilation)
            .where(WikiCompilation.id == row.id, WikiCompilation.status == "FAILED")
            .values(
                status="PENDING",
                attempt_count=0,
                error_code=None,
                lease_token=None,
                lease_expires_at=None,
            )
        )
        if result.rowcount != 1:
            fail("WIKI_RETRY_CONFLICT", "Only failed compilations can be retried.")
        _audit(session, context, "wiki_retry", row)
        await session.commit()

    @staticmethod
    async def _chunks(session, row, revision_ids):
        chunks = await session.scalars(
            select(DocumentChunk)
            .where(
                DocumentChunk.workspace_id == row.workspace_id,
                DocumentChunk.knowledge_base_id == row.knowledge_base_id,
                DocumentChunk.document_revision_id.in_(revision_ids),
            )
            .order_by(DocumentChunk.document_revision_id, DocumentChunk.ordinal)
        )
        return [_chunk_data(c) for c in chunks]


async def claim_job(session, job_id):
    now = datetime.now(UTC)
    token = uuid4().hex
    row = await session.scalar(
        update(WikiCompilation)
        .where(
            WikiCompilation.id == job_id,
            WikiCompilation.attempt_count < MAX_ATTEMPTS,
            or_(
                WikiCompilation.status == "PENDING",
                (WikiCompilation.status == "PROCESSING") & (WikiCompilation.lease_expires_at < now),
            ),
        )
        .values(
            status="PROCESSING",
            lease_token=token,
            lease_expires_at=now + timedelta(seconds=LEASE_SECONDS),
            attempt_count=WikiCompilation.attempt_count + 1,
        )
        .returning(WikiCompilation)
    )
    await session.commit()
    return row


async def renew_job(session, job_id, token):
    result = await session.execute(
        update(WikiCompilation)
        .where(
            WikiCompilation.id == job_id,
            WikiCompilation.status == "PROCESSING",
            WikiCompilation.lease_token == token,
            WikiCompilation.lease_expires_at > func.now(),
        )
        .values(lease_expires_at=datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS))
    )
    await session.commit()
    return result.rowcount == 1


async def execute_job(factory, context, job_id, token, compiler):
    """Persist per-revision work; only an unexpired lease can atomically install pages."""
    service = WikiService()
    async with factory() as session:
        row = await session.scalar(
            select(WikiCompilation).where(
                WikiCompilation.id == job_id,
                WikiCompilation.workspace_id == UUID(context.workspace_id),
                WikiCompilation.lease_token == token,
                WikiCompilation.status == "PROCESSING",
            )
        )
        if row is None:
            return "stale"
        require(context, "knowledge_edit")
        revisions = await service.sources(session, context, row)
        chunks = await service._chunks(session, row, [r.id for r in revisions])
        if len(chunks) > 2000:
            fail("WIKI_CAPACITY_EXCEEDED", "This compilation exceeds the supported chunk limit.")
        by_revision = defaultdict(list)
        for c in chunks:
            by_revision[c["document_revision_id"]].append(c)
        # Withdrawal changes the publication source scope, not extraction behavior.
        spec_hash = extraction_spec_hash(row.compiler_spec)
        all_claims = []
        telemetry = {"reused_revisions": 0, "compiled_revisions": 0, "calls": []}
        for revision in revisions:
            inputs = by_revision[str(revision.id)]
            if not inputs:
                fail("WIKI_SOURCE_EMPTY", "A source revision has no persisted chunks.")
            input_hash = canonical_json_hash({"schema_version": 1, "chunks": inputs})
            cache = await session.scalar(
                select(WikiExtraction).where(
                    WikiExtraction.workspace_id == row.workspace_id,
                    WikiExtraction.document_revision_id == revision.id,
                    WikiExtraction.compiler_hash == spec_hash,
                )
            )
            if cache:
                if cache.input_hash != input_hash:
                    fail("WIKI_SOURCE_MUTATED", "An immutable source revision has changed.")
                payload = cache.payload
                telemetry["reused_revisions"] += 1
            else:
                # Release read transaction during model latency.
                await session.commit()
                payload, usage = await compiler.extract(context, inputs)
                validate_extraction(payload, inputs)
                if not await renew_job(session, job_id, token):
                    return "stale"
                await session.execute(
                    insert(WikiExtraction)
                    .values(
                        id=uuid4(),
                        workspace_id=row.workspace_id,
                        knowledge_base_id=row.knowledge_base_id,
                        document_revision_id=revision.id,
                        compiler_hash=spec_hash,
                        input_hash=input_hash,
                        payload=payload,
                        telemetry=usage,
                    )
                    .on_conflict_do_nothing()
                )
                await session.commit()
                # A concurrent compiler may have won. Always consume the persisted result.
                cache = await session.scalar(
                    select(WikiExtraction).where(
                        WikiExtraction.workspace_id == row.workspace_id,
                        WikiExtraction.document_revision_id == revision.id,
                        WikiExtraction.compiler_hash == spec_hash,
                    )
                )
                if cache.input_hash != input_hash:
                    fail("WIKI_SOURCE_MUTATED", "An immutable source revision has changed.")
                payload = cache.payload
                telemetry["compiled_revisions"] += 1
                telemetry["calls"].append(usage)
            all_claims.extend(validate_extraction(payload, inputs))
            if row.compiler_spec.get("review_manifest") and len(all_claims) > 200:
                fail("WIKI_REVIEW_CAPACITY_EXCEEDED", "This Wiki exceeds the reviewed claim limit.")
        all_claims.sort(
            key=lambda c: (
                c["document_revision_id"],
                c["chunk_id"],
                c["quote"],
                c["topic"],
                c["predicate"],
            )
        )
        if row.compiler_spec.get("review_manifest"):
            await session.commit()
            if not await renew_job(session, job_id, token):
                return "stale"
            groups, review_usage = await compiler.review(context, all_claims)
            apply_review_groups(all_claims, groups)
            telemetry["conflict_review"] = review_usage
            telemetry["conflict_group_count"] = len(groups)
        pages = organize(all_claims)
        # Lock and fence before installing output. No partial page set ever becomes READY.
        owned = await session.scalar(
            select(WikiCompilation)
            .where(
                WikiCompilation.id == job_id,
                WikiCompilation.lease_token == token,
                WikiCompilation.status == "PROCESSING",
                WikiCompilation.lease_expires_at > func.now(),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if owned is None:
            await session.rollback()
            return "stale"
        await service.sources(session, context, owned)
        for p in pages:
            session.add(
                WikiPage(
                    workspace_id=owned.workspace_id,
                    knowledge_base_id=owned.knowledge_base_id,
                    compilation_id=owned.id,
                    **p,
                )
            )
        # Install while the parent is PROCESSING; SQLAlchemy otherwise flushes the
        # parent UPDATE before child INSERTs. The enclosing transaction stays atomic.
        await session.flush()
        owned.content_hash = publication_hash(owned, pages)
        owned.telemetry = telemetry
        owned.status = "READY"
        owned.lease_token = None
        owned.lease_expires_at = None
        await session.commit()
        return "ready"
