# QMSum M4 公开数据实验准备

日期：2026-10-27
状态：来源与样本已冻结，数据可用；完整上游许可映射与 64K 压力尚未验证。

## 范围与隔离

本次准备独立于 M3，仅面向 `ContextBudgetPolicy` 的 M4 开发实验。实验只使用 QMSum 官方仓库的 `data/ALL/train`，不读取官方 `val`、`test` 或项目内其他题目作为质量分母。每个会议是独立曝光与计分单元；同一会议的转写只以不可信的会议资料进入上下文，不伪造成用户/助手历史，也不把会议建议写成已执行动作。reference 与 relevant span 只可留在离线评分侧，不得进入压缩器、在线选择器或回答上下文。B/C 输出不得用于选题。

本次抓取前，本地仓库中没有找到 QMSum 数据或既往 QMSum 曝光台账；本次是项目内首次纳入该数据。这不能证明 QMSum 未被外部模型预训练或历史非归档会话见过。实验只能称为本地来源隔离的开发集，不能称为盲测、独立确认或 holdout。

## 来源身份

- 官方仓库：<https://github.com/Yale-LILY/QMSum>
- 抓取位置：`.scratch/context-policy-m4-20261027/upstream/QMSum`
- 来源 commit：`83d7768c1f2b4dfeb091385d3dc7e239b8e5bb7e`
- tree：`159791fb5fae5ef625bd0ce4d300fd3d53aca9c8`
- commit 时间：`2023-08-29T09:07:05+08:00`
- 数据发布 commit：`80a490fd504bce28f7e446fae5cef38f8807c290`（`release data`，2021-04-12）
- 固定数据文件：`data/ALL/jsonl/train.jsonl`
- 数据文件 Git blob：`612fb8ca3e2810c85af1821691b38b2961347528`
- README Git blob：`3c0a8f9c0db9f418414bb3b399954e06e85aac06`
- 文件 SHA-256：`4756915227ddea4e9a9f51b20ea28fd0c9ccbda2f87e288b7fa4b08fe144ca59`
- 行数：162 个会议

逐会议 `data/ALL/train/*.json` 的 SHA-256 清单保存在 `.scratch/context-policy-m4-20261027/evidence/qmsum-train-file-sha256.csv`，含 162 条数据记录；清单自身 SHA-256 为 `B1C39F28F2345B54DD7CA164007CD82080D6B22B4B323CD2404F82C222755715`。`.scratch` 被 Git 忽略，以下文件身份用于复现，不依赖提交清单或另造哈希测试。

## 预登记选样

选样规则在查看任何答案、relevant span 或 B/C 输出之前固定：

1. 只在官方 `ALL/train` 内，按官方 Academic、Product、Committee 三个域分别处理。
2. 对域内会议按 transcript 的 `content` 字符总数降序排列；同长度以会议文件名升序打破平局。
3. 每域取前两个会议，共 6 个会议。
4. 每个选中会议按原始顺序取 `specific_query_list[0]` 和 `[1]`，共 12 题，每会议恰为 2 题。
5. 不因 query 文本、答案可答性、reference 长度或 relevant span 调整上述规则。

冻结样本如下。稳定问题 ID 为 `QMSum@83d7768:ALL/train/<meeting>.json#specific_query_list[<index>]`；“query 字符”仅记录问题长度，不公开答案。

