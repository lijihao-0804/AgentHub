# AgentHub 面试增强 M-I1 完成报告

日期：2026-10-02；开工基线 `a1c51b4`；施工分支 `codex/interview-m-i1`。范围仅为指标与观测基础。配套[契约与口径](AgentHub-M-I1观测契约与指标口径-20261002.md)、[工作清单](AgentHub-面试增强工作清单-20261002.md)。实际源码身份为本报告所在提交。

## 交付

1. **F00**：新 observation/manifest v2，runtime/controlled/synthetic 来源标识；真实候选融合排名与最终 evidence 分离，实际 k/快照/策略可追溯。缺观测未知，空 truth 不自动通过；历史 v1 evaluator 和持久快照保留原语义。每次物化/比较使用独立 registry，避免共享服务被版本或 judge 绑定污染。
2. **F03 契约与验收器**：Support 场景、轨迹约束和业务后置条件；复用持久 Ticket/AgentRun 与 evaluation_run 权限，在工作区内校验数量、客户和状态。交付十条 PostgreSQL 受控场景及原始记录，不用回答文本判业务成功。
3. **F06 采集**：复用 TraceSpan/RetrievalTrace 的局部阶段计时，首字回调与 p50/p95 基础；计入 provider 重试次数的 usage/cost coverage、按币种有效任务成本、未知数及来源计数。费用小计不完整时明确标记，不把未知费用填零。
4. 新增 23 个单位用例、2 个集成用例与 SSE client 回归脚本。两处旧发布/评测集成夹具补齐 v2 最终检索事实。无新依赖、数据库迁移或页面；复用现有 JSONB observation 和指标快照存储。

## 验收门与真实结果

| Gate / 检查 | 结果 | 退出码与证据 |
| --- | --- | --- |
| G0 契约/差异审阅 | 来源、工作区权限、版本与缺失边界已检查 | 本报告及契约；git diff --check 为 0 |
| G1 全量单元 | **934 passed**；191 integration 用例排除 | 0；[unit-status.json](evidence/mi1-20261002/unit-status.json)、[stdout](evidence/mi1-20261002/unit-stdout.txt) |
| G2 全量真实基础设施集成 | **187 passed，4 skipped**；38 warnings | 0；[integration-status.json](evidence/mi1-20261002/integration-status.json)、[stdout](evidence/mi1-20261002/integration-stdout.txt) |
| Ruff packages + tests | 通过 | 0 |
| TypeScript noEmit | 通过 | 0 |
| Next production build | 通过 | 0；[build-status.json](evidence/mi1-20261002/build-status.json) |
| SSE client 五类边界 | 首文本、无文本、取消、失败、可选回调异常隔离通过 | 0；scripts/test_stream_timing.cjs |
| 现有前端七组回归 | Markdown、分页、权限、Agent 会话过滤/深链等通过 | 0；scripts/test_web_review.cjs |
| G3 浏览器 UI / i18n | 本批不适用 | 没有新增页面、展示键或页面调用链 |
| G6 文档/证据/提交 | 契约、清单和受控证据已归档 | 提交与推送结果见 Git 记录 |

G2 使用全新独立库 `agenthub_mi1_accept_20261002`，显式 checkpoint bootstrap + Alembic 29 次迁移后运行。4 个跳过来自 `test_m3b_worker.py`：未设置 AGENTHUB_TEST_REDIS_URL，因此不声称本轮启动了真实 Celery worker。38 个警告保留在原始测试计数中，不包装成零告警。

首轮失败均已记录和处理：pytest 临时目录权限与 Next 子进程的沙箱限制通过项目独立测试目录及提权运行解决；旧夹具缺最终检索观测补齐；复用测试库的待重试 job 污染通过全新库避免；新增历史快照断言改为比较持久读回与 hash，因为现有 materialize/read payload 包装有差异。没有放宽 v2 缺失保护。

## 十条受控证据

[原始 JSON](evidence/mi1-20261002/controlled-business.json) 与 [逐条 Markdown](evidence/mi1-20261002/controlled-business.md) 包含来源、fixture hash、基线 SHA、manifest、持久 ID 和失败检查。

来源：controlled 10，runtime 0，synthetic 0。九条有业务判定，一条缺 Run 为未知；三条满足各自后置条件，六条按故障事实不满足。这是验收器测试集，**不能把 3/10 写成模型业务成功率，也不能把断言通过写成模型质量提升**。

## 本批边界与后续

- 首字服务端起点是 evaluation execution，客户端是 fetch；没有浏览器绘制耗时。无流式文本返回未知，未做 UI 展示或遥测上传。
- 阶段时延是基础采集与聚合，不是负载 SLA。非标准 gateway、judge 额外调用等采集不完整时如实降级；有效成本目前是已知 Run 费用小计，未取得的 provider bill 不补零。
- 十条场景为独立受控持久 fixture，未执行多轮 Support 模型业务，也未访问付费 provider。真实模型质量、固定知识/定价对照、多轮 driver、人工审阅和容量测量留在 M-I3/M-I4。
- M-I2、M-I3、M-I4、M-I5 均未开始；原前端特性清单不整体改为完成。

按项目“一次一个里程碑”规则，本批验收后停止。下一批为 M-I2：反馈→人工纠正→审核→DEV 回归，以及安全工具摘要/固定快照证据查看。
