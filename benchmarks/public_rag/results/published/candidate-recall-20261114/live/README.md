# 候选召回在真实引用问答中的表现

在 5,183 篇 SciFact 文献的固定快照上，词法补位把 838 的关联论文送进了 20 个候选的第 18 位，重排至第 2 位。该论文讲 NR5A2 与妊娠，无法回答原问题所问的人类逆向胆固醇转运。问答接口识别了证据缺口，返回零引用并拒答。另一道专门询问该研究生殖功能的问题得到正确回答，列出黄体维持、蜕膜化和胎盘形成，引用了同一论文的两段原文。两臂原本都取回了这篇论文，因此这道衍生题不能证明召回带来了答案增益。[逐题答复、实际候选位次、引文与逐项审阅](qa-delivery.json)

| 同快照的首次问答请求 | 题数/臂 | 基线 HTTP 200 | 词法 HTTP 200 | 基线答出且引目标 | 词法答出且引目标 |
| --- | ---: | ---: | ---: | ---: | ---: |
| SciFact 全库，含 1 道合成缺据和 1 道衍生题 | 6 | 3 | 6 | 0 | 2 |
| 42 篇真实上传的干扰子集，含 1 道合成缺据 | 4 | 2 | 3 | 0 | 1 |
| 18 份合成账户文件，固定退款问题 | 1 | 1 | 1 | 0 | 0 |

全库的另一道正确带引文答复是原本检索正确的 791。词法臂答对伴先兆偏头痛与缺血性卒中的关联，并引用实际给出相对风险 2.16 的论文 15984735。基线在 791 和生殖功能衍生题已将目标放在靠前位置，但首次模型输出均未通过引用问答的输出校验。1109 的全库基线则收到网关 `MODEL_BAD_RESPONSE`。在 42 篇子集上，两臂的 1109 都返回 502。在这个分母很小的 DEV 诊断中，不把 502 转为答对、拒答或检索失误。六次 HTTP 错误各保留首次原样结果，补取同快照的检索轨迹时使用不发生模型生成的试验台。[冻结选题和原始判分](subset-protocol.json) · [全库追问与评分补充](full-addendum.json)

818 在全库两臂的 20 个候选里都缺失，在小库词法臂进入第 20 位后又掉出重排前六。即使找回 SCoRe 成像论文，其索引摘要依然不足以直接证明神经信号传导的原断言。合成退款题有明确的 17 天期限和 2025 年 7 月 18 日签署记录，两个检索通道都只把目标放在第 25 位。词法补位新增了另一个账户的文档。两次实际答复均拒绝把其他账户的期限当作当前账户的答案，也都没有交付用户所需的数值及文件。[实际上传的 18 份合成记录和评分要求](product-fixture.json)

独立确认集的 8 个新增文献命中也逐一核对了原始 AllenAI TRAIN 标注和被取回摘要。7 道原题的证据字典为空，另 1 道的唯一证据组指向另一篇论文。8 个新增摘要均不足以直接判定对应原断言。[逐项来源复核](rescue-source-review.json)

## 运行条件

基线由提交 `f02a6fbece9d4aea2280fe59408ad0f363aad5ab` 的应用和包启动，沿用默认 RRF。词法臂使用实验 API，先显式准备快照 BM25 索引，再在同一个 `POST /api/v1/knowledge/query` 请求上增加 `candidate_policy="lexical_refill_v1"`。同一配对固定查询字符串、工作区、知识库快照、模型配置及输出契约。每臂每题只提交一次带模型的 HTTP 请求。22 个问答请求共记录 24 个模型生成调用尝试，均使用配置的北京阿里云 `deepseek-v4.1-flash-1017`、温度 0、最大输出 512 和 `PROMPT_JSON`。2 次额外尝试来自全库词法臂内置的有界引文修正，未出现模型回退。网关日志对 token 字段作了脱敏，未据此推算费用。12 次跨工作区/错快照请求全部为 404，未返回答案或暗号。

