"""Legacy audit probes. Assertions record observed defects, not desired behavior."""

import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.dialects import sqlite

from packages.agent_runtime.models import AgentRun
from packages.core.errors.exceptions import AgentHubError
from packages.memory.contracts import MemoryCandidate
from packages.memory.store import SqlAlchemyMemoryStore, content_hash
from packages.threads.context import SqlAlchemyThreadContextProvider
from packages.threads.models import ThreadTurn
from packages.threads.service import ThreadService


class Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def commit(self):
        pass


@pytest.mark.asyncio
async def test_orphan_turn_recovers_another_turns_run_via_real_query_predicate():
    workspace_id, thread_id, turn_id, other_run_id, version_id = (uuid4() for _ in range(5))
    now = datetime.now(UTC)
    old_turn = SimpleNamespace(
        id=turn_id,
        created_at=now - timedelta(minutes=10),
        user_input="original question A",
        agent_run_id=None,
    )
    other_run = SimpleNamespace(
        id=other_run_id,
        agent_version_id=version_id,
        status="SUCCEEDED",
        input_text="different question B",
    )
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE agent_runs (id TEXT, workspace_id TEXT, thread_id TEXT, created_at TEXT)"
    )
    db.execute(
        "INSERT INTO agent_runs VALUES (?, ?, ?, ?)",
        (
            other_run_id.hex,
            workspace_id.hex,
            thread_id.hex,
            (now - timedelta(minutes=5)).replace(tzinfo=None).isoformat(" "),
        ),
    )

    class QuerySession(Session):
        async def scalar(self, statement):
            # Preserve the production WHERE/ORDER/LIMIT; project only the ID.
            compiled = statement.with_only_columns(AgentRun.id).compile(
                dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}
            )
            row = db.execute(str(compiled)).fetchone()
            return other_run if row and row[0] == other_run_id.hex else None

    class Service(ThreadService):
        async def resolve_agent_version(self, *_args):
            return SimpleNamespace(id=thread_id), version_id

        async def open_turn(self, *_args, **_kwargs):
            return None

        async def _require_token_turn(self, *_args):
            return old_turn

        async def attach_run(self, _context, *, turn_id, run_id):
            self.attached = (turn_id, run_id)

    service = Service(lambda: QuerySession(), run_service=SimpleNamespace())
    result = await service.submit_turn(
        SimpleNamespace(workspace_id=str(workspace_id)),
        thread_id,
        user_input=old_turn.user_input,
        client_token="token-A",
    )
    db.close()
    assert result.run_id == other_run_id
    assert service.attached == (turn_id, other_run_id)
    assert old_turn.user_input != other_run.input_text


@pytest.mark.asyncio
async def test_history_query_includes_turn_after_current_run():
    workspace_id, thread_id, current_run_id, later_run_id, later_turn_id = (
        uuid4() for _ in range(5)
    )
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE thread_turns (id TEXT, workspace_id TEXT, thread_id TEXT, "
        "agent_run_id TEXT, sequence INTEGER)"
    )
    db.execute(
        "CREATE TABLE agent_runs (id TEXT, workspace_id TEXT, status TEXT, final_output TEXT)"
    )
    db.execute(
        "INSERT INTO thread_turns VALUES (?, ?, ?, ?, ?)",
        (later_turn_id.hex, workspace_id.hex, thread_id.hex, later_run_id.hex, 3),
    )
    db.execute(
        "INSERT INTO agent_runs VALUES (?, ?, ?, ?)",
        (later_run_id.hex, workspace_id.hex, "SUCCEEDED", "future answer"),
    )
    turn = SimpleNamespace(id=later_turn_id, user_input="future question", sequence=3)
    run = SimpleNamespace(id=later_run_id, final_output="future answer")

    class QuerySession(Session):
        async def execute(self, statement):
            compiled = statement.with_only_columns(ThreadTurn.id, AgentRun.id).compile(
                dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}
            )
            matches = db.execute(str(compiled)).fetchall()
            return SimpleNamespace(all=lambda: [(turn, run)] if matches else [])

        async def scalars(self, _statement):
            return []

    provider = SqlAlchemyThreadContextProvider(lambda: QuerySession())
    history = await provider.conversation(
        workspace_id=workspace_id, thread_id=thread_id, before_run_id=current_run_id, max_turns=10
    )
    db.close()
    assert history.turns[0].user_input == "future question"


@pytest.mark.asyncio
async def test_same_source_run_redelivery_inflates_memory_salience():
    workspace_id, agent_id, source_id = uuid4(), uuid4(), uuid4()
    content = "The team deploys every Friday"
    row = SimpleNamespace(content_hash=content_hash(content), salience=1, source_run_id=source_id)

    class MemorySession(Session):
        async def scalars(self, _statement):
            return SimpleNamespace(all=lambda: [row])

    store = SqlAlchemyMemoryStore(lambda: MemorySession())
    for _ in range(2):
        await store.record(
            workspace_id=workspace_id,
            agent_id=agent_id,
            thread_id=None,
            source_run_id=source_id,
            candidates=(MemoryCandidate(content=content, kind="FACT"),),
        )
    assert row.salience == 3


