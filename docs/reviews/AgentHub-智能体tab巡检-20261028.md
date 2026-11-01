# AgentHub 前端：智能体各 tab 巡检 + LATEST/PINNED 本地化（2026-10-28）

## 智能体详情各 tab 巡检
逐一点开 基础/模型/知识/工具/运行时/记忆/版本：
- 布局与控件正常（模型配置、知识绑定、工具绑定列表、运行时限值、记忆开关、版本）。
- 发现：工具绑定行提示「LATEST 在发布时解析。」及知识绑定说明里仍混用英文
  `LATEST/PINNED` 词条。

## 本地化
- `latestResolves`：`LATEST 在发布时解析。` → `最新修订版本在发布时解析。`
- `bindingModeHint`：用「最新快照 / 固定快照」替换 `LATEST/PINNED`。
- `usedByLatest` / `usedByPinned`：改为「最新快照 · 跟随最新 / 固定快照 · 快照 …」。
- 验证：工具 tab 提示显示「最新修订版本在发布时解析。」，无残留 `LATEST`。

## 提交
- `fix(web): localize LATEST/PINNED tokens in agent hints`
