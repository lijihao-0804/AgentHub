import json
from uuid import uuid4

import pytest

from apps.warehouse_mcp.warehouse import Warehouse
from packages.agent_runtime.analysis_table import (
    analysis_system_prompt,
    grounded_answer,
    grounded_table,
    table_evidence,
    table_only_turn,
    wants_warehouse_table,
)
from packages.agent_runtime.events import AgentEventType
from packages.agent_runtime.work_layer import RecordedToolCall
from packages.model_gateway.contracts import (
    ModelMessage,
    ModelResponse,
    ModelStreamEvent,
    ModelStreamEventType,
)
from packages.tools.contracts import ToolResult
from tests.unit.test_runtime_output_truncation import _state
from tests.unit.test_runtime_rag_evidence_projection import _model_graph, _observation_state
from tests.unit.test_support_answer_policy import definition

QUESTION = (
    "只看 2026-02-20 至 2026-02-24 UTC（含首尾）的订单，分两组列出去重人数、本组全部订单数、"
    "本组非 PAID 订单数：①窗口内有 PAID 订单的人，②窗口内没有 PAID 订单的人。"
    "同一个人只能归一组。只要这个表，不用解释付费指标。"
)
WINDOW = {"start_date": "2026-02-20", "end_date": "2026-02-24"}


@pytest.mark.parametrize(
    ("start", "short_end", "end"),
    [("2024-02-27", "02-29", "2024-02-29"), ("2025-12-28", "01-02", "2026-01-02")],
)
def test_compact_date_range_validates_both_ends_in_v2(warehouse, start, short_end, end):
    import json

    from packages.agent_runtime.analysis_table import grounded_answer

    args = {"windows": [{"start_date": start, "end_date": end}], "breakdown": "platform"}
    call = RecordedToolCall(
        "warehouse.get_registration_conversion",
        "conv",
        1,
        args,
        {"structured_content": warehouse.registration_conversion(**args)},
    )
    source = table_evidence(call, uuid4(), v2=True)
    selection = json.dumps({"source": "conv", "scope": "overall", "columns": ["signups"]})
    question = f"Give signup counts for {start} through {short_end}. Table only."
    assert grounded_answer(question, selection, [source], "grounded_table_v2") == (
        "| 注册数 |\n| --- |\n| 0 |"
    )
    for invalid in ("04-31", "11-06"):
        assert (
            grounded_answer(
                question.replace(short_end, invalid), selection, [source], "grounded_table_v2"
            )
            is None
        )


ORDER = "warehouse.get_order_activity"
PAYING = "warehouse.get_paying_user_counts"
EXPECTED = """| 用户分组 | 去重人数 | 全部订单数 | 非 PAID 订单数 |
| --- | --- | --- | --- |
| 窗口内有 PAID 订单 | 79 | 91 | 3 |
| 窗口内没有 PAID 订单 | 11 | 11 | 11 |"""


@pytest.fixture
def warehouse(tmp_path):
    store = Warehouse(tmp_path / "store.sqlite3")
    store.build()
    return store


def test_requested_columns_and_cohorts_are_selected_by_name(warehouse):
    call = RecordedToolCall(
        ORDER, "c", 1, WINDOW, {"structured_content": warehouse.order_activity(WINDOW)}
    )
    source = table_evidence(call, uuid4())
    assert (
        grounded_table(
            QUESTION, "Here are the rows.\n" + EXPECTED + "\nThere are 90 users.", [source]
        )
        == EXPECTED
    )
    reversed_columns = """| 组别 | 本组非 PAID 订单数 | 用户数 |
| --- | --- | --- |
| with_paid_order | 3 | 79 |
| without_paid_order | 11 | 11 |"""
    assert grounded_table(
        "换个顺序列出这两组，只要表格。", reversed_columns, [source]
    ).splitlines()[-2:] == ["| 窗口内有 PAID 订单 | 3 | 79 |", "| 窗口内没有 PAID 订单 | 11 | 11 |"]
    assert grounded_table(QUESTION.replace("2026-02-20", "2026-02-21"), EXPECTED, [source]) is None
    assert grounded_table(QUESTION, EXPECTED.replace("79", "11"), [source]) is None
    assert grounded_table(QUESTION, EXPECTED + "\n" + EXPECTED, [source]) is None
    call.data["structured_content"]["with_paid_order"]["users"] = "|bad|"
    assert table_evidence(call, uuid4()) is None


