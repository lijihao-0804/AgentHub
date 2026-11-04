from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from apps.worker.tasks import evaluation as worker
from packages.core.config.settings import Settings
from packages.core.errors.exceptions import AgentHubError
from packages.evaluation.driver_router import EvaluationDriverRouter


class _RuntimeDriver:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def prepare(self, *, run, variant, item):
        self.calls.append({"run": run, "variant": variant, "item": item})
        return "runtime-result"


class _InteractiveDriver:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def prepare(self, *, run, variant, item, case_identity=None):
        self.calls.append(
            {
                "run": run,
                "variant": variant,
                "item": item,
                "case_identity": case_identity,
            }
        )
        return "interactive-result"


def _item(category: str, *, environment_ref: str | None = "support_customer"):
    input_value = {"task": category.lower()}
    if environment_ref is not None:
        input_value["environment_ref"] = environment_ref
    return SimpleNamespace(category=category, input=input_value)


@pytest.mark.asyncio
async def test_router_selects_driver_for_each_item_in_a_mixed_run() -> None:
    workspace_id = uuid4()
    runtime_driver = _RuntimeDriver()
    interactive_driver = _InteractiveDriver()
    router = EvaluationDriverRouter(
        interactive_driver=interactive_driver,
        runtime_driver=runtime_driver,
        sandbox_workspace_ids=frozenset({workspace_id}),
    )
    run = SimpleNamespace(workspace_id=workspace_id)
    variant = SimpleNamespace(id=uuid4())
    identity = object()
    interactive = _item("INTERACTIVE")
    normal = _item("KNOWLEDGE_QA", environment_ref=None)

    results = [
        await router.prepare(
            run=run,
            variant=variant,
            item=item,
            case_identity=identity,
        )
        for item in (interactive, normal, interactive)
    ]

    assert results == ["interactive-result", "runtime-result", "interactive-result"]
    assert [call["item"] for call in interactive_driver.calls] == [interactive, interactive]
    assert all(call["case_identity"] is identity for call in interactive_driver.calls)
    assert runtime_driver.calls == [{"run": run, "variant": variant, "item": normal}]


@pytest.mark.asyncio
async def test_router_does_not_apply_interactive_sandbox_to_other_categories() -> None:
    runtime_driver = _RuntimeDriver()
    interactive_driver = _InteractiveDriver()
    router = EvaluationDriverRouter(
        interactive_driver=interactive_driver,
        runtime_driver=runtime_driver,
        sandbox_workspace_ids=frozenset(),
    )
    run = SimpleNamespace(workspace_id=uuid4())
    normal = _item("TOOL_USE", environment_ref="not_an_interactive_environment")

    result = await router.prepare(
        run=run,
        variant=object(),
        item=normal,
        case_identity=object(),
    )

    assert result == "runtime-result"
    assert len(runtime_driver.calls) == 1
    assert interactive_driver.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("assigned_workspace", "item"),
    [
        (False, _item("INTERACTIVE")),
        (True, _item("INTERACTIVE", environment_ref="unregistered_fixture")),
        (True, _item("INTERACTIVE", environment_ref=None)),
    ],
)
async def test_router_rejects_unassigned_workspace_or_fixture_before_dispatch(
    assigned_workspace: bool,
    item,
) -> None:
    interactive_driver = _InteractiveDriver()
    runtime_driver = _RuntimeDriver()
    allowed_workspace = uuid4()
    run = SimpleNamespace(workspace_id=allowed_workspace if assigned_workspace else uuid4())
    router = EvaluationDriverRouter(
        interactive_driver=interactive_driver,
        runtime_driver=runtime_driver,
        sandbox_workspace_ids=frozenset({allowed_workspace}),
    )

    with pytest.raises(AgentHubError) as raised:
        await router.prepare(
            run=run,
            variant=object(),
            item=item,
            case_identity=object(),
        )

    assert raised.value.code == "EVALUATION_SANDBOX_REQUIRED"
    assert raised.value.status_code == 403
    assert interactive_driver.calls == []
    assert runtime_driver.calls == []


def test_evaluation_sandbox_workspace_setting_is_empty_by_default_and_parses_json(
    monkeypatch,
) -> None:
    monkeypatch.delenv("AGENTHUB_EVALUATION_SANDBOX_WORKSPACE_IDS", raising=False)
    assert Settings(_env_file=None).evaluation_sandbox_workspace_ids == frozenset()

    first = UUID("11111111-1111-1111-1111-111111111111")
    second = UUID("22222222-2222-2222-2222-222222222222")
    monkeypatch.setenv(
        "AGENTHUB_EVALUATION_SANDBOX_WORKSPACE_IDS",
        f'["{first}", "{second}"]',
    )

    assert Settings(_env_file=None).evaluation_sandbox_workspace_ids == frozenset({first, second})


