# AgentHub G-02 compact 默认化评估结果报告（阶段 1 停止条件触发，方向关闭）

- 输入：[下一批立项执行计划](AgentHub-RAG下一批立项执行计划-0730a94.md) 工作流二；预注册协议：[results/compact-default-20261025/protocol.json](../../benchmarks/public_rag/results/compact-default-20261025/protocol.json)。
- 结论先行：**阶段 1 停止条件触发，G-02 关闭，G2-3 答案 A/B 不执行**。停止原因不是 compact 更差——而是预注册主指标"送达条数/字节较 structured5 上升 ≥ +25%"在 MuSiQue@8000 双层预算下**结构上不可能达成**：该场景数量上限（limit=5）先于预算绑定，两臂送达相同 5 条；compact 的实际效果是**同 5 条记录省 28.6% 字节**（5466→3900B），方向与"上升"相反。这是停止条件设计时未预见"数量绑定"边界的情况，按纪律不得事后放宽门槛，只如实记录后关闭。

## G2-1 四臂重放（200 题，零 API）

| 臂 | 最终召回 | 全文召回 | 完整集合 | 送达记录 | 最终字节 |
| --- | ---: | ---: | ---: | ---: | ---: |
| legacy5（生产默认） | 0.783 | 0.743 | 0.540 | 5.00 | 5466 |
| structured5（②档） | 0.783 | 0.743 | 0.540 | 5.00 | 5466 |
| **compact5（③档）** | 0.783 | 0.743 | 0.540 | 5.00 | **3900（−28.6%）** |
| gold_budgeted（控制） | 1.000 | 1.000 | 1.000 | 2.56 | 3027 |

compact5 与 structured5 成员完全一致（短 ID 化不改变预算选择结果），证据质量零差异、零回归；纯效率差 1566B/题。

## G2-2 中文合成送达量（1200 字符块 × 5，零 API，无门槛）

| 配置 | 字节 | 事实送达 | 说明 |
| --- | ---: | ---: | --- |
| 默认 rag_records @5000 池 | 4330 | **1/5** | 生产默认现状 |
| compact @5000 池 | 3970 | **1/5** | 同送达、省 8.3% 字节 |
| compact @8000 双层 | 7749 | **2/5** | vs rag_records@8000 的 1/5（此前测量）——**计划假说（1/5→2-3/5）在预算绑定场景成立** |

## 结论与遗留观察（不构成重开）

1. **在预算为绑定约束的场景（中文胖记录、≥8000 预算），compact 确实多送 1 条完整证据**——假说方向正确，但生产默认池（5000）下中文单条记录 ~3.7KB，无论投影形态都装不下第二条，问题在池大小而非投影形态。
2. 在数量为绑定约束的场景（MuSiQue@8000、英文瘦记录），compact 的价值是**同信息省 28.6% 上下文**——对多轮检索共享证据池（R-06 后每轮都争池）意味着真实容量增益。
3. 若未来重开，新协议应预注册的指标是"**默认池 5000 下的多轮场景送达总量**"或"**池耗尽场景的字节效率**"，而非本协议的"条数上升"。
4. 依据以上两点，compact_v1 保持 opt-in（现状），默认路径维持 rag_records_v1。

## 账本

- LLM 调用：**0**（两阶段全部零 API；G2-3 未执行）。
- 实现：`consumer_replay.py` 新增 `compact5` profile（含 compact 视图的评分映射修复——短 ID 经运行私有注册表还原 chunk_id，仅用于漏斗计分，不进模型可见载荷；该修复对既有臂零影响，测试全过）。
- 工件：`protocol.json`、`chinese-synthetic.json`、replay 输出（.scratch，未入库）。

## 复现

```bash
PYTHONPATH=. uv run python benchmarks/public_rag/consumer_replay.py \
  --baseline benchmarks/public_rag/results/consumer-rag-20261023/confirmation-candidates-1 \
  --data data/external/rag-public-20261021/musique/extracted/data \
  --protocol benchmarks/public_rag/results/consumer-rag-20261023/protocol.json \
  --output <新目录> --profiles legacy5,structured5,compact5,gold_budgeted
```
