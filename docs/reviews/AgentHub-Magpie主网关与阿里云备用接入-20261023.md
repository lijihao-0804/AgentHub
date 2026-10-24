# 本机 Magpie 主网关与阿里云备用接入

2026-10-23。用户明确指定本机Magpie、模型`auto/deepseek-v4.1-flash`，并要求连接失败时使用现有阿里云接口。

**本里程碑完成：配置、工作区档案安装、普通回答/原生工具/真实连接拒绝后的备用接管检查通过。** 不属于新的RAG答案质量实验，旧分数不变。

## 接入方式

项目已有`ModelGatewayService`、`SqlAlchemyModelGateway`、LiteLLM适配器、`fallback_profile_id`、发布时冻结主备链，以及输出开始后禁止切换的保护。本轮复用这些实现，没有新增另一套HTTP重试/切换循环。

新增统一[配置](../../benchmarks/public_rag/model-route.json)与[配置加载入口](../../packages/model_gateway/configured_route.py)。Magpie首选基址为`http://127.0.0.1:3425/v1`、OpenAI Chat Completions协议、模型`auto/deepseek-v4.1-flash`；本机默认密钥magpie，可用`AGENTHUB_MAGPIE_API_KEY`覆盖。阿里云保留原`AGENTHUB_RAG_BASE_URL`和`AGENTHUB_RAG_API_KEY`，备用模型`deepseek-v4.1-flash-1017`。密钥不写入公开JSON或日志。

[安装/检查命令](../../scripts/model_route.py)通过现有控制面服务安装工作区档案和加密凭证；同配置复用，修改配置创建新记录。实际安装在论文问答开发工作区，主模型档案`862ba47b-2d21-4647-b31e-5ff4f4f7f6bd`。主数据库只有历史测试档案，未批量改其他工作区。**已有24个发布版本保持原样；后续Agent选择新档案并正常发布，才能使用冻结的新主备链。** 配置入口也可供本机后续实验直接使用。旧实验runner的固定协议/模型按历史保留。

## 验证

- 配置模块14项定向用例通过：环境覆盖、密钥缺失、外部HTTP限制、工作区隔离、默认单次尝试、主成功/连接错误切换、首个可见片段后禁止切换。
- 原网关5项相关用例通过：整个fallback链的能力校验、首个可见片段前的切换，以及文字/工具片段已可见时禁止切换。
- 经实际持久化SQL网关：Magpie普通回答成功、原生工具调用成功；将一次探测的首选地址临时替换为绑定但未监听的本机端口，收到真实连接失败后由同一网关调用阿里云并得到回答。运行中的Magpie和配置地址不变，未执行真实工具动作。
- 4次物理模型派发尝试：3次成功响应、1次注入连接拒绝。观测357 tokens，货币价格未知，不填0费用。每次输出上限128、总派发上限6；输入经过`ContextBudgetPolicy`。
- 重复安装的profile/credential ID一致；安装前后逐项比对24份既有AgentVersion resolved_spec，均未改变。没有新增哈希测试或全库测试。
- 实际作业exit0、22.156秒；价格表自动获取失败后使用本地备份的日志保留，未据此编造价格。清除失效代理仅作用于本次工具子进程，未修改系统代理。

Magpie请求模型别名为`auto/deepseek-v4.1-flash`，接口原始返回模型字段为`deepseek-v4.1-flash`；阿里云返回`deepseek-v4.1-flash-1017`。`openai/`为LiteLLM路由前缀。该记录核对协议/别名与接口声明，不独立查验后端权重。

主备链沿用现有错误策略，并非仅对某一种错误码切换；一旦流式文字或工具片段已可见，后续失败报中断，不能换模型续写。流式边界由定向测试验证，本轮没有额外执行流式网络故障注入。结构化输出/视觉仍关闭，文本与工具能力、上下文容量均保留原校验。

## 使用与后续评测

换模型修改统一配置，再安装档案、在需要更新的草稿中选择主档案并发布新版本。[操作说明](../deployment/local-model-route.md)包含独立调用和数据库档案两种方式。本次连接验收后停止，不继续扩展模型切换功能。

后续质量评测应绑定新配置，记录实际模型与fallback使用情况；切换样本独立分层，不能与旧阿里云固定模型成绩混算。此前论文问答F1/EM保持原记录。

[机器验收](../../benchmarks/public_rag/results/model-route-20261023/acceptance.json)、[实际请求与用量](../../benchmarks/public_rag/results/model-route-20261023/live-smoke.json)、[安装身份](../../benchmarks/public_rag/results/model-route-20261023/installed.json)、[重复安装](../../benchmarks/public_rag/results/model-route-20261023/installed-repeated.json)、[作业记录](../../benchmarks/public_rag/results/model-route-20261023/job-records/status.json)。