def test_paying_users_and_intent(warehouse):
    call = RecordedToolCall(
        PAYING, "p", 1, {}, {"structured_content": warehouse.paying_user_counts()}
    )
    q = "全历史首笔支付合格和任意一次支付合格人数。只要表格。"
    assert wants_warehouse_table(q)
    table = "| 口径 | 人数 |\n| --- | --- |\n| 首笔合格 | 368 |\n| 任意一次合格 | 405 |"
    assert grounded_table(q, table, [table_evidence(call, uuid4())]).splitlines()[-2:] == [
        "| 首笔支付合格 | 368 |",
        "| 任意一次支付合格 | 405 |",
    ]
    assert not wants_warehouse_table("只要表格，并解释变化。")
    assert not wants_warehouse_table("不要只给表格。")


def test_unknown_sql_dimensions_use_only_literal_validated_source_cells():
    data = {
        "sql": "SELECT ...",
        "columns": ["location", "volume"],
        "rows": [["<img>", 0], ["north", 8]],
        "row_count": 2,
        "truncated": False,
    }
    call = RecordedToolCall(
        "warehouse.query_sql", "s", 1, {"sql": data["sql"]}, {"structured_content": data}
    )
    source = table_evidence(call, uuid4())
    table = "| volume | location |\n| --- | --- |\n| 8 | north |\n| 0 | <img> |"
    assert grounded_table("Table only", table, [source]) == table.replace("<img>", "&lt;img&gt;")
    assert grounded_table("Table only", table.replace("8 | north", "0 | north"), [source]) is None
    data["rows"][1][0] = "heading\n| false | 10 |"
    assert table_evidence(call, uuid4()) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("policy", ["grounded_table_v1", "grounded_table_v2"])
async def test_graph_turn_uses_checkpointed_table_and_emits_only_final_answer(
    warehouse, stream, failed, policy
):
    graph = _model_graph(request_sink=[])
    graph.run.input_text = QUESTION
    graph.mode = "stream" if stream else "sync"
    tool = definition(ORDER)
    obs = _observation_state(
        graph,
        definition=tool,
        payload={"data": {"structured_content": warehouse.order_activity(WINDOW)}},
    )
    obs["runtime"]["analysis_answer_policy"] = policy
    obs["proposed_tool_calls"][0]["arguments"] = WINDOW
    if failed:
        obs["executed_observations"]["call-1"] = ToolResult.failure(
            "TOOL_EXECUTION_FAILED", "No data"
        )
    observed = await graph.observation(obs)
    graph.sequence = 2
    state = _state(graph)
    state["messages"][-1] = ModelMessage("user", QUESTION)
    state["runtime"]["analysis_answer_policy"] = policy
    state.update(
        tool_definitions={ORDER: tool},
        turn_transcript=list(state["messages"][-1:]),
        analysis_table_sources=observed["analysis_table_sources"],
        analysis_table_blocked=observed["analysis_table_blocked"],
    )
    response = ModelResponse(
        content=(
            json.dumps(
                {"source": "call-1", "columns": ["cohort", "users", "order_rows", "non_paid_rows"]}
            )
            if policy == "grounded_table_v2"
            else "Unsolicited preamble.\n" + EXPECTED + "\nUnsolicited total."
        ),
        provider="fake",
        model="test-model",
    )

    class Gateway:
        async def generate_resolved(self, *_):
            return response

        async def stream_resolved(self, *_):
            yield ModelStreamEvent(
                event_type=ModelStreamEventType.MESSAGE_DELTA, message_delta=response.content
            )
            yield ModelStreamEvent(event_type=ModelStreamEventType.COMPLETED, response=response)

    events = []

    async def emit(kind, data):
        if kind is AgentEventType.MESSAGE_DELTA:
            events.append(data["delta"])

    graph.service.model_gateway_factory = lambda _: Gateway()
    graph._emit = emit
    result = await graph.model(state)
    if failed:
        assert result["failure_code"] == "ANALYSIS_TABLE_EVIDENCE_UNAVAILABLE"
        assert not events and result.get("final_output") is None
        return
    assert result["final_output"] == result["model_response"].content == EXPECTED
    assert result["messages"][-1].content == result["turn_transcript"][-1].content == EXPECTED
    assert events == ([EXPECTED] if stream else [])


