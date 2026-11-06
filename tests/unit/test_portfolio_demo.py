"""Keep the optional portfolio adapter confined to its synthetic environment."""

import json
from types import SimpleNamespace

import pytest

from packages.model_gateway.contracts import ModelMessage, ModelRequest
from packages.model_gateway.errors import ModelGatewayError, ModelGatewayErrorCode
from scripts import portfolio_runtime


@pytest.mark.parametrize(
    "host,port,database",
    [
        ("10.0.0.1", 56416, "agenthub_portfolio_test"),
        ("127.0.0.1", 5432, "agenthub_portfolio_test"),
        ("127.0.0.1", 56416, "agenthub"),
    ],
)
def test_portfolio_refuses_another_database(tmp_path, monkeypatch, host, port, database):
    monkeypatch.setattr(portfolio_runtime, "STATE", tmp_path)
    (tmp_path / "private-config.json").write_text(
        json.dumps(
            {
                "mode": "PORTFOLIO_LOCAL_ONLY",
                "env": {"AGENTHUB_DATABASE_URL": f"postgresql://fixture@{host}:{port}/{database}"},
            }
        )
    )
    with pytest.raises(ValueError, match="dedicated loopback"):
        portfolio_runtime.read_config()


@pytest.mark.asyncio
async def test_scripted_adapter_refuses_a_real_model_identity():
    with pytest.raises(ModelGatewayError) as error:
        await portfolio_runtime.IncidentProvider().complete(
            SimpleNamespace(model="some-real-model"),
            None,
            ModelRequest(messages=()),
        )
    assert error.value.code == ModelGatewayErrorCode.MODEL_TARGET_FORBIDDEN


@pytest.mark.asyncio
async def test_scripted_adapter_never_proposes_another_action_after_denial():
    response = await portfolio_runtime.IncidentProvider().complete(
        SimpleNamespace(model=portfolio_runtime.MODEL),
        None,
        ModelRequest(
            messages=(
                ModelMessage(
                    "tool",
                    name="portfolio_rollback_deployment",
                    tool_call_id="fixture-action",
                    content=json.dumps({"error_code": "TOOL_APPROVAL_DENIED"}),
                ),
            )
        ),
    )
    assert not response.tool_calls
    assert "拒绝" in response.content and "未执行" in response.content