@pytest.mark.asyncio
async def test_stale_stream_turn_with_no_run_cannot_recover(monkeypatch):
    import apps.api.routes.threads as routes

    existing = SimpleNamespace(
        id=uuid4(), agent_run_id=None, created_at=datetime.now(UTC) - timedelta(minutes=10)
    )

    class Service:
        async def resolve_agent_version(self, *_args):
            return SimpleNamespace(id=uuid4()), uuid4()

        async def open_turn(self, *_args, **_kwargs):
            return None

        async def token_turn(self, *_args):
            return existing

    monkeypatch.setattr(routes, "_service", lambda _request: Service())
    monkeypatch.setattr(routes, "get_production_agent_run_service", lambda _request: object())
    with pytest.raises(AgentHubError) as error:
        await routes.stream_turn(
            workspace_id=uuid4(),
            thread_id=uuid4(),
            payload=SimpleNamespace(input_text="question", client_token="token"),
            request=SimpleNamespace(),
            context=SimpleNamespace(),
        )
    assert error.value.code == "THREAD_TURN_IN_PROGRESS"


@pytest.mark.asyncio
async def test_memory_worker_executes_for_actor_now_downgraded_to_viewer(monkeypatch):
    import apps.worker.tasks.memories as worker
    from packages.model_gateway.contracts import ModelResponse

    workspace_id, run_id, actor_id = uuid4(), uuid4(), uuid4()
    run = SimpleNamespace(
        id=run_id,
        status="SUCCEEDED",
        thread_id=uuid4(),
        input_text="The team deploys every Friday",
        agent_version_id=uuid4(),
        created_by=actor_id,
    )
    version = SimpleNamespace(resolved_spec={}, agent_id=uuid4())
    dispatched, written = [], []
    viewer = SimpleNamespace(permissions=frozenset({"workspace_read"}))

    class WorkerSession(Session):
        index = 0

        async def scalar(self, _statement):
            self.index += 1
            return run if self.index == 1 else version

    class Engine:
        async def dispose(self):
            pass

    class Tenant:
        async def get_workspace_access(self, *_args, **_kwargs):
            return SimpleNamespace(context=viewer)

    class Gateway:
        def __init__(self, *_args, **_kwargs):
            pass

        async def prepare_resolved(self, context, *_args, **_kwargs):
            dispatched.append(context.permissions)
            return self

        async def generate_resolved(self, *_args):
            return ModelResponse(
                content='{"memories":[{"content":"The team deploys every Friday",'
                '"kind":"FACT","evidence":"deploys every Friday"}]}',
                provider="fake",
                model="fake",
            )

    class Store:
        def __init__(self, *_args):
            pass

        async def record(self, **kwargs):
            written.append(kwargs)
            return (uuid4(),)

    monkeypatch.setattr(worker, "create_database", lambda _url: (Engine(), lambda: WorkerSession()))
    monkeypatch.setattr(
        worker,
        "parse_frozen_agent_spec",
        lambda *_args, **_kwargs: SimpleNamespace(
            runtime={"memory": {"long_term_memory": True}}, model_plan=object()
        ),
    )
    monkeypatch.setattr(worker, "TenantService", Tenant)
    monkeypatch.setattr(worker, "SqlAlchemyModelGateway", Gateway)
    monkeypatch.setattr(worker, "SqlAlchemyMemoryStore", Store)
    monkeypatch.setattr(
        worker, "ProviderCredentialCipher", SimpleNamespace(from_settings=lambda _s: None)
    )
    await worker._extract_run_memories(
        SimpleNamespace(database_url="unused"),
        workspace_id=workspace_id,
        run_id=run_id,
        request_id="audit",
    )
    assert dispatched == [frozenset({"workspace_read"})]
    assert len(written) == 1


@pytest.mark.asyncio
async def test_artifact_recorder_duplicates_identical_replayed_call():
    from packages.artifacts.recorder import ToolResultArtifactRecorder
    from tests.unit.test_application_artifacts import QUERY_SQL, call

    rows = []

    class ArtifactSession(Session):
        def add_all(self, pending):
            rows.extend(pending)

    recorder = ToolResultArtifactRecorder(lambda: ArtifactSession())
    workspace_id, thread_id, run_id, creator_id = (uuid4() for _ in range(4))
    calls = (call("warehouse.query_sql", QUERY_SQL),)
    for _ in range(2):
        await recorder.record(
            workspace_id=workspace_id,
            thread_id=thread_id,
            run_id=run_id,
            created_by=creator_id,
            calls=calls,
        )
    assert len(rows) == 2
    assert rows[0].content["provenance"] == rows[1].content["provenance"]