| 域 | 会议 | transcript 字符 | 轮次 | speaker 数 | JSON 字节 | 固定问题 ID | query 字符 |
|---|---|---:|---:|---:|---:|---|---|
| Academic | `Bmr003` | 105,550 | 1,832 | 5 | 267,149 | `QMSum@83d7768:ALL/train/Bmr003.json#specific_query_list[0]` | 54 |
| Academic | `Bmr003` | 105,550 | 1,832 | 5 | 267,149 | `QMSum@83d7768:ALL/train/Bmr003.json#specific_query_list[1]` | 54 |
| Academic | `Buw001` | 92,910 | 1,530 | 8 | 230,777 | `QMSum@83d7768:ALL/train/Buw001.json#specific_query_list[0]` | 49 |
| Academic | `Buw001` | 92,910 | 1,530 | 8 | 230,777 | `QMSum@83d7768:ALL/train/Buw001.json#specific_query_list[1]` | 46 |
| Product | `TS3005d` | 55,407 | 1,616 | 4 | 210,456 | `QMSum@83d7768:ALL/train/TS3005d.json#specific_query_list[0]` | 66 |
| Product | `TS3005d` | 55,407 | 1,616 | 4 | 210,456 | `QMSum@83d7768:ALL/train/TS3005d.json#specific_query_list[1]` | 60 |
| Product | `TS3006d` | 53,378 | 1,401 | 4 | 189,287 | `QMSum@83d7768:ALL/train/TS3006d.json#specific_query_list[0]` | 103 |
| Product | `TS3006d` | 53,378 | 1,401 | 4 | 189,287 | `QMSum@83d7768:ALL/train/TS3006d.json#specific_query_list[1]` | 166 |
| Committee | `covid_1` | 138,380 | 338 | 92 | 177,076 | `QMSum@83d7768:ALL/train/covid_1.json#specific_query_list[0]` | 30 |
| Committee | `covid_1` | 138,380 | 338 | 92 | 177,076 | `QMSum@83d7768:ALL/train/covid_1.json#specific_query_list[1]` | 54 |
| Committee | `covid_3` | 113,796 | 336 | 96 | 157,174 | `QMSum@83d7768:ALL/train/covid_3.json#specific_query_list[0]` | 71 |
| Committee | `covid_3` | 113,796 | 336 | 96 | 157,174 | `QMSum@83d7768:ALL/train/covid_3.json#specific_query_list[1]` | 75 |

六个固定会议文件的 SHA-256：

| 会议文件 | SHA-256 |
|---|---|
| `Bmr003.json` | `558840ec51a2b39f62ab3b52b8aabaaf1052e4f98916ee1ed23119299d633e3e` |
| `Buw001.json` | `f37f758d34c50d89d80e8cac381d3dfb11038b4720414e89937f19bcb4be0bf6` |
| `TS3005d.json` | `3a500acfc8ca0112082afef849f16d72e98a0dc87f575469eadd108719a6d5fd` |
| `TS3006d.json` | `644db94b6de612832e6a8ad6dc4bb7c56b75924d40b9a09ec590d5591ef8c1f8` |
| `covid_1.json` | `b93abc12211f6c0a3b691b2595262ee9b2e510a47697e2ac4fbf7dc9b05544c2` |
| `covid_3.json` | `16b2d8d99d9a57da68ad3c2bab0b0d52de4a6e5a3c6a84e5225be8a8e5812dc5` |

12 个 query 的字符长度为 54、54、49、46、66、60、103、166、30、54、71、75；最小 30，中位数 57，均值 69.0，最大 166。选题阶段只查看了 query 文本。随后一次错误的字段核对读取让终端回显了 `Bmr003` 的部分 answer；该暴露发生在冻结之后，没有改变选样、会议顺序或问题索引，也未继续检查任何 selected answer/relevant span。因此这里的“未据答案选题”可复现，但不能表述为操作者从未见过任何答案内容。

## 自然长度

字符数是对 `meeting_transcripts[*].content` 的字符计数；轮次对应 transcript 条目数，speaker 数对应去重后的 `speaker` 值。这些是长度代理，不是模型 tokenizer 计数。官方 `ALL/train` 的 162 个会议如下：

| 范围 | 会议数 | 字符最小/中位/均值/最大 | 轮次最小/均值/最大 | JSON 字节最小/最大 |
|---|---:|---:|---:|---:|
| 全体 | 162 | 4,587 / 39,318 / 44,728.0 / 138,380 | 91 / 545.3 / 1,832 | 20,322 / 267,149 |
| Academic | 41 | 24,544 / 60,481 / 62,171.3 / 105,550 | 245 / 819.6 / 1,832 | — |
| Product | 97 | 4,587 / 29,712 / 28,865.8 / 55,407 | 91 / 514.6 / 1,616 | — |
| Committee | 24 | 46,554 / 73,445.5 / 79,038.9 / 138,380 | 91 / 200.5 / 370 | — |

冻结会议保留了来源自然顺序、原始 speaker 标签和相邻轮次。没有截断、拼接、复制或人工扩写来制造长上下文。样本含长篇 Academic/Committee 会议和中长 Product 会议，但实际模型 token、真实 `B_work`、B 是否可答，以及是否超过 64K 预设触发的真实压力条件均为 `UNVERIFIED`。在完成这些测量前，不能宣称高压压缩收益或 64K 业务效果。

