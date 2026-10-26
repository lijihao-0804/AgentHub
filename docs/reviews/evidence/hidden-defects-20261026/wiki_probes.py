from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from packages.agent_runtime.publish import AgentPublishService
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.knowledge.evidence_plan import assemble_evidence_plan
from packages.tools.builtins.query_wiki import _project
from packages.tools.contracts import ToolResult
from packages.wiki.evidence_envelope import envelope_violation
from tests.unit.test_runtime_rag_evidence_projection import (
    _definition as _runtime_definition,
)
from tests.unit.test_runtime_rag_evidence_projection import (
    _graph,
    _observation_state,
)


def _context() -> WorkspaceExecutionContext:
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(request_id="audit", trace_id="audit"),
            organization_id=str(uuid4()),
            org_role="OWNER",
        ),
        workspace_id=str(uuid4()),
        workspace_role="DEVELOPER",
        permissions=frozenset({"knowledge_run"}),
    )


def _record(*, origin: str, index: int) -> dict:
    value = {
        "origin": origin,
        "chunk_id": f"chunk-{index}",
        "document_id": str(uuid4()),
        "document_revision_id": str(UUID(int=index + 1)),
        "locator": {"page": 1},
        "quote": f"quote-{index}",
        "snippet": f"quote-{index}",
        "conflict": origin == "WIKI_DERIVED",
    }
    if origin == "WIKI_DERIVED":
        value.update(
            {
                "publication_id": str(uuid4()),
                "publication_hash": "a" * 64,
                "wiki_page_id": f"page-{index}",
                "source_revisions": [str(UUID(int=index + 1))],
            }
        )
    return value


def _compilation() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        knowledge_base_id=uuid4(),
        source_snapshot_id=uuid4(),
        content_hash="a" * 64,
    )


def test_hybrid_raw_dropped_after_ten_wiki_records() -> None:
    compilation = _compilation()
    evidence = [_record(origin="WIKI_DERIVED", index=i) for i in range(1, 11)]
    evidence.append(_record(origin="RAW", index=11))
    projected = _project(
        {
            "route": "HYBRID",
            "insufficient_evidence": False,
            "supplemental_required": False,
            "conflicts": [],
            "evidence": evidence,
        },
        compilation,
    )
    assert projected["route"] == "HYBRID"
    assert {item["origin"] for item in projected["results"]} == {"WIKI_DERIVED"}
    assert len(projected["results"]) == 10


def test_runtime_projection_drops_insufficiency_and_conflict_status() -> None:
    definition = _runtime_definition(identity="query_wiki")
    compilation = _compilation()
    projected = _project(
        {
            "route": "HYBRID",
            "insufficient_evidence": True,
            "supplemental_required": True,
            "conflicts": [{"chunk_id": "chunk-1"}],
            "evidence": [_record(origin="WIKI_DERIVED", index=1)],
        },
        compilation,
    )
    graph = _graph()
    state = _observation_state(
        graph,
        definition=definition,
        payload={"status": "SUCCESS", "trust": "UNTRUSTED", "data": projected},
    )
    assert isinstance(state["executed_observations"]["call-1"], ToolResult)
    update = __import__("asyncio").run(graph.observation(state))
    visible = json.loads(update["messages"][-1].content)
    assert visible["status"] == "SUCCESS"
    assert "route" not in visible
    assert "insufficient_evidence" not in visible
    assert "supplemental_required" not in visible
    assert "conflict" not in visible["results"][0]


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)


class _Session:
    def __init__(self, *, binding, rows):
        self.binding = binding
        self.rows = rows
        self.limit_seen = None

    async def scalar(self, _statement):
        return self.binding

    async def scalars(self, statement):
        limit = statement._limit_clause.value
        self.limit_seen = limit
        return _Rows(self.rows[:limit])


class _StubWiki:
    async def sources(self, _session, _context, row, require_current=False):
        del require_current
        if getattr(row, "revoked", False):
            raise AgentHubError("WIKI_SOURCE_REVOKED", "revoked")
        return []


def test_latest_scan_limit_skips_live_eleventh_candidate(monkeypatch) -> None:
    import packages.agent_runtime.publish as publish_module

    monkeypatch.setattr(publish_module, "WikiService", _StubWiki)
    kb_id = uuid4()
    binding = SimpleNamespace(
        binding_mode="LATEST",
        knowledge_base_id=kb_id,
        wiki_publication_id=None,
        publication_hash=None,
    )
    rows = [SimpleNamespace(id=uuid4(), content_hash=f"{i:064x}", revoked=True) for i in range(10)]
    live = SimpleNamespace(id=uuid4(), content_hash="f" * 64, revoked=False)
    rows.append(live)
    session = _Session(binding=binding, rows=rows)
    with pytest.raises(AgentHubError) as error:
        __import__("asyncio").run(
            AgentPublishService()._resolve_wiki_binding(
                session, _context(), SimpleNamespace(workspace_id=uuid4(), id=uuid4())
            )
        )
    assert session.limit_seen == 10
    assert rows[10] is live
    assert error.value.code == "WIKI_BINDING_UNRESOLVED"


def test_evidence_plan_ignores_semantic_status_fields() -> None:
    compilation = _compilation()
    projected = _project(
        {
            "route": "HYBRID",
            "insufficient_evidence": True,
            "supplemental_required": True,
            "conflicts": [{"chunk_id": "chunk-1"}],
            "evidence": [_record(origin="WIKI_DERIVED", index=1)],
        },
        compilation,
    )
    refs = projected["evidence_refs"]
    plan = assemble_evidence_plan(
        projected["results"],
        refs,
        budget=8_000,
        estimator=len,
        record_validator=envelope_violation,
        allow_shared_references=True,
    )
    assert plan is not None
    assert plan.payload["status"] == "SUCCESS"
    assert "route" not in plan.payload
    assert "insufficient_evidence" not in plan.payload
