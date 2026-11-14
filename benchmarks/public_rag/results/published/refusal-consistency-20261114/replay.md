# 离线复验

在 AgentHub 仓库根目录运行。原始数据使用本机 `data/external/rag-public-20261021/qasper/extracted/qasper-train-v0.3.json`。语料正文、版本、定位和引用原文按字节校验。以下命令使用已保存的公开数据，不会调用阿里云。

```powershell
& .venv/Scripts/python -m benchmarks.public_rag.refusal_consistency_replay
& .venv/Scripts/python -m benchmarks.public_rag.refusal_consistency_verify
& .venv/Scripts/python -m pytest -q -p no:cacheprovider tests/unit/test_refusal_consistency.py tests/unit/test_knowledge_m3e_api.py tests/unit/test_evidence_sentence_reranker.py
```

历史回放把先前公开的正文、引用编号和布尔值重组为终态 JSON，将上一轮实际六段原文送入服务，在进程内调用 `POST /api/v1/knowledge/query`。先检查旧服务是否能逐项重建旧答复，再把相同输入交给候选服务，比较正文、标志和每条引用的文档、版本、块 ID、定位及摘录。上轮有一条 HTTP 错误，没有可重放的终态答复。重放不使用上轮未保存的供应商原始字节或检索分值。

新一轮真实请求同时记录了实际提供给模型的证据、供应商公开生成的结构化字段、首次 HTTP 答复和内部调用次数。对每条成功的终态生成，校验器分别运行旧逻辑和修复逻辑，再与当时真实 HTTP 返回值比对。这样可以逐题区分两次云端生成措辞变化和本地程序改写。完整语义评分独立使用盲评结果。拒答和 HTTP 错误都不产生引用准确率的分子或分母。