后续离线长度核对：仅将每轮序列化为 `speaker: content` 并以换行连接，调用项目 `ApproxTokenEstimator`，六个会议按上表顺序分别估算为 **31,155、27,533、20,832、19,226、36,815、30,686 tokens**。该值未包含问题、系统提示、工具、协议和输出预留，亦不是供应商 tokenizer 的实测，不能单独判断完整请求是否达到实际 `B_work` 阈值；参考答案与标注片段未参与此测量。

已建立离线试验预算工厂，按既有路由声明的 32,768 总窗口和 2,048 输出预留解析 `GENERAL_64K`，得到本试验 `B_work=29,184`、70% 触发点 20,429、85% 触发点 24,807。该结果是指定配置下的预算事实，不是对供应商模型最大窗口的重新声明；实际 B/C 请求仍需把完整系统消息、问题、证据与协议开销一起准入，并在实际使用量返回后核对。

### 无模型输出的最终预检

在任何本轮 QMSum 答案或摘要派发之前，先用完整会议与冻结问题做离线准入。会议按自然顺序编码为 `[speaker, content]` 数组，整体是一个带有 `UNTRUSTED_MEETING_DATA`、`execution_record=false` 的历史资料组，当前问题放在最后；没有把发言冒充 AgentHub 的真实 user/assistant/tool 执行记录。最初把会议放入 RAG 证据池时，默认 5,000 token 的检索池使八题全部不可用。会议历史实验改用 CONVERSATION 分组后，仍使用同一 `B_work=29,184` 和完整输入，不放大窗口、不截断会议、不修改选题。此前以不同池上限做过离线可行性检查；两种检查都没有模型调用或答案，不能作为质量结果。

最终预检保留原 12 题分母：Committee 4 题按许可排除；Academic 的 Bmr003、Buw001 各 2 题完整输入不能准入；Product 的 TS3005d 两题估算分别为 23,031、23,029 token，TS3006d 两题为 21,165、21,181 token，均未截断且介于预先约定的 70% 与 85% 之间。这些是项目估算值，供应商实际 tokenizer、B 的可答性和 C 的质量仍未知。达到 85% 的完整 B 不派发；同一题的 C 必须与 B 使用相同的输入预算并再次准入。

最终判定有效压力配对时还要看供应商实际 usage：B 须至少达到 70% 且低于 85%，C 也须低于 85%。如果实际 usage 超过估算而触及硬阈值，保留调用和结果，但不把该题算作符合准入条件的压力配对。

该预检直接使用项目 `ContextBudgetPolicy`，把会议文档视为一个完整的历史组；它是用于研究的受控近似，并未经过真实 Thread 的权限读取、M3 全量历史规划与恢复路径，不能当作产品端到端表现。原文、问题及离线答案分离；压缩候选尚未生成，完整账本仍仅含下方一次校准调用。

配对候选在看到模型输出之前固定为 `summary_candidate_zh_v1`，摘要上限 2,048 token、附归属及受保护原文的最终视图上限 4,096 token；摘要请求最大输出 2,048 token，B、C 答案各最大输出 1,024 token。摘要输入沿用完整原始会议，speaker 与自然顺序保留；每题预先选取少量可在原文精确定位的受保护发言，作为 C 的附带原文，保留该发言的完整内容、原始顺序号及 speaker，B 则包含完整会议。主代理在派发前审阅来源并选择原文，不读取 reference、标注范围或模型输出；这不是独立人工盲审，审阅也不保证穷尽所有关键事实，其工作量应作为实验限制报告。会议记录没有 AgentHub 业务动作或服务端引用清单，不能将会议建议提升为已执行动作；研究信封中的空动作/结构化引用清单仅表示这些权威记录不存在，并不表示语料里没有数字、实体或重要事实。研究候选通过机械校验仍不等同生产来源资格或语义保真。

对四题的预分配首个答案臂依次为：TS3005d 的第 1、2 题分别 C、B；TS3006d 的第 1、2 题分别 C、B。若候选不合法，按原分母记录失败或不可用，不更换提示、不补样本。此前只用临时原文摘录验证过摘要请求的离线准入，未用该摘录调用模型；正式受保护摘录在首次摘要派发前另行冻结。