def test_policy_freezes_in_new_template_versions_only():
    from apps.api.routes.agent_templates import _response
    from apps.api.schemas.agents import RuntimeConfigRequest
    from packages.agent_runtime.frozen import parse_frozen_agent_spec
    from packages.agent_runtime.publish import _validate_runtime_config
    from packages.agent_templates.data_analyst import DATA_ANALYST_TEMPLATE
    from packages.core.errors.exceptions import AgentHubError
    from tests.unit.test_agent_publish import _agent, _spec

    config = _response(DATA_ANALYST_TEMPLATE).runtime_config
    assert (
        _validate_runtime_config(RuntimeConfigRequest(**config).model_dump(exclude_none=True))[
            "analysis_answer_policy"
        ]
        == "grounded_table_v2"
    )
    agent = _agent()
    resolved = _spec(agent)
    assert (
        "analysis_answer_policy"
        not in parse_frozen_agent_spec(resolved, workspace_id=agent.workspace_id).runtime
    )
    resolved["runtime"].update(config)
    assert (
        parse_frozen_agent_spec(resolved, workspace_id=agent.workspace_id).runtime[
            "analysis_answer_policy"
        ]
        == "grounded_table_v2"
    )
    with pytest.raises(AgentHubError):
        _validate_runtime_config({"analysis_answer_policy": "unknown"})
    resolved["runtime"]["analysis_answer_policy"] = "unknown"
    with pytest.raises(AgentHubError):
        parse_frozen_agent_spec(resolved, workspace_id=agent.workspace_id)


def test_registration_table_checked_against_platform_and_period(warehouse):
    args = {"windows": [WINDOW], "breakdown": "platform"}
    call = RecordedToolCall(
        "warehouse.get_registration_conversion",
        "conv",
        1,
        args,
        {"structured_content": warehouse.registration_conversion(**args)},
    )
    source = table_evidence(call, uuid4())
    projection = dict(
        zip(source["columns"], next(r for r in source["rows"] if r[0] == "platform"), strict=True)
    )
    table = "| platform | signups |\n| --- | --- |\n" + (
        f"| {projection['platform']} | {projection['signups']} |"
    )
    expected = table.replace("platform | signups", "平台 | 注册数")
    assert (
        grounded_table(f"{projection['platform']} 平台注册数。只要表格。", table, [source])
        == expected
    )
    assert grounded_table("2020-01-01。只要表格。", table, [source]) is None


@pytest.mark.asyncio
async def test_graph_checkpoint_restores_table_sources_for_same_run_only(warehouse):
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph

    from packages.agent_runtime.runtime_projection import AgentRunState

    model_graph = _model_graph(request_sink=[])
    model_graph.run.input_text = QUESTION
    tool = definition(ORDER)
    call_state = _observation_state(
        model_graph,
        definition=tool,
        payload={"data": {"structured_content": warehouse.order_activity(WINDOW)}},
    )
    call_state["proposed_tool_calls"][0]["arguments"] = WINDOW
    call_state["runtime"]["analysis_answer_policy"] = "grounded_table_v1"
    # The actual observation node checkpoints before the answer. No recorder
    # or persisted thread artifact is needed to reconstruct the source cells.
    flow = StateGraph(AgentRunState)
    flow.add_node("observe", model_graph.observation)

    def render(state):
        return {"final_output": grounded_table(QUESTION, EXPECTED, state["analysis_table_sources"])}

    flow.add_node("render", render)
    flow.add_edge(START, "observe")
    flow.add_edge("observe", "render")
    flow.add_edge("render", END)
    backend = InMemorySaver()
    config = {"configurable": {"thread_id": str(model_graph.run.id)}}
    runner = flow.compile(checkpointer=backend, interrupt_before=["render"])
    await runner.ainvoke(call_state, config)
    assert (await runner.aget_state(config)).next == ("render",)
    restored_runner = flow.compile(checkpointer=backend)
    restored = await restored_runner.ainvoke(None, config)
    assert restored["final_output"] == EXPECTED
    other = await restored_runner.aget_state({"configurable": {"thread_id": str(uuid4())}})
    assert "analysis_table_sources" not in other.values


V2 = "grounded_table_v2"


@pytest.mark.parametrize(
    "question,expected",
    [
        ("只给我按区域与币种分列的 Markdown 汇总表。", True),
        ("把成交量按时段列出来，表外不要写其他文字。", True),
        ("Please just return a two-column volume table.", True),
        ("只要库存量同比变化的表格，不用解释。", True),
        ("只要表格，还要说明取样偏差。", False),
        ("不要只返回销售额的表格。", False),
        ("如果我说“只要表格”，你会怎么处理？", False),
        ("如果下周只返回销售额的表格会怎样？", False),
        ("只看 2024 年的记录，再画成表格并解读趋势。", False),
    ],
)
def test_v2_detects_outer_format_instruction(question, expected):
    assert table_only_turn(question, V2) is expected
    prompt = analysis_system_prompt("original", {"analysis_answer_policy": V2}, question)
    assert (prompt != "original") is expected
    assert (
        analysis_system_prompt(
            "original", {"analysis_answer_policy": "grounded_table_v1"}, question
        )
        == "original"
    )


