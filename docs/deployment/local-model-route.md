# 本机模型路由：Magpie 首选、阿里云备用

统一配置在 [model-route.json](../../benchmarks/public_rag/model-route.json)。它复用已有的
`ModelGatewayService`、LiteLLM 适配器、工作区模型档案和 fallback 链。

| 顺序 | API 基址 | 模型 ID |
|---|---|---|
| 首选 | `http://127.0.0.1:3425/v1` | `auto/deepseek-v4.1-flash` |
| 备用 | `.env` 中的 `AGENTHUB_RAG_BASE_URL` | `deepseek-v4.1-flash-1017` |

Magpie 的本机密钥默认值为 `magpie`，可通过 `AGENTHUB_MAGPIE_API_KEY` 覆盖。
阿里云密钥继续读取原来的 `AGENTHUB_RAG_API_KEY`；配置文件和日志不保存该密钥。
只有明确的本机回环地址允许 HTTP，外部接口仍要求 HTTPS。

## 查看和检查

在仓库根目录运行，使用项目的虚拟环境：

```powershell
.\.venv\Scripts\python.exe -m scripts.model_route show
.\.venv\Scripts\python.exe -m scripts.model_route smoke --output .scratch/model-route-smoke.json
```

`smoke` 实际检查普通回答、原生工具调用，以及首选连接被拒绝后转到阿里云。
故障注入只将一次检查请求指向临时的本机未监听端口，不停止 Magpie。
最多 6 次物理派发，每次最多 128 输出 tokens；不执行真实工具动作，不属于 RAG 质量评测。
输入先经过 `ContextBudgetPolicy`。输出记录配置模型和供应商返回的原始模型标识，方便核对网关别名。

主备切换由原有网关负责，每个档案默认一次请求。首选未成功时按既有错误策略尝试备用；
一旦流式输出了文字或工具调用片段，后续错误成为中断，不换模型继续拼接。
能力校验覆盖整条模型链，保留文本、工具、结构化输出、视觉能力与上下文容量限制。

## 接入 AgentHub 工作区

普通工作区指定其 ID 和真实管理员用户：

```powershell
.\.venv\Scripts\python.exe -m scripts.model_route install --workspace-id WORKSPACE_UUID --actor-user-id USER_UUID --output .scratch/installed-model-route.json
```

对于已有本地实验，可传入该实验的 `prepared.json`（读取它的隔离数据库和工作区身份）：

```powershell
.\.venv\Scripts\python.exe -m scripts.model_route install --state benchmarks/public_rag/results/paper-qa-live-20261023/prepared.json --output .scratch/installed-model-route.json
.\.venv\Scripts\python.exe -m scripts.model_route smoke --state benchmarks/public_rag/results/paper-qa-live-20261023/prepared.json --installed .scratch/installed-model-route.json --output .scratch/installed-model-route-smoke.json
```

安装通过现有控制面服务创建加密凭证与 `ModelProfile.fallback_profile_id`，检查实际管理员归属。
相同配置重复安装复用现有记录；修改地址、密钥或模型时创建新的配置记录，保留旧版本引用。
返回的 `primary_profile_id` 可在草稿 Agent 中选择，再正常发布新 AgentVersion。
主备模型链会一起冻结到新版本；已经发布的版本与历史实验不会自动改写。
此操作不建表、不迁移 schema、不批量改其他工作区。

## 后续换模型与评测

修改统一配置中的 `model`、地址或相应环境变量，然后重新安装并发布需要更新的 Agent。
本机配置调用也可直接使用 `load_route` / `build_gateway`，应用层仍负责上下文准入。
旧实验 runner 的固定模型与协议按历史原样保留，新实验使用此路由并绑定新的模型配置。

评测需要记录实际 `ModelResponse.model`、fallback 使用情况、请求失败和未知费用；
发生切换的样本单独报告，不能把混合模型的结果冒充为单个固定模型的准确率。
该连接检查不改变之前论文问答的分数。