def test_production_gateway_seam_uses_the_existing_credential_cipher(monkeypatch) -> None:
    session = object()
    settings = object()
    trace_sink = object()
    cipher = object()
    construction: list[dict] = []

    class CipherFactory:
        @staticmethod
        def from_settings(actual_settings):
            assert actual_settings is settings
            return cipher

    class Gateway:
        def __init__(self, actual_session, *, credential_cipher, trace_sink):
            construction.append(
                {
                    "session": actual_session,
                    "credential_cipher": credential_cipher,
                    "trace_sink": trace_sink,
                }
            )
            self.credential_cipher = credential_cipher
            self.trace_sink = trace_sink

    monkeypatch.setattr(worker, "ProviderCredentialCipher", CipherFactory)
    monkeypatch.setattr(worker, "SqlAlchemyModelGateway", Gateway)

    gateway = worker._production_model_gateway(
        session,
        settings=settings,
        trace_sink=trace_sink,
    )

    assert isinstance(gateway, Gateway)
    assert construction == [
        {
            "session": session,
            "credential_cipher": cipher,
            "trace_sink": trace_sink,
        }
    ]


@pytest.mark.asyncio
async def test_session_scoped_judge_uses_the_named_production_gateway_seam(
    monkeypatch,
) -> None:
    session = object()
    settings = object()
    gateway = object()
    context = object()
    plan = object()
    observed: list[dict] = []

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_args):
            return False

    def factory():
        return SessionContext()

    def production_gateway(actual_session, *, settings, trace_sink):
        observed.append(
            {
                "session": actual_session,
                "settings": settings,
                "trace_sink": trace_sink,
            }
        )
        return gateway

    class JudgeClient:
        def __init__(self, actual_gateway, actual_context, actual_plan):
            assert actual_gateway is gateway
            assert actual_context is context
            assert actual_plan is plan

        async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
            return f"{system_prompt}:{user_prompt}"

    monkeypatch.setattr(worker, "_production_model_gateway", production_gateway)
    monkeypatch.setattr(worker, "ModelGatewayJudgeClient", JudgeClient)

    result = await worker._SessionScopedJudgeClient(
        factory,
        settings,
        context,
        plan,
    ).complete(system_prompt="system", user_prompt="user")

    assert result == "system:user"
    assert len(observed) == 1
    assert observed[0]["session"] is session
    assert observed[0]["settings"] is settings
    assert observed[0]["trace_sink"] is not None


