# Memory M5：缓存能力与稳定表示（进行中）

本轮在现有 LiteLLM 适配器中，对历史工具调用的 JSON 参数按对象键稳定序列化；不改变消息角色、工具调用顺序、数组顺序或上下文预算。发布版本的工具列表本身按照冻结 spec 的工具列表读取，不依据数据库无序查询重新排序。针对无法按混合键排序的历史参数，继续使用原有序列化方式，兼容既有请求；该回退不保证字节稳定。

针对明确的北京 DashScope 地址与 `deepseek-v4.1-flash-1017`，适配器返回“文档支持隐式缓存”，不表示当前账号已命中或部署已核验。阿里云的 [DeepSeek 接口](https://help.aliyun.com/zh/model-studio/deepseek-api)列出该模型支持隐式缓存，[接入域名](https://help.aliyun.com/zh/model-studio/beijing-access-information)列出北京 OpenAI 兼容地址；[缓存说明](https://help.aliyun.com/zh/model-studio/context-cache)指出公共前缀达到 1024 token 只是可能命中的条件。代理地址、模型别名和其他部署一律返回 UNKNOWN，不从兼容协议推断缓存能力。显式缓存的适配状态为 UNKNOWN，适配器不会主动添加缓存标记。

已报告的 `cached_tokens` 是供应商请求使用量，不是本地摘要复用，也不减少模型上下文占用。供应商缓存的账号边界不能代替 AgentHub Workspace 授权。历史用户输入、来源及工具结果仍按现有权限与预算处理，不把它们移入稳定系统前缀。

当前仅完成适配器能力查询和工具调用参数稳定化。运行时尚未把该能力集成到完整主备路由的产品输出；动态历史提示仍在首个 system 消息中，可能随被裁剪轮次数变化，因此不能宣称整个提示前缀稳定。未开展供应商在线缓存实验，命中、延迟、账单及净收益均为 UNVERIFIED；也没有启用新缓存或更改现有 Agent 默认行为。M5 整体仍为 PARTIAL。
