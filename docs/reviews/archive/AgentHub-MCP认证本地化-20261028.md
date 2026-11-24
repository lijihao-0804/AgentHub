# AgentHub 前端：MCP 认证本地化（2026-10-28）

- `/tools/mcp` 连接表「认证」列与新建表单的认证下拉都显示原始 `NONE / BEARER`。
- 改为双语：`NONE → 无 / None`，`BEARER → Bearer 令牌 / Bearer token`（未知值回退原文）。
- 验证：连接表认证列显示「无」，无残留 `NONE`；`tsc` + `next build` 通过。
