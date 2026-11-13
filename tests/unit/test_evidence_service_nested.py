import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from packages.agent_runtime.events import AgentEventType
from packages.observability.evidence import RunEvidenceService


@pytest.mark.asyncio
async def test_run_evidence_service_deduplicates_persisted_references():
    ws_id = uuid4()
    kb_id = str(uuid4())
    rev_id = str(uuid4())
    snap_id = str(uuid4())

    class FakeSession:
        async def scalar(self, query):
            return SimpleNamespace(id=uuid4(), agent_version_id=uuid4(), workspace_id=ws_id)

        async def scalars(self, query):
            rows = [
                SimpleNamespace(
                    sequence=1,
                    event_type=AgentEventType.TOOL_COMPLETED,
                    request_id="req-1",
                    event_id=str(uuid4()),
                    step_id="step-1",
                    occurred_at=datetime.now(UTC),
                    payload={
                        "tool_call_id": "call-1",
                        "tool_identity": "search_knowledge",
                        "status": "SUCCESS",
                        "duration_ms": 100,
                        "evidence_refs": json.dumps([
                            {
                                "snapshot_id": snap_id,
                                "knowledge_base_id": kb_id,
                                "document_revision_id": rev_id,
                                "chunk_id": "c-1",
                            },
                            {
                                "snapshot_id": snap_id,
                                "knowledge_base_id": kb_id,
                                "document_revision_id": rev_id,
                                "chunk_id": "c-1",
                            },
                            {
                                "snapshot_id": snap_id,
                                "knowledge_base_id": kb_id,
                                "document_revision_id": rev_id,
                                "chunk_id": "c-2",
                            },
                        ]),
                    },
                )
            ]
            return iter(rows)

    context = SimpleNamespace(
        permissions=frozenset({"workspace_read", "knowledge_run"}),
        workspace_id=str(ws_id),
    )
    service = RunEvidenceService()
    result = await service.tools(FakeSession(), context, uuid4())

    assert len(result["events"]) == 1
    assert len(result["evidence_refs"]) == 2
    assert result["evidence_refs"][0]["chunk_id"] == "c-1"
    assert result["evidence_refs"][1]["chunk_id"] == "c-2"
