# 已结束 RAG 实验的复核边界

本次把最小数据库修复和精简结果带回 `E:\JAVA\AI+agent\AgentHub`。今后默认在此目录用主题分支、小提交开发，不再新建项目副本，除非用户另行要求。已有研究副本仅封存保留，不删除。

本仓库不包含原始第三方题源、完整模型回答、盲评输入、供应商回执、API配置、SQLite运行账本或模型缓存。结果来自研究提交 `577b3878985e0500a326a135b8a8db8706a2f90b`；真实派发发生于 `5fcc53e`，不是本次重新实验。

## 在原目录运行的零网络检查

```powershell
Set-Location 'E:\JAVA\AI+agent\AgentHub'
$ragPython = '.\.venv\Scripts\python.exe'
& $ragPython -B docs/reviews/evidence/rag-crossversion-r2-20261115/verify_results.py
& $ragPython -B -m pytest tests/unit/test_run_step_kind_contract.py tests/unit/test_complementary_search_product.py tests/unit/test_hidden_selector_accounting.py tests/unit/test_hidden_official_pricing.py tests/unit/test_m6a_observability.py -o addopts='' -q -p no:cacheprovider
& $ragPython -B -m ruff check --no-cache packages/agent_runtime/models.py tests/unit/test_run_step_kind_contract.py tests/integration/test_auxiliary_run_step_migration.py tests/support/read_auxiliary_steps_old_checkout.py
```

第一条只读6份派生结果的语义哈希，并从48行失误表重算四臂完整正确、胜平负、正式次数和费用，核对真实PG摘要；不读取凭据、数据库、模型或网络，也不替代原始盲评。43项产品定向单测在原目录通过。Ruff使用`--no-cache`避免改动原项目缓存。

## 历史材料所在位置

本机封存根为 `$env:USERPROFILE\Documents\Codex\2026-11-15\task-2`；路径是本机研究存档，并非新开发工作区。

| 子目录 | 版本 / 用途 |
|---|---|
| `agenthub-crossversion-r2-20261115` | `577b387`，冻结材料、原协议、研究脚本和完整去标识盲评 |
| `agenthub-crossversion-old-20261026` | `a2d47e409569565d22cffd34703d7615f4a1882c`，真实旧代码 |
| `agenthub-run-step-fix-20261115` | `c3ea72b`，最小修复及既有隔离PG验收 |
| `agenthub-crossversion-20261115` | `a532002`，首批15题/11可裁定，已关闭，不能补题 |
| `agenthub-support-scope-r1` | `69f7358`，前序范围/请求项/配准/来源交付/公共语料诊断 |
| `runs/crossversion-r2-20261115` | 已关闭原始账本、回执和trace；禁止进Git，状态文件含合成测试登录信息 |
| `runs/original-local-integration-20261115` | 本次原仓库WIP哈希、原index及已修改源文件备份；不是可提交材料 |

如需法证式复核历史，可只读既有封存目录的 `docs/reviews/rag-crossversion-r2-reproduce-20261115.md` 和原始manifest；不要在原项目复制语料、重新激活paid runner或重新初始化账本。旧两笔UNKNOWN不重发。本次没有新实验或新API请求。

## 数据库验证边界

[原PG专项验收](run-step-kinds-acceptance-20261115.md)覆盖0055升级、新库全迁移、成功/回退两类步骤与Run用量、旧行/索引保留、安全拒绝降级及旧c224只读读取。正式12个C Run的真实持久化对账见本目录结果。
本次仅把同一修复代码整合并在原目录运行定向单测，没有对业务库执行Alembic，也没有重跑新PG实验。需要再次PG验收时，必须显式使用独立测试数据库及既有测试命令；不能把服务启动当迁移步骤。

带新类型行的0056降级会明确拒绝，不能删除或改写历史行绕过。旧代码读取能力不等于完整部署回退；旧聚合timeline可能不显示两类节点，原始steps可查。
