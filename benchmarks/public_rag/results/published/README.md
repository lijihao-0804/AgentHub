# RAG研究公开汇总与本地证据边界

Git保存代码、研究报告和本目录的聚合投影。完整语料、逐题模型请求、数据库连接描述、运行JSONL和源码ZIP仍保留在本地原路径，不随Git上传。这意味着GitHub不是约466MB原始实验资料的完整备份。

报告中指向 `benchmarks/public_rag/results/<实验>/...` 的链接是本地证据地址；在GitHub阅读时请使用下表汇总。`report-v2.json`等修复后的版本与早期错误报告分别保存，须按对应研究报告的说明解释，不可将错误报告当最终成绩。

| 实验组 | 本地文件数 | 本地MiB | 公开聚合JSON |
|---|---:|---:|---:|
| [aliyun-delivery-20261024](aliyun-delivery-20261024/README.md) | 19 | 7.43 | 9 |
| [answer-path-20261024](answer-path-20261024/README.md) | 26 | 1.60 | 4 |
| [attributed-reader-20261023](attributed-reader-20261023/README.md) | 14 | 5.69 | 4 |
| [budget-diversity-20261022](budget-diversity-20261022/README.md) | 21 | 60.95 | 2 |
| [consumer-rag-20261023](consumer-rag-20261023/README.md) | 78 | 95.42 | 19 |
| [entity-delivery-20261024](entity-delivery-20261024/README.md) | 30 | 2.91 | 8 |
| [evidence-plan-20261023](evidence-plan-20261023/README.md) | 25 | 49.58 | 8 |
| [evidence-set-20261023](evidence-set-20261023/README.md) | 56 | 32.69 | 11 |
| [fiqa-confirmation-20261022](fiqa-confirmation-20261022/README.md) | 15 | 9.64 | 2 |
| [fiqa-confirmation-continued-20261022](fiqa-confirmation-continued-20261022/README.md) | 17 | 14.37 | 3 |
| [learned-sparse-20261022](learned-sparse-20261022/README.md) | 13 | 29.90 | 2 |
| [model-route-20261023](model-route-20261023/README.md) | 9 | 0.01 | 2 |
| [onyx-selection-20261022](onyx-selection-20261022/README.md) | 8 | 0.08 | 2 |
| [onyx-selection-off-20261022](onyx-selection-off-20261022/README.md) | 10 | 6.28 | 3 |
| [paper-qa-live-20261023](paper-qa-live-20261023/README.md) | 98 | 18.38 | 8 |
| [paragraph-qa-20261023](paragraph-qa-20261023/README.md) | 104 | 24.17 | 7 |
| [parent-context-20261022](parent-context-20261022/README.md) | 16 | 32.78 | 4 |
| [project-dbsf-20261022](project-dbsf-20261022/README.md) | 8 | 8.45 | 3 |
| [prompt-trial-20261023](prompt-trial-20261023/README.md) | 25 | 4.18 | 6 |
| [rag-root-cause-20261024](rag-root-cause-20261024/README.md) | 66 | 49.79 | 27 |
| [relation-reader-20261023](relation-reader-20261023/README.md) | 14 | 1.18 | 1 |
| [scenario-rag-20261023](scenario-rag-20261023/README.md) | 51 | 15.97 | 13 |
| [scifact-20261021](scifact-20261021/README.md) | 20 | 51.60 | 1 |
| [support-dataset-20261023](support-dataset-20261023/README.md) | 19 | 0.40 | 4 |

[本地资料清单](local-artifact-manifest.json)只记路径与体积，不含原文或密钥。重新导出：`python -m scripts.export_rag_publication`；该命令不调用模型。
