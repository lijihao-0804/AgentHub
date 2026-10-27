# Memory M5：缓存能力与稳定表示（进行中）

本轮在现有 LiteLLM 适配器中，对历史工具调用的 JSON 参数按对象键稳定序列化；不改变消息角色、工具调用顺序、数组顺序或上下文预算。发布版本的工具列表本身按照冻结 spec 的工具列表读取，不依据数据库无序查询重新排序。针对无法按混合键排序的历史参数，继续使用原有序列化方式，兼容既有请求；该回退不保证字节稳定。

针对明确的北京 DashScope 地址与 `deepseek-v4.1-flash-1017`，适配器返回“文档支持隐式缓存”，不表示当前账号已命中或部署已核验。阿里云的 [DeepSeek 接口](https://help.aliyun.com/zh/model-studio/deepseek-api)列出该模型支持隐式缓存，[接入域名](https://help.aliyun.com/zh/model-studio/beijing-access-information)列出北京 OpenAI 兼容地址；[缓存说明](https://help.aliyun.com/zh/model-studio/context-cache)指出公共前缀达到 1024 token 只是可能命中的条件。代理地址、模型别名和其他部署一律返回 UNKNOWN，不从兼容协议推断缓存能力。显式缓存的适配状态为 UNKNOWN，适配器不会主动添加缓存标记。

已报告的 `cached_tokens` 是供应商请求使用量，不是本地摘要复用，也不减少模型上下文占用。供应商缓存的账号边界不能代替 AgentHub Workspace 授权。历史用户输入、来源及工具结果仍按现有权限与预算处理，不把它们移入稳定系统前缀。

现有摘要 job 身份包括 Workspace、Thread、触发 Run、模型轮次、来源及水位、权限快照、父版本、策略、提示、模型、计数器和目标预算；它用于同一派发边界去重。由于其中包含触发 Run 和轮次，不能直接称为跨 Run 摘要复用机制。当前运行时也没有选择已发布摘要，来源或权限变化后的跨 Run 失效与复用仍未落地。

网关服务可按当前配置的主备链逐个返回适配器能力判断，无需模型调用；该内部查询不代表已经绑定到历史 Run 的冻结模型配置，也尚未暴露为产品 API。动态历史提示仍在首个 system 消息中，可能随被裁剪轮次数变化，因此不能宣称整个提示前缀稳定。未开展供应商在线缓存实验，命中、延迟、账单及净收益均为 UNVERIFIED；没有启用新缓存或更改现有 Agent 默认配置。工具参数的对象键顺序对旧版本也会改变；虽然 JSON 内容保持等价，模型行为是否变化尚未经过真实模型比较。M5 整体仍为 PARTIAL。

[OpenHands 的 Agent 源码](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/agent/base.py)将固定 system 指令与动态会话信息分为不同内容块；[Deep Agents 的提示构造](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/graph.py)保留已提供的消息及缓存标记。AgentHub 当前的 OpenAI-compatible 消息契约只保存单段文本，不能直接照搬不同供应商的内容块和显式标记。后续要冻结新的提示布局并验证旧版本、工具及预算语义后，再考虑拆开动态提示；这两份源码不证明本项目已经获得缓存命中。
