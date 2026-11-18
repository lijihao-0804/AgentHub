# 多层表头的检索证据与引用问答

同名列在跨年度、地区或指标组的表格中很常见。基线 `f2822c3ae0ff9d6f0c6a24547c58472142fde3af` 的 MinerU HTML 表解析按列保存最后一级名称。有空白角格的表头还会被当作普通数据。即使检索和重排都把正确的行送到第一位，行内仍可能有多个同名字段，或者没有字段名。

产品修复 `92f691e` 将连续表头中**明确覆盖更窄列范围**的父名称加到子名称前。例如 `Mars / Sold` 和 `Venus / Sold` 随相应数值进入同一个可引用行。单元格仍以源行、列、合并范围和原值偏移定位。跨不同列名的合并值保留原值，不添加猜测的字段名。数据行之后出现的新表头会替换上一组名称。多列展开的派生标签总字符数受解析限额控制。改动影响后续解析的修订，已发布快照无需重建。

## 配对实验

两张外部来源表共十行，其中一张包含分组列标题，另一张包含跨行海报名称。八条查询穷举第一张表的数据单元格，另有两条单层列名对照、四条缺少行或字段的拒答题。问题、源表摘要、目标值与单元格坐标在首次检索前写入协议。两张表来自同一 W3C 教程，数值是教程示例，不能估计财报或客服生产效果。[W3C 原文](https://www.w3.org/WAI/tutorials/tables/irregular/) · [冻结协议](../../benchmarks/retrieval/table_header_protocol.json)

两臂提交相同 MinerU JSON，分别进入独立工作区。基线在原有隔离解析子进程内运行 Git 基线的解析模块，候选运行当前模块。两侧均经过上传 API、入库 worker、PostgreSQL 持久化、冻结快照，以及生产 Playground 的稠密检索、稀疏检索、融合、BGE 重排和前五证据。队列在进程内派发，Qdrant 使用 SDK 本地模式。BGE-M3 的稀疏通道是项目既有 tokenizer 词法适配器。分值按题配对，八个单元格仅对应两条数据行，不能当作八个独立文档。

|指标|基线|多层表头|
|---|---:|---:|
|有答案题正确行进入融合前 20|10/10|10/10|
|有答案题正确行进入重排第一位|10/10|10/10|
|有答案题正确行进入最终前五|10/10|10/10|
|按完整字段名严格抽取，值与物理坐标均正确|2/10|10/10|
|缺行或缺字段时严格抽取拒答|4/4|4/4|

严格抽取器使用预标注的行键和字段名，只在前五行里有唯一匹配时返回值和单元格坐标。八个新增命中定位了字段名缺失的问题。这个读数衡量证据是否足以支持单元格查询，不衡量模型自然语言理解。保留检索默认配置，零改动于排序策略。[逐题逐阶段结果](../../benchmarks/retrieval/results/table-headers-wai-20261118.json)

## 生成问答与失败归因

两臂还通过 `POST /api/v1/knowledge/query` 分别回答相同的 14 题。这个入口自行检索前六条，并执行模型资料解析、上下文预算、严格 JSON、正文引用号与列表一致性、拒答状态校验并将引用映射回持久化的块。实验在本机替换身份上下文、模型生成适配器与任务队列。评分要求回答值唯一正确且至少一条引用回到包含该值的金标单元格。数字比较允许千分位，出现其他数值则失败；未折算英文拼写、缩写单位，也未对每个自由文本断言做人工判读。HTTP 错误单独计为失败；正确拒答要求明确拒答且无引用。冻结的行键、目标字段、值和坐标仅交给评分器。

|本地 0.5B 输出配置|有效 HTTP 200，旧→新|正确值且引对单元格，旧→新|正确拒答，旧→新|
|---|---:|---:|---:|
|生产 PROMPT_JSON，贪心解码|0→0 /14|0→0 /10|0→0 /4|
|生产 JSON_SCHEMA，约束解码|0→0 /14|0→0 /10|0→0 /4|
|相同 JSON_SCHEMA，加通用支持和拒答示例|12→11 /14|0→0 /10|3→3 /4|

第一组两臂各有 13 次未返回有效 JSON、1 次返回 JSON 数组。第二组用 LM Format Enforcer 0.11.3 按生产请求的 JSON schema 过滤可选 token，未向模型提供可选答案或金标证据。两臂共 28 次均产生字典，其中 13 次同时宣称拒答且附带引用，15 次正文引用标记与列表不一致。第三组补充与测试无关的办公时间示例，明确如何输出一致的字段。有效响应增加，但许多有答案题被误拒，仍无正确的正题联合命中。示例仅在实验中、生产预算检查前注入；线上提示与校验均保持原配置。相同两张表经多次分析，本组用于机制诊断。[生产提示模式](../../benchmarks/retrieval/results/table-headers-wai-20261118.json) · [原生 schema 对照](../../benchmarks/retrieval/results/table-headers-wai-schema-20261118.json) · [示例提示对照](../../benchmarks/retrieval/results/table-headers-wai-contract-20261118.json) · [解码器说明](https://github.com/noamgat/lm-format-enforcer)

3B 模型第一次使用原始 schema，28 次请求的服务日志均出现语法编译失败。llama.cpp b7550 对重复次数设有上限，不能编译项目允许最长 8,000 字符的答案字符串，但仍继续生成并返回文本。生产入口随后逐项校验输出。旧表与新表的 HTTP 成功数分别为 7/14、12/14，正题值正确 2/10、8/10，正题值和引用同时正确 2/10、5/10，正确拒答 0/4、1/4。此组反映服务端未强制语法时的表现。新表的海报颜色题答出 Sepia 却引用错行，旧表在这题两个条件均正确。对缺失指标“退货量”，新表仍将售出数字当作退货量，暴露出答案与证据含义不一致。[兼容性负结果](../../benchmarks/retrieval/results/table-headers-wai-3b-20261118.json) · [该版本的语法重复次数检查](https://github.com/ggml-org/llama.cpp/blob/b7550/src/llama-grammar.cpp)

最后一组只将 3B 本地解码器的短答案上限收紧到 256 字符。独立 canary 要求回答未加引号的单词，实际返回通过生产 JSON 对象校验的内容。整组服务日志核对到 29 次本机请求（1 个 canary、28 个正式生成），语法编译失败 0 次，运行退出码 0。两臂使用同一权重、同一示例提示、同一解码及生产校验；只改变表格解析器。[最终逐题结果与解码审计](../../benchmarks/retrieval/results/table-headers-wai-3b-bounded-20261118.json)

|受限解码的 3B 真实问答入口|基线表|多层表头|
|---|---:|---:|
|返回 HTTP 200|5/14|13/14|
|有答案题的回答值正确|1/10|9/10|
|有答案题至少一条引用包含金标源单元格|1/10|7/10|
|有答案题回答值与引用均正确|1/10|7/10|
|缺失条件正确拒答且无引用|1/4|2/4|
|联合通过|2/14|9/14|

按冻结协议的题号，1、2、3、6、7、10、12 新增联合通过，原有两题继续通过。新表仍有五题未过。第 4、8 题给出正确数字却指错行；第 5 题在宣称证据不足时附带引用，被生产入口拒绝；第 13、14 题对缺少退货指标和荧光绿颜色的查询仍作答。旧表九个无效输出均是拒答带引用，新表同类错误一例。两阶段 3B 对照各 28 个正式本地生成；0.5B 三组各 28 笔，另有两组各 28 笔离线诊断重放，加上最终 canary，共 197 次本地生成，均与下述云端尝试分开核算。最终一组每臂仅跑一遍，八条分组数字问题共享两行来源，不据此变更全局问答、拒答或重排配置。

## 云端调用记录

阿里云北京 `qwen-plus-2025-07-28` 非思考定价按输入、输出每百万 token 分别 0.8、2 元估算。每笔按最多 4,096 输入、96 输出另用高档价 4.8、64 元/百万 token 校验，预留 0.03 元。28 笔计划共预留上限 0.84 元。第一笔返回 HTTP 401，未取得 usage，随后停止。实际发送一笔，收到完整用量零笔，保留 0.03 元未确认额，供应商实际扣款未知。没有再次发送、替换凭据或继续剩余题。[官方定价](https://help.aliyun.com/zh/model-studio/qwen-plus) · [脱敏执行记录](../../benchmarks/retrieval/results/table-header-provider-20261118.json)

## 免费复现

准备独立本机 PostgreSQL 数据库 `agenthub_nov18_rag` 并迁移到 head。将 W3C 页保存到忽略目录，脚本会校验两张源表的 SHA-256。模型只读取本地路径。公开生成权重固定为 [Qwen2.5-0.5B-Instruct 的 7ae5576 快照](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct/tree/7ae557604adf67be50417f59c2c2f167def9a775)，加载时校验权重 SHA-256。

```powershell
$env:AGENTHUB_TEST_DATABASE_URL='postgresql+asyncpg://agenthub@127.0.0.1:56419/agenthub_nov18_rag'
$env:AGENTHUB_DATABASE_URL=$env:AGENTHUB_TEST_DATABASE_URL
.venv/Scripts/python -m alembic upgrade head
Invoke-WebRequest https://www.w3.org/WAI/tutorials/tables/irregular/ -OutFile .scratch/wai-irregular.html
.venv/Scripts/python -m benchmarks.retrieval.table_headers --page .scratch/wai-irregular.html --embedding '<BGE-M3 5617a9f 快照>' --reranker '<bge-reranker-v2-m3 953dc6f 快照>' --answer-model '<Qwen2.5-0.5B-Instruct 7ae5576 快照>' --output .scratch/table-headers.json
```

对小模型 `JSON_SCHEMA` 组，在项目隔离目录安装 `lm-format-enforcer==0.11.3`、`interegular==0.3.3`，将目录加入当前进程的 `PYTHONPATH`，再增加 `--output-mode JSON_SCHEMA`。示例组额外传 `--include-citation-examples`。每次应写到新输出路径。`--diagnostics` 可选导出本地公开样例回复用于排查；这些回复和第三方表格不会随项目提交。

较大模型组使用 Qwen 官方 3B 指令模型的 Q4_K_M 文件和 llama.cpp b7550，仅在本机 CPU 上推理。模型 SHA-256 为 `626b4a6678b86442240e33df819e00132d3ba7dddfe1cdc4fbb18e0a9615c62d`。Windows CPU x64 官方程序压缩包 SHA-256 为 `c10e86bc486e2bbb72ba318367215e1512f221d4b347aa86a6a109ef15922da0`。先在独立终端启动下列服务，再将上面的实验命令中 `--answer-model` 替换为 `--gguf '<量化权重路径>' --include-citation-examples`。温度 0、seed 41、最多输出 256 token，两臂完全相同。结束后关闭自己的本机服务。[官方权重快照](https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/tree/7dabda4d13d513e3e842b20f0d435c732f172cbe) · [官方程序校验和](https://github.com/ggml-org/llama.cpp/releases/expanded_assets/b7550)

```powershell
./llama-server.exe -m '<量化权重路径>' --alias Qwen/Qwen2.5-3B-Instruct-GGUF --host 127.0.0.1 --port 56423 --ctx-size 8192 --threads 8 --parallel 1 -ngl 0 2> llama.stderr.log
```

最终受限解码组额外传 `--short-decoder-schema --server-log '<llama.stderr.log绝对路径>'`。适配器仅将 decoder 的答案最大字符数从 8,000 收紧到 256，生产校验仍用原 schema。先单独要求模型输出未加引号的 `BANANA`，必须返回通过生产 JSON 对象校验的内容。随后复核整组服务日志中的请求数和语法编译失败数，防止服务在语法失效时继续给出看似正常的响应。

解析、证据投影、引用问答单测，以及隔离 PostgreSQL 与真实 HTTP Qdrant 的引用路由回归首次组合共 59 项通过。新增宽表头膨胀边界、评分器假阳性和解码器静默失效校验又覆盖四项。真实服务单独使用 Qdrant 1.15.5 与客户端 1.13.3，客户端报版本跨度警告；最初的四项服务回归因默认端口没有 Qdrant 失败，启动独立服务后复核通过。冻结问题配对使用 SDK 本地模式。[解析测试](../../tests/unit/test_knowledge_layout_parser.py) · [评分约束](../../tests/unit/test_table_header_benchmark.py)