@pytest.mark.asyncio
async def test_build_driver_shares_runtime_services_and_guards_environment_factory(
    monkeypatch,
) -> None:
    sandbox_workspace_id = uuid4()
    trace_sink = object()
    retriever = object()
    factory = object()
    settings = SimpleNamespace(
        knowledge_rrf_k=60,
        knowledge_min_rerank_score=None,
        knowledge_superseded_rank_penalty=0.0,
        approval_ttl_seconds=3600,
        database_url="postgresql+asyncpg://example/test",
        evaluation_sandbox_workspace_ids=frozenset({sandbox_workspace_id}),
    )

    class Stub:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        async def execute_read(self, *_args, **_kwargs):
            return None

    class AgentRunServiceStub:
        def __init__(self, session_factory, **kwargs):
            self.session_factory = session_factory
            self.model_gateway_factory = kwargs["model_gateway_factory"]
            self.kwargs = kwargs

    approval_services: list[Stub] = []

    class ApprovalServiceStub(Stub):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            approval_services.append(self)

    approval_providers: list = []

    class ApprovalActorProviderStub:
        def __init__(self, session_factory):
            self.session_factory = session_factory
            approval_providers.append(self)

        async def context_for(self, run):
            return SimpleNamespace(run=run)

    class ScriptedScenarioDriverStub:
        def __init__(self, runtime, *, driver_kind):
            self.runtime = runtime
            self.driver_kind = driver_kind

    class InteractiveScenarioDriverStub:
        def __init__(
            self,
            scenarios,
            *,
            context_factory,
            environment_factory,
            approval_context_factory,
        ):
            self.scenarios = scenarios
            self.context_factory = context_factory
            self.environment_factory = environment_factory
            self.approval_context_factory = approval_context_factory

    class RuntimeDriverStub:
        def __init__(
            self,
            agent_run_service,
            context_factory,
            *,
            retriever,
            approval_context_factory,
            judge,
        ):
            self.agent_run_service = agent_run_service
            self.context_factory = context_factory
            self.retriever = retriever
            self.approval_context_factory = approval_context_factory
            self.judge = judge

    environment_calls: list[dict] = []

    class EnvironmentStub:
        def __init__(self, session_factory, *, workspace_id):
            environment_calls.append(
                {"session_factory": session_factory, "workspace_id": workspace_id}
            )

    monkeypatch.setattr(worker, "ProductionTraceSink", lambda: trace_sink)
    monkeypatch.setattr(worker, "production_retrieval_components", lambda _: object())
    monkeypatch.setattr(
        worker,
        "SessionScopedKnowledgeRetriever",
        lambda **_kwargs: retriever,
    )
    monkeypatch.setattr(worker, "ApprovalService", ApprovalServiceStub)
    monkeypatch.setattr(worker, "EvaluationApprovalActorProvider", ApprovalActorProviderStub)
    monkeypatch.setattr(worker, "McpToolExecutor", Stub)
    monkeypatch.setattr(worker, "ToolRuntime", Stub)
    monkeypatch.setattr(worker, "ToolRegistry", Stub)
    monkeypatch.setattr(
        worker,
        "onyx_section_selector_from_settings",
        lambda _settings: object(),
    )
    monkeypatch.setattr(worker, "SqlAlchemyToolAuditSink", Stub)
    monkeypatch.setattr(worker, "ActionRuntime", Stub)
    monkeypatch.setattr(worker, "McpActionExecutor", Stub)
    monkeypatch.setattr(worker, "LangGraphCheckpointAdapter", Stub)
    monkeypatch.setattr(worker, "SqlAlchemyMemoryStore", Stub)
    monkeypatch.setattr(worker, "AgentRunService", AgentRunServiceStub)
    monkeypatch.setattr(worker, "ScriptedScenarioDriver", ScriptedScenarioDriverStub)
    monkeypatch.setattr(worker, "InteractiveScenarioDriver", InteractiveScenarioDriverStub)
    monkeypatch.setattr(worker, "AgentRuntimeEvaluationDriver", RuntimeDriverStub)
    monkeypatch.setattr(worker, "SupportFixtureEnvironment", EnvironmentStub)

    driver = await worker._build_driver(settings, factory)
    outer_scripted = driver.interactive_driver.scenarios
    agent_run_service = outer_scripted.runtime

    assert isinstance(driver, EvaluationDriverRouter)
    assert agent_run_service.session_factory is factory
    assert outer_scripted.driver_kind == "controlled"
    assert driver.runtime_driver.agent_run_service is agent_run_service
    assert driver.interactive_driver.context_factory is driver.runtime_driver.context_factory
    assert driver.runtime_driver.retriever is retriever
    assert agent_run_service.kwargs["approval_service"] is approval_services[0]
    assert (
        driver.runtime_driver.approval_context_factory.__self__
        is driver.interactive_driver.approval_context_factory.__self__
    )
    assert driver.runtime_driver.approval_context_factory.__self__ is approval_providers[0]

    gateway_calls: list[dict] = []
    gateway = object()

    def production_gateway(session, *, settings, trace_sink):
        gateway_calls.append(
            {
                "session": session,
                "settings": settings,
                "trace_sink": trace_sink,
            }
        )
        return gateway

    monkeypatch.setattr(worker, "_production_model_gateway", production_gateway)
    session = object()
    assert agent_run_service.model_gateway_factory(session) is gateway
    assert gateway_calls == [{"session": session, "settings": settings, "trace_sink": trace_sink}]

    context = SimpleNamespace(workspace_id=str(sandbox_workspace_id))
    interactive = _item("INTERACTIVE")
    environment = driver.interactive_driver.environment_factory(context, interactive)
    assert isinstance(environment, EnvironmentStub)
    assert environment_calls == [
        {"session_factory": factory, "workspace_id": str(sandbox_workspace_id)}
    ]

    rejected = _item("INTERACTIVE", environment_ref="another_fixture")
    with pytest.raises(AgentHubError) as raised:
        driver.interactive_driver.environment_factory(context, rejected)
    assert raised.value.code == "EVALUATION_SANDBOX_REQUIRED"
    assert len(environment_calls) == 1
