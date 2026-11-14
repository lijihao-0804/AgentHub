# Why these two interventions

Alibaba Cloud documents the DeepSeek thinking switch as an optional
`enable_thinking` request field, with reasoning separate from the visible
answer. The experiment uses the project's already verified, narrow Beijing
endpoint/model adapter; it does not widen the allowlist based on the docs.
This suggests testing whether the small cap is consumed before a valid answer
can be delivered, but only the actual finish reason and usage establish that
for an observed response.
[DeepSeek API](https://www.alibabacloud.com/help/en/model-studio/deepseek-api) ·
[Chat Completions parameters](https://www.alibabacloud.com/help/en/model-studio/qwen-api-via-openai-chat-completions)

The provider also documents JSON Object and JSON Schema output modes.
Those formats do not prove that the cited text supports the answer. This
configured endpoint had previously refused native schema requests; retain
the explicit PROMPT_JSON and strict local schema/citation checks for these
two experiments so format capability and reasoning/cap changes are not
confounded. Do not remove content validation or parse a fabricated completion
of a truncated JSON object.
[Structured output](https://docs.modelstudio.console.alibabacloud.com/en/model-studio/qwen-structured-output)

The second intervention comes from the actual project configuration:
`benchmarks/public_rag/model-route.json` sets 2,048 for aliyun-rag, whereas
`scripts/aliyun_api_smoke.py` creates smoke profiles capped at 512.
`scripts/model_route.py` already honors the configured budget. The issue is
reuse of a smoke helper by real QA benchmark setup, not a universal 512-token
production default. Separating these setup paths avoids drawing product
conclusions from an unintended test cap. Higher allowance may increase
latency or cause a timeout and must be measured before recommending it.
