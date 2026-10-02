"""Versioned scenario inputs and labels; labels never enter runtime input."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator


class ScenarioInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: StrictInt
    fixture_kind: Literal["support_customer"]
    user_turns: list[str] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def valid(self):
        if self.schema_version != 1 or any(
            not turn.strip() or len(turn) > 32000 for turn in self.user_turns
        ):
            raise ValueError("INVALID_SCENARIO_INPUT")
        return self


ToolName = Literal["query_customer", "create_ticket"]


class ScenarioExpected(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: StrictInt
    ticket_count: StrictInt = Field(ge=0, le=10)
    approval_decisions: list[Literal["APPROVED", "DENIED"]] = Field(max_length=10)
    required_tools: list[ToolName]
    forbidden_tools: list[ToolName]
    reference_answer: str = Field(min_length=1, max_length=8192)
    answer_available: StrictBool

    @model_validator(mode="after")
    def valid(self):
        if self.schema_version != 1 or set(self.required_tools) & set(self.forbidden_tools):
            raise ValueError("INVALID_SCENARIO_EXPECTED")
        return self