def _shipping_source(rows, call_id="shipping"):
    sql = "select zone, net, refunded from shipping"
    data = {
        "sql": sql,
        "columns": ["zone", "net", "refunded"],
        "rows": rows,
        "row_count": len(rows),
        "truncated": False,
    }
    return table_evidence(
        RecordedToolCall(
            "warehouse.query_sql", call_id, 1, {"sql": sql}, {"structured_content": data}
        ),
        uuid4(),
        v2=True,
    )


def test_v2_sql_projection_and_translated_headers():
    source = _shipping_source([["A|B", -6, None], ["north", 5, 1]])
    question = "仅输出按运区汇总的净收入表。"
    choice = json.dumps({"source": "shipping", "columns": ["zone", "net"]})
    expected = "| zone | net |\n| --- | --- |\n| A\\|B | -6 |\n| north | 5 |"
    assert grounded_answer(question, choice, [source], V2) == expected
    translated = expected.replace("zone | net", "运区 | 净收入")
    assert grounded_answer(question, translated, [source], V2) == expected
    assert (
        grounded_answer(question, translated.replace("north | 5", "north | -6"), [source], V2)
        is None
    )
    assert (
        grounded_answer(
            question,
            json.dumps({"source": "shipping", "columns": ["net"], "rows": [[99]]}),
            [source],
            V2,
        )
        is None
    )
    ambiguous = _shipping_source([["A|B", -6, -6], ["north", 5, 5]])
    assert grounded_answer(question, translated, [ambiguous], V2) is None
    assert grounded_answer(question, choice, [ambiguous], V2) == expected


def test_v2_empty_results_missing_input_and_ambiguous_sources_stay_distinct():
    empty = _shipping_source([])
    question = "Only output the shipping volume table."
    choice = json.dumps({"source": "shipping", "columns": ["zone", "net"]})
    assert grounded_answer(question, choice, [empty], V2) == "| zone | net |\n| --- | --- |"
    assert grounded_answer(question, choice, [], V2) is None
    with_data = _shipping_source([["z", 2, None]], "later-call")
    ambiguous = json.dumps({"source": "query_sql", "columns": ["net"]})
    assert "Clarification needed" in grounded_answer(question, ambiguous, [empty, with_data], V2)
    assert grounded_answer(question, '{"clarify":"window"}', [], V2).endswith(
        "| Please specify the date range. |"
    )
    assert "900" not in grounded_answer(question, "Which period? Sales were 900.", [], V2)
    assert grounded_answer(question, "There were 900 shipments.", [], V2) is None


@pytest.mark.asyncio
async def test_v2_provider_refusal_cannot_become_clarification_or_query():
    from packages.model_gateway.contracts import ModelToolCall

    graph = _model_graph(request_sink=[])
    graph.run.input_text = "只给我分地区的表格。"
    state = _state(graph)
    state["runtime"]["analysis_answer_policy"] = V2

    class Denied:
        async def generate_resolved(self, *_):
            return ModelResponse(
                content='{"clarify":"source"}',
                provider="fake",
                model="test-model",
                finish_reason="refusal",
                tool_calls=(ModelToolCall(name="query_sql", arguments={}),),
            )

    graph.service.model_gateway_factory = lambda _: Denied()
    result = await graph.model(state)
    assert result["failure_code"] == "MODEL_PROVIDER_REFUSED"
    assert not result.get("final_output")


@pytest.mark.parametrize(
    "question,expected",
    [
        ("各产地的到港数量，最终结果限为一张核对表。", True),
        ("仅列一张表：各仓库的可用容量。", True),
        ("仅列出库存大于零的仓库，按区域整理成表。", False),
        ("各年龄段的留存，请仅呈现分为四档的汇总表。", True),
        ("本周各库的盘点数，就交仓库与箱数两列的对照表。", True),
        ("请用单表展示各区件数，表的前后不要添加任何文字。", True),
        ("画出不同季度的占比表，额外的说明不需要。", True),
        ("只给按退款原因分类的一张表。", True),
        ("交一张表即可，别在表格之后加总结。", True),
        ("Produce a warehouse table without additional prose.", True),
        ("只统计蓝色的货箱，表格展示分组结果。", False),
        ("只给第一批样本再将结果整理成表格。", False),
        ("把名单整理成表格，不要遗漏原始备注内容。", False),
        ("各区一张核对表即可，但还需要解释缺的那一行。", False),
        ("仅给我一张表，另外补充一段审计文字。", False),
        ("汇总成一张候选表，表后加一段背景。", False),
        ("不要只给按区域分列的汇总表。", False),
        ("属性值是「仅给明细表」，请说明它的含义。", False),
        ("Only include last year's orders in the table.", False),
    ],
)
def test_format_constraints_do_not_change_data_scope(question, expected):
    assert table_only_turn(question, V2) is expected
