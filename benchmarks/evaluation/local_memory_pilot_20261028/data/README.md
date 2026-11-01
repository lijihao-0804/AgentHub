# Local Memory Pilot：公开材料包

## 用途与边界

此包为 AgentHub 的一次本地摘要与事实回忆试验准备一个公开技术材料、两个固定任务和独立评分表。它是单文档开发样本，不是公开 benchmark 的总体成绩，不读取 holdout，也不复用 QMSum 的失败题目。准备材料时未调用模型 API、未修改生产代码、未运行测试、未提交 Git。

## 来源与摘段范围

- **来源身份：** Apache Spark `v3.5.9` 的 `docs/structured-streaming-programming-guide.md`，官方固定版本文件：[GitHub v3.5.9 原文](https://github.com/apache/spark/blob/v3.5.9/docs/structured-streaming-programming-guide.md)。
- **本包的文档子任务：** 原文连续第 3937–4117 行，从 “Recovering from Failures with Checkpointing” 到 “Switching the setting off” 的末段；下一节 “Continuous Processing” 从原文第 4119 行开始，未纳入。这里明确是连续章节摘段，不代表整篇指南。
- `source_document.md` 保留该范围的原始 Markdown 正文与空行，未给每行添加编号。引用坐标均使用上游完整文件的一基行号；本地摘段第 1 行对应上游第 3937 行，换算关系为“本地行号 + 3936”。
- **许可：** Apache License 2.0；同版本的 `LICENSE` 与 `NOTICE` 随包保存。材料来自公开软件技术文档，不含私有会话或账号资料。

## 固定小型工作集

`source_document.md` 共 181 行、10,108 UTF-8 字节、1,325 个空白分隔词。使用 scratch 中已有的 DeepSeek V4.1 固定 tokenizer revision `9f9818127fdfbb60feba204799d617a874b32a86`、项目 `DeepseekV41TextCounter`，按 `thinking=False` 的单条 user 消息格式离线计数，原文为 **2,276 tokens**。`summary_request.txt` 是可直接用于摘要阶段的完整 user 消息正文；按同一消息格式计入 tokenizer framing 后为 **2,400 tokens**，未计任何运行时 system prompt 或额外上下文。

工作集固定为 `tasks.md` 中的两道事实问答。两题分别覆盖 checkpoint 恢复与异步进度跟踪，以及同一 checkpoint 下的配置变更分类。它用于检验一个约 2.3K-token 原文样本能否触发本地摘要流程，适合 4K/8K/16K 业务工作集；它不代表 64K 长上下文压力或收益。

## 文件清单与使用

- `source_document.md`：被摘要的原文摘段。
- `summary_request.txt`：完整、可直接提交给本地摘要器的单条 user 输入，只含摘要要求与公开原文。
- `tasks.md`：两道固定后续问题；可分别对原文基线和摘要结果提问。
- `gold_scoring.md`：十项独立评分标准、答案要点及上游引用坐标。此文件是评分材料，不要并入摘要请求或候选摘要。
- `LICENSE`、`NOTICE`：同版本上游许可与归属文件。

评分时按 `gold_scoring.md` 十项分别记 0/1；回答需保留对应的条件、例外或否定含义，且引用坐标支持该项。只报告这个固定样本的结果，不外推为公开 benchmark 总体表现。
