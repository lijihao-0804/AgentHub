# 隐藏缺陷修复验证证据

所有路径以仓库根为工作目录；Python 使用项目 `.venv`。SQL 集成使用专用 `agenthub_hidden_defects_20261026_final` 数据库和显式部署 checkpoint bootstrap；迁移轮转仅用命名受限的独立迁移库，不使用业务库。数据库连接通过环境变量传入，不在本目录保存凭据。

- `unit-full-*`：最终全量单测首跑，2077 通过、1 个旧夹具不符合新增工具定义契约，保留退出码 1。
- `unit-fixture-retry-junit.xml`：修复夹具后相关三模块 39 项通过；没有放宽产品校验或删掉原引用绑定断言。按 classname/name 合并复跑结果，2078 个全量用例全部覆盖通过。
- `integration-*`：最终合并的 PostgreSQL 回归，81 项通过，退出码 0。
- `migration-junit.xml`：真实旧数据 0034→head→0034→head、遗留身份隔离与删除行为，1 项通过。
- `validation-summary.json`：包含前端 Node 5 项、TypeScript、迁移 head 与费用调用边界的执行记录。前端命令：`node --experimental-strip-types --test-isolation=none --test apps/web/components/apps/turn-submission.test.mts`；`node apps/web/node_modules/typescript/bin/tsc --noEmit --incremental false --project apps/web/tsconfig.json`。

单测与故障注入不等于真实 provider 准确率评测；没有新增付费模型调用。原审查目录中的缺陷复现探针不改写为通过版本。

- `compatibility-*`：既有运行、流式、checkpoint、可观测性、记忆、反馈、场景与正式评测补验；首跑 60 通过、1 个取消状态回归，退出码 1，保留失败原文。
- `cancellation-fix-junit.xml`：在租约释放前完成已知取消收尾后，28 项通过；原取消用例拓展为无动作 / CLAIMED / UNKNOWN_OUTCOME 三种状态，后两者保持 NEEDS_ATTENTION。
- `runtime-final-unit-junit.xml`：取消收尾修正后相关 36 项单测通过，退出码 0。
- 合并 81 项和既有兼容补验、用三状态取消测试替换原单状态用例后，144 个不同 PostgreSQL 回归用例通过；另有 1 个独立迁移轮转用例。不是把重叠的 28 项全数相加。
- 最终 Ruff check/format 检查退出码均 0，共 91 个本轮 Python 文件。

- CI 同等补验：仓库级 `ruff check .` 与隔离库 `alembic check` 退出码均 0；`web-build-*` 保留 Next.js 生产构建的命令、日志及真实退出码 0。此为本地等价检查，不声明 GitHub Actions 已全部完成。
