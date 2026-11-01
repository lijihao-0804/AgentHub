"""One offline mechanism probe; never a RAG quality benchmark."""

import json
import sys
from uuid import UUID

sys.path.insert(0, sys.argv[1])
from packages.agent_runtime.context_budget import (
    ContextBudgetConfig,
    ContextCategory,
    ContextMessage,
)
from packages.model_gateway.contracts import ModelMessage
from tests.unit.test_m4d_context_budget import _policy

entry = {
    "id": str(UUID(int=1)),
    "kind": "CONSTRAINT",
    "content": "Background " * 14 + "Refunds require approval; never auto-refund after 30 days.",
}
result = _policy(
    context=1000,
    max_tokens=1,
    config=ContextBudgetConfig(reserved_output_tokens=1, max_memory_tokens=160),
).admit(
    [
        ContextMessage(
            ModelMessage("system", json.dumps({"memories": [entry]})), ContextCategory.MEMORY
        ),
        ContextMessage(
            ModelMessage("user", "Explain the refund rule."), ContextCategory.CURRENT_USER_TASK
        ),
    ]
)
records = []
for message, category in zip(result.messages, result.message_categories, strict=True):
    if category is ContextCategory.MEMORY:
        payload = json.loads(message.content)
        records.extend(payload.get("memories", []))
print(
    json.dumps(
        {
            "frozen_candidates": 1,
            "complete_entries": sum(record == entry for record in records),
            "partial_entries": sum(record != entry for record in records),
            "final_memory_messages": sum(
                category is ContextCategory.MEMORY for category in result.message_categories
            ),
            "estimated_input_after": result.usage.estimated_input_after,
            "memory_budget": 160,
        }
    )
)