## 许可与上游来源

QMSum 当前 `LICENSE` 为 MIT，Copyright (c) 2021 Yale-LILY；Git blob 为 `3260fade8bfc520cc284adbb4ef2d397c8f7c23e`。许可证 commit 为 `ddaab65d7771f46951c08bacb5578123dbcf297f`（2021-11-13），晚于数据发布；数据发布 commit 的根目录没有 `LICENSE`。MIT 文本限定 “Software”，所以不能据此推导仓库内所有会议转写都可无限制复用。

官方论文 PDF 为 <https://arxiv.org/pdf/2104.05938v1>，本地 SHA-256 为 `D1ED91BCE9597DD6F4E73F8175BD10BF9D080422FFFDB5A334166F22E3000564`。论文将 Product 来源标为 AMI Meeting Corpus，Academic 来源标为 ICSI Meeting Corpus，Committee 来源为 25 场 Welsh Parliament 与 11 场 Parliament of Canada 会议；论文称这些转写按所引上游规则公开且可使用。论文的引用只能支持来源叙述，不能替代逐会议文件映射或独立许可核验。

已核对官方 AMI 与 ICSI 页面。两者均说明 corpus 与 annotations 以 CC BY 4.0 发布，也分别说明 signals/transcription 以及部分 annotations 公开于 CC BY 4.0：

- <https://groups.inf.ed.ac.uk/ami/corpus/license.shtml>
- <https://groups.inf.ed.ac.uk/ami/icsi/license.shtml>

因此，Product/Academic 的域级来源与 CC BY 4.0 上游许可有官方页面证据；仍不能从文件名自动断言每个 QMSum 会议都完整复制了对应上游转写。

Committee 的域级来源来自论文，但独立许可核验未完成：论文引用的 Welsh open-data 页面返回 HTTP 404；Canadian Commons 的 legal/terms 页面返回 HTTP 403。未尝试绕过访问控制、提供凭据或把失败当作拒绝。当前不能声称 Committee 各会议的许可已经逐项确认。

