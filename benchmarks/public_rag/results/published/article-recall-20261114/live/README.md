# 真实问答与引文审计

17 道题分别走同一公共 `POST /api/v1/knowledge/query`，每个检索臂只保留第一次 HTTP 结果。每个语料单独冻结一个快照。三臂顺序固定为原检索、文档融合后文内选段、标题与摘要父文档补位。生成使用相同的北京阿里云模型 `deepseek-v4.1-flash-1017`、温度 0 和 PROMPT_JSON。未轮换顺序或重复抽样，一次生成差异不能单独归因于检索。曝光的 TRAIN 诊断、小样本和单人盲审都限制推广范围。

SciFact 对 5,183 篇同版本公开标题及摘要恢复固定 BGE 向量和 9,189 个原块。QASPER 用原始 TRAIN 的七篇论文，在同一知识库中上传完整正文（不含表图说明），Celery 真实摄取；提问加论文原标题以消除多篇库内的指代歧义。父索引只收标题与摘要，读者只能引用原块。对照题 791 和 838 生殖问题来自既往首次生成中答对且有据的输出；本轮基线的全部有据答案另算，所有预定题都计入分母。QASPER 的抽取答案保留上游 BIBREF 标记，语义判定允许同义词、大小写和引用占位符差异。

[事前方案的公开投影](protocol-public.json) 提供题文、原注释哈希和完整冻结文件哈希；[语料与快照核验](provenance.json) 记录数据哈希。六份首答按语料与实验臂命名，保留回答、引文号、源文档、原块序号、完整摘录 SHA-256、块内字符偏移及短摘。检索成功的回包含有逐阶段名次。生成错误回包不含排名。没有以二次生成替换失败。另见[单题标题诊断](title-diagnostic.json)，该回包不进入 17 道配对统计。

## 首次回答和原有正确答案

|17 道固定题|有据答案|保留原检索有据题|新增 / 丢失有据题|两道负控正确拒答|HTTP 502|
|---|---:|---:|---:|---:|---:|
|原检索|7|7/7|—|2/2|3|
|按文档融合再文内选段|7|6/7|1 / 1|1/2|4|
|标题与摘要父索引补位|9|7/7|2 / 0|1/2|2|

各臂另有五次未给出有据答案的普通弃答。620 的限定性回答计为有据，明确标注尚未验证原断言中“纤维化减少”；只把完整回答计入时，父索引有 8 个，原检索和文档融合各 7 个。两种候选都保住 QASPER 原基线 5/5 个有据回答并新增完整 VG 数据量答案；两种候选对 QASPER 真无答案题都返回 502，原基线正确拒答。合成缺资料题三臂均正确拒答。文档融合臂让已答对的 838 生殖功能题首回变成 502。此前已被有据回答的两题中，文档融合保住 1/2、父索引 2/2。

核查返回引文发现，QASPER 的两个原标注段跨块（聚类算法、槽位填充指标），所需事实在实际返回的原句内可见。两臂 QASPER 各 6 个有据正答中有 4 个至少引用一个完整的官方证据段，另 2 个引用可直接证实答案的段落局部。基线 5 个有据回答中相应为 3 个完整和 2 个局部。逐题评分及理由见[盲审结果](review.json)。753 的原标文档因文档融合进入 #11，BGE 返回前六仍未入选，三臂均无引文。[原漏例实测名次](original-misses-live-ranks.json)

620 的原检索和文档臂首答为 502，它们无可还原的原始模型输入日志。单独、不调用生成的原设置重放表明，三个臂均把限定回答引用的同一篇替代来源第 0 块送至读者 #1。只有父索引首答写出小鼠间质减少、血管密度与药物浓度升高、疾病短暂稳定，并指出不能据此确认纤维化减少。该差异没有检索因果证据。[620 来源逐级名次](620-source-delivery.json)

QASPER 的 VG 数据量问题也出现同样情况。三臂在独立只检索重放中都交付了相同的三个原块，名次均为 #1、#4、#6；#6 内含完整官方证据和 16k 图片、740k 描述。两候选首答有据，原基线首答 502。两组观察到的新增均不能由此次试验证实为检索造成。[QASPER 变化题证据交付](qasper-changed-evidence.json) · [从官方语料恢复已返回引文的核验结果](citation-verification.json)

## 在独立本地实例复跑 QASPER

从仓库根目录运行。使用已安装项目依赖和 CPU BGE 权重的虚拟环境。准备隔离的 PostgreSQL、Redis、Qdrant、共享 blob 目录及本地 API 环境；确认 CLI、API、Celery 子进程的 `AGENTHUB_DATABASE_URL`、`AGENTHUB_REDIS_URL`、`AGENTHUB_QDRANT_URL`、`AGENTHUB_BLOB_ROOT`、认证密钥及加密主密钥一致。先运行迁移，再在相同配置下分别启动 API 和 worker。正式付费重放从环境或项目 .env 中加载已配置的北京阿里云路由。以下各条命令中，API 和 worker 要持续运行。若 CPU 模型默认缓存不在当前环境，请设置 HF_HOME/HF_HUB_CACHE 或为 --model 传入与服务相同的本地 BGE-M3 模型目录。

```powershell
.venv/Scripts/python -m alembic upgrade head
.venv/Scripts/python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8049
.venv/Scripts/python -m celery -A apps.worker.celery_app worker --pool=solo --concurrency=1
.venv/Scripts/python -m benchmarks.public_rag.article_live_replay --api-url http://127.0.0.1:8049/api/v1 --train data/external/rag-public-20261021/qasper/extracted/qasper-train-v0.3.json --output .scratch/qasper-article-fresh
```

使用新的输出目录。`--prepare-only` 可实测上传、快照和父/原段索引而无需模型凭据或付费请求。`--arms baseline article_local_passage full_parent4` 指定运行顺序。重放入口启动并回收只监听回环地址的各臂 API 进程，运行结束禁用和轮换测试凭据。该命令创建新的 UUID；Qdrant 同分排序和模型响应可能变化。独立验证见 [从公开原始数据重建 7 篇、99 个原块并启动三个 API 的复跑回执](qasper-replay-check.json)。完整 SciFact 三臂观测来自原公开语料的已核验既有向量，QASPER 重放不需要 SciFact 缓存。

用官方文件恢复所有返回引文并逐个核验 SHA-256，示例命令如下。

```powershell
.venv/Scripts/python -m benchmarks.public_rag.article_live_verify benchmarks/public_rag/results/published/article-recall-20261114/live/qasper-baseline.json --qasper-train data/external/rag-public-20261021/qasper/extracted/qasper-train-v0.3.json
.venv/Scripts/python -m benchmarks.public_rag.article_live_verify benchmarks/public_rag/results/published/article-recall-20261114/live/full-baseline.json --scifact-corpus data/external/rag-public-20261021/scifact/extracted/scifact/corpus.jsonl
```