全库读侧使用单独的 Qdrant 集合，以免小库改变该集合的稀疏 IDF。先从公开原文生成 5,183 篇文档和 9,189 个当前修订块，再将块号、文本、SHA-256 和定位区间逐块匹配到本机重新计算的 BGE-M3 向量。将真实稠密向量和 tokenizer 的原始 log-TF 向量灌入 Qdrant，精确计数 9,189；PostgreSQL 持有块和作用域成员关系。公开快照接口创建快照后，由真实产品问答入口执行读取、CPU BGE 查询及重排、模型生成和引文验证。这个全库基准使用批量导入的读侧夹具。42 篇/88 块和 18 篇/30 块的两个伴随诊断从头走真实 multipart 上传和 Celery Worker。全库及两份小库共 22 条比较都用同臂的实际 dense/sparse 排序和按相同块身份重建的词法榜独立复算，实际前 20 块顺序 22/22 相符。部分 HTTP 失败按前述方式补读。[全库向量、块及模型的复现资料](../FORENSICS.md) · [准备快照、来源与结果指纹](qa-delivery.json)

## 本机复跑

在仓库根目录从项目虚拟环境执行。程序的 `validate` 仅校验公开语料文件和冻结子集，合成数据随本目录一同保存。[复跑程序](../../../../candidate_recall_live_replay.py)

```powershell
& .\.venv\Scripts\python.exe -m benchmarks.public_rag.candidate_recall_live_replay validate --cohort subset
& .\.venv\Scripts\python.exe -m benchmarks.public_rag.candidate_recall_live_replay validate --cohort product
```

运行 `run` 时传入已有的隔离回环 API 地址，配套数据库完成迁移，Worker 使用相同的 Redis、PostgreSQL、Qdrant 和 blob 根目录。复跑 A/B 要用保存在本地分支 `codex/rag-candidate-recall-lab-20261114`（提交 `467329996bd272f7f4d771c1a92c6f42b78c1dc7`）的 API，数据库完成 `0056_snapshot_word_bm25` 迁移。从包含复跑程序的主线工作区执行以下客户端命令，指向这个实验服务。外部模型沿用仓库配置的北京端点。程序创建空工作区和合成租户对照，先上传并等待就绪，再建同一快照提交选定两臂，结束时停用并换钥本次创建的凭据。`--api-url` 应替换为这套隔离 API 的真实回环地址。`--arms A` 可单独重跑默认路径，`--retrieval-only` 只比较公开检索试验台、不调用外部模型。

```powershell
& .\.venv\Scripts\python.exe -m benchmarks.public_rag.candidate_recall_live_replay run --cohort product --retrieval-only --api-url http://127.0.0.1:8000/api/v1 --output .scratch/recall-product-baseline-new.json
& .\.venv\Scripts\python.exe -m benchmarks.public_rag.candidate_recall_live_replay run --cohort product --arms both --api-url http://127.0.0.1:8000/api/v1 --output .scratch/recall-product-new.json
```

运行双臂时，从主线工作目录执行复跑程序，在另一个工作目录启动实验 API 和 Worker。可以用 `git worktree add --detach ../AgentHub-recall-lab 4673299` 取得实测版本，实验迁移与服务共用隔离数据库。默认 A 路径可直接连接主线 API。

把上例 `--cohort product` 换为 `--cohort subset` 可重放 42 篇子集。运行使用本地公开 SciFact corpus SHA-256 `dec31c8182f3d744c7d2c09423756fd1d17cbef75808db13ba01cc0aab4d1ac6` 和 42 个已冻结文档 ID。每次新上传会取得新的修订 ID，相同分数的检索结果可能因此改变顺序；模型回复也可能随运行变化。全库读侧需要另行准备 9,189 个真实向量和作用域成员关系，当前一键入口仅覆盖两份从 HTTP 上传的语料。逐题原始完整内容留在本地 `.scratch/recall-live-20261114`，本目录回执保留公开答案、来源、引文定位与原文摘要指纹。

另以全新合成工作区运行了此复跑程序的 `--cohort product --arms A --retrieval-only`。18 份业务材料实际上传并形成 30 块，公开试验台返回 6 个其他账户的文档，正确账号仍未送入首批证据。该独立复验没有调用外部模型。[复跑命令实测记录](replay-check.json)

主线默认使用 A。此次实际生成使用的临时模型凭据已停用、换钥，合成账户登录文件与隔离环境密钥已移除。清理了本轮的 3 个容器、2 个挂载卷和 1 个网络，核对 19 个其他已停止容器的身份完全一致后，将 Docker Desktop 恢复到原先的停止状态。[会话与隔离环境收尾回执](session-cleanup.json)