后续找到加拿大下议院的[现行重要声明](https://www.ourcommons.ca/en/important-notices)：对其议事记录的复制许可要求准确且不能冒充官方，并排除以商业目的获取经济利益的复制、分发或使用；可能包含第三方提交材料，不能仅凭该声明确定所有内容的授权。尚未确认所选两场会议与该声明覆盖的原始记录逐一对应，也没有确认将其发送至商业模型服务的许可。因此 Committee 样本仅保留本地准备，不纳入在线实验，除非相关权利和使用方式得到核实。域级许可不足不改变已冻结的原始 12 题名单；可用在线子集及缩小的分母须如实报告。

## 未验证项

- 6 个冻结会议到具体 AMI、ICSI、Welsh Parliament 或 Parliament of Canada 原始会议文件的逐会议映射。
- 选中的 2 个 Committee 会议对应上游来源的具体许可条款与文件身份。
- QMSum 数据字段与上游转写的逐轮一致性和转换损失。
- 模型 tokenizer 后的输入长度、真实 `B_work`、64K 阈值压力、B 可答性和任何压缩收益。
- QMSum 在外部模型预训练或未归档历史会话中的曝光。
- 数据集参与者的隐私授权是否覆盖本次使用；论文的公开使用声明只能作为来源证据，不能代替完整的隐私审计。

## 操作边界

数据准备阶段没有调用模型，没有生成或查看本轮 M4 的 B/C 输出，也没有把会议数据上传到私有或外部服务。冻结选样后的一次证据搜索误命中上游仓库自带的 `model_output/preds.txt` 与 `refs.txt`，终端回显了若干文本片段；该扫描没有用于选题，也没有改变冻结集合。下载和读取仅来自官方 QMSum GitHub 仓库、官方 arXiv 论文及论文所引 AMI/ICSI 官方页面，并只保存于项目 `.scratch`。除本材料外，该准备子任务没有修改生产代码或提交 Git；后续短校验见下文。

## 用户更新的 API 预算

用户允许使用阿里云 API，并把本轮费用预算提高到 **30 元人民币**；这取代旧执行单中的 5 元工程保守上限，原有的 M4/M5 调用数及 token 上限仍保留。准备工作只检查了配置是否存在：主工作区已配置北京地域的阿里云 endpoint 和凭据，未输出凭据值。实际模型仍需在正式派发前核对。

截至 2026-10-27，[阿里云官方价格表](https://help.aliyun.com/zh/model-studio/model-pricing)列出北京地域 `deepseek-v4.1-flash-1017` 忙时标准输入每百万 token 2 元、输出每百万 token 8 元，闲时分别为 1 元和 4 元；输出计费包含思考与回答。实验预留按忙时标准价计算，不预扣缓存折扣或免费额度。价格、部署地域、实际模型、专属工作空间配置及费用字段仍应在派发前验证；若适用价格无法证明则不得声称 30 元为可核实的硬上限。实际账单以供应商为准，未知结果保留预留。上述价格核查本身不涉及模型请求。

随后执行一次无业务数据的短校验请求：单条 user 消息为 `Reply with OK.`，最大输出 64 token、temperature 为 0，未使用工具、重试或 fallback。阿里云返回模型 `deepseek-v4.1-flash-1017`、输入 34 token、输出 16 token。按上述忙时全价估算费用 **0.000196 元**；实际账单未查询，费用仅为估算。此调用计入 M4/M5 合并试验账本的校准次数及总预算，并不代表摘要质量测试。请求前曾有一次 Python 导入路径错误，该错误发生在账本预留及网络派发之前；修正路径后只派发了上述一次请求。账本及脱敏结果保存在项目忽略目录 `.scratch/context-policy-m4-20261027/`，一次性脱敏快照见同目录的 `aliyun-calibration-result.json` 和 `budget-ledger-after-calibration.json`。

## 已冻结摘要候选的首次结果

四道 Product 题各派发了一次预登记摘要请求；模型均为阿里云北京 `deepseek-v4.1-flash-1017`，没有重试或 fallback。供应商均返回 `finish_reason=length`、空内容，最大输出 2,048 token 用尽。四份产物全部标记 `INVALID / PROVIDER_RESPONSE_INCOMPLETE`，不进入 C，不用于替换原文。

| 固定题目 | 摘要请求估算输入 | 实际输入 | 实际输出 | 忙时全价估算 |
|---|---:|---:|---:|---:|
| TS3005d #0 | 25,842 | 30,952 | 2,048 | 0.078288 元 |
| TS3005d #1 | 26,028 | 31,203 | 2,048 | 0.078790 元 |
| TS3006d #0 | 24,188 | 28,927 | 2,048 | 0.074238 元 |
| TS3006d #1 | 23,992 | 28,720 | 2,048 | 0.073824 元 |

四次合计 119,802 输入、8,192 输出 token，忙时全价估算 **0.305140 元**；加上短校验共 **0.305336 元**。缓存输入返回为 0，缓存写入字段缺失；费用是上限式估算，非供应商账单。实际摘要输入比同请求的本地估算高约 19%–20%；前两次的实际输入加预留输出已超过本试验声明的 32,768 token 窗口，尽管供应商接受了请求。因此不能将先前估算视为精确的模型计数，也不能声称该摘要派发符合声明窗口。项目需要模型匹配的 tokenizer 或经验证的保守上界；目前不可从这四个样本外推安全系数。

后续从 [DeepSeek 的 ModelScope 模型仓库](https://modelscope.cn/models/deepseek-ai/DeepSeek-V4.1-Flash-1017)取得公开的 `tokenizer.json`（文件 revision `9f9818127fdfbb60feba204799d617a874b32a86`），核对仓库文件清单中的内容身份后，仅在本地对已冻结消息逐条计数；没有访问参考答案或发送新请求。四次摘要的正文 token 数分别为 30,922、31,173、28,897、28,690；各比供应商总输入少 30 token。四题对应完整 B 消息的正文计数分别为 23,790、23,790、22,047、22,055。这里的正文计数不含角色封装及服务商内部开销；四个摘要出现相同差值仅是有限样本观察，不能作为其他请求的安全保证。B 未实际派发，实际用量仍未知；且四题 C 均无有效候选，即使单独发送 B 也无法形成预登记配对。

进一步下载并审阅同一模型 revision 的官方 `encoding/encoding.py`，在本地将完整消息按 DeepSeek V4.1 思考模式编码，再用同一 tokenizer 计数。四次摘要的本地计数分别为 30,952、31,203、28,927、28,720，均与供应商返回的输入 token **逐项相等**；两次合成诊断在思考和非思考模式下的本地计数也分别与 47、21 的供应商实测相等。按此编码得到四题完整 B 的估算分别为 23,821、23,821、22,078、22,086，均低于实验硬阈值 24,807；由于 B 没有派发，它们仍是离线推算而非供应商实测。六次一致是有限证据，不覆盖不同代理、工具、图片或供应商以后修改模板的情况，也没有接入现有冻结策略；不能把它宣称为通用生产上界。编码器来源与 tokenizer 均来自同一公开模型仓库，来源文件身份在本地核对，原始消息没有外传。

[vLLM 的 DeepSeek V4.1 实现说明](https://github.com/vllm-project/recipes/blob/main/models/deepseek-ai/DeepSeek-V4.1-Flash.yaml)指出默认思考模式下，较小的最大输出可能耗尽于思考，导致 `length` 与空内容；[阿里云 DeepSeek 接口说明](https://help.aliyun.com/en/model-studio/deepseek-api)也区分思考模式及其控制。思考消耗输出预算是本轮失败的合理假设，但本次客户端未保存思考内容或可用的思考 token 字段，不能将其断言为已经证实的原因。

冻结分母保持 12 题：Committee 4 题因许可未确认不在线发送，Academic 4 题预检不能准入，Product 4 题摘要不完整。当前有效 B/C 配对 **0**，尚未派发答案，正确率、语义保真、任务通过率、token 降幅与净收益均为 **UNVERIFIED**，不是 0 分也不是性能提升。根据原协议不替换候选、不重试这四道题，线上部署与生产默认启用均不成立。[逐题脱敏结果](qmsum-outcomes.json)保留原分母、失败状态、计数与费用；原始实验账本及冻结输入位于项目忽略目录 `.scratch/context-policy-m4-20261027/`。

### 独立故障诊断预登记

为了判断当前阿里云路由是否会默认消耗思考 token，另做一次无业务数据的短诊断，不修改正式候选、不复用 QMSum 题目、也不计入质量分母。唯一输入为 `Calculate 987654321 times 123456789. Return only the final number.`，默认模式、temperature 0、输出上限 64 token；事先预留价格，不重试、不 fallback。记录模型、结束原因、输入/输出 token、供应商若返回的 `reasoning_tokens` 数值、无业务内容的短回答及费用；不保存思考内容。即使诊断证实思考 token 存在，也只能证明这次短请求的行为，不能反推四次长请求的确切思考用量或证明替换摘要可行。

**诊断结果：**请求返回 `deepseek-v4.1-flash-1017`，输入 47 token、输出 64 token，供应商的 `reasoning_tokens` 为 64；结果 `finish_reason=length` 且回答为空。该调用忙时全价估算 0.000606 元，总计六次供应商调用估算 **0.305942 元**，未查询实际账单。这次直接证实该路由在默认模式下可能用尽全部输出额度于思考，且能复现空回答；原四次摘要的思考用量未记录，仍不能断言四次的精确原因或补做被禁止的质量比较。

为验证该路由是否真的接受关闭思考模式，再登记一次配对诊断：保持相同合成输入、模型、temperature 和 64 token 上限，仅显式设置 `enable_thinking=false`。检查响应是否完成、是否有输出，以及计算结果是否等于本地整数运算；记录用量和费用，未知结果不重试。此诊断不修改正式候选或重跑任何 QMSum 题，不能作为压缩效果评分，也不能证明关闭思考后的长文质量。

**配对诊断结果：**阿里云接受 `enable_thinking=false`，返回 `finish_reason=stop`，输入 21、输出 7 token，回答与本地整数计算完全相同；`reasoning_tokens` 字段缺失，记为未知而非 0。该次忙时全价估算 0.000098 元，累计七次调用估算 **0.306040 元**。同一简短输入的默认模式实测输入为 47，关闭思考模式后为 21，说明输入开销会随模式改变；这进一步说明不能将长摘要观测到的 30 token 差值硬编码到所有请求。诊断表明下一次独立预注册实验可以显式审查思考模式和输出预算，但本轮 QMSum 质量和收益仍未验证。
