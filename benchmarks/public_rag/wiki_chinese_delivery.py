"""Stage 3 (rag-final): Chinese real-prose delivery assertion, zero LLM calls.

Authored Chinese wiki-style sources go through the production path —
deterministic chunking, BGE-M3 dense+sparse, Qdrant, hybrid RRF, top-5
delivery — and every mechanically extracted QA pair must deliver the chunk
holding its gold sentence. Isolated collection; no wiki code is modified.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from uuid import UUID, uuid4

from packages.core.config.settings import Settings
from packages.knowledge.adapters.embeddings import BgeM3DenseEmbedder
from packages.knowledge.adapters.qdrant import QdrantVectorIndex
from packages.knowledge.adapters.sparse import BgeM3SparseEncoder
from packages.knowledge.chunking import build_deterministic_chunks
from packages.knowledge.contracts import (
    VectorRecord,
    VectorScope,
)
from packages.knowledge.parser import ParsedBlock, ParsedDocument
from packages.knowledge.retrieval import fuse_reciprocal_rank

RUN = Path(__file__).parent / "results" / "rag-final-20261026"
UUID_NS = UUID("5c1f7a2e-9b34-4d18-8f60-000000000003")
COLLECTION = "ctxp-wiki-zh"
FINAL_K = 5
GATE_PCT = 90.0

# Authored Chinese wiki-style sources: real prose, factual sentences with a
# 「term … 是/为/共 value …」shape so question extraction stays mechanical.
SOURCES = {
    "数据保留政策": (
        "数据保留政策\n\n"
        "主数据的核心业务记录保留期为 7 年，自记录归档之日起算。\n"
        "审计日志的保留期为 3 年，到期后由清理任务自动删除。\n"
        "备份快照共保留 4 份，其中 1 份存放于异地机房。\n"
        "用户会话数据的保留期为 90 天，超期后仅保留匿名统计。\n"
        "合同附件的保留期与主合同一致，均为合同终止后 5 年。\n"
        "跨境传输的日志副本保留期为 180 天，且必须加密存储。\n"
    ),
    "退款流程": (
        "退款流程\n\n"
        "标准退款的承诺到账时限为 7 个工作日。\n"
        "大额退款需要两级审批，第二级审批人为财务总监。\n"
        "会员等级为钻石的用户享受退款加速，到账时限缩短为 3 个工作日。\n"
        "争议退款的处理窗口为 30 个自然日，超时自动升级。\n"
        "原路退回失败时，资金退至备用账户，备用账户须完成实名验证。\n"
        "每笔退款申请须附带订单号与退款原因编码，共 2 项必填字段。\n"
    ),
    "唯一性约束": (
        "唯一性约束\n\n"
        "部分唯一索引只约束满足条件的行，不能用普通唯一约束表达。\n"
        "逻辑主键由业务侧生成，物理主键使用数据库自增列。\n"
        "同一工作区内知识库名称唯一，跨工作区允许重名。\n"
        "幂等键的有效窗口为 24 小时，窗口外重复请求视为新请求。\n"
        "快照哈希覆盖全部成员标识，共 3 类字段参与计算。\n"
        "唯一冲突的默认处理是拒绝写入并返回 409 状态码。\n"
    ),
    "审批边界": (
        "审批边界\n\n"
        "写操作的审批默认策略为始终需要审批，读操作免审批。\n"
        "审批单的有效期为 1 小时，过期后须重新发起。\n"
        "同一审批单最多可绑定 5 个待执行动作。\n"
        "审批通过后动作未执行时，运行状态标记为需要关注而非失败。\n"
        "外部副作用无法确认结果时，必须记录为未知结果状态。\n"
        "审批记录的保存期限为 10 年，且不可修改。\n"
    ),
    "检索预算": (
        "检索预算\n\n"
        "证据池的默认份额为模型窗口的 25%。\n"
        "通用工具结果池的默认份额为窗口的 15%。\n"
        "长期记忆池的默认份额为窗口的 8%。\n"
        "证据条数上限为 10 条，超出部分整条丢弃。\n"
        "检索上下文预算的合法区间为 2 到 64000。\n"
        "多轮检索共享证据池时，最新一轮优先分配。\n"
    ),
    "文档解析": (
        "文档解析\n\n"
        "解析器当前支持纯文本、Markdown 与 PDF 三种媒体类型。\n"
        "PDF 解析按页提取文本，单文档页数上限为 500 页。\n"
        "解析在独立子进程中执行，超时上限为 60 秒。\n"
        "解析字符总量上限为 200 万字符，超限报资源超限错误。\n"
        "切块采用固定窗口，默认块长 1200 字符、重叠 200 字符。\n"
        "空文档解析结果为失败，错误码为解析失败。\n"
    ),
    "成员与工作区": (
        "成员与工作区\n\n"
        "组织成员与工作区成员是两个独立的成员概念。\n"
        "工作区角色共 3 种：所有者、成员与只读访客。\n"
        "邀请成员须由所有者或管理员发起，普通成员无权邀请。\n"
        "访问令牌的有效期为 15 分钟。\n"
        "刷新令牌的有效期为 30 天，且随请求轮换。\n"
    ),
    "运行可观测": (
        "运行可观测\n\n"
        "追踪内容默认关闭，开启后按脱敏规则写入。\n"
        "业务正文默认不写入日志，错误信息使用安全消息。\n"
        "每次运行的事件按序号递增，客户端可断线续收。\n"
        "流式断开后的宽限期为 60 秒，超时终止运行。\n"
        "请求标识通过指定请求头透传，便于全链路追踪。\n"
    ),
}

_SENTENCE = re.compile(r"[^\n。]+[。？]")
_TERM = re.compile(r"「([^」]{2,12})」")
_TERM_KEYWORD = re.compile(r"([一-鿿]{2,8}(?:上限|期限|默认|唯一|审批|保留|窗口))")
_FACT = re.compile(
    r"^(.{2,14}?)的?(?:保留期|到账时限|处理窗口|有效期|有效窗口|默认份额|页数上限|"
    r"超时上限|字符总量上限|保存期限|上限|承诺到账时限|缩短)?(?:为|共|是)\s*([^，。\n]{1,24})[。]?",
    re.UNICODE,
)


def build_qa():
    """Mechanically extract (question, gold_sentence, source) triples."""
    pairs = []
    for source_name, text in SOURCES.items():
        for sentence in _SENTENCE.findall(text):
            sentence = sentence.strip()
            if sentence.endswith("？") or len(sentence) < 12:
                continue
            term = _TERM.search(sentence)
            fact = _FACT.match(sentence)
            if term:
                question = f"{term.group(1)}是什么？"
            elif fact:
                subject = fact.group(1).strip(" 的")
                question = f"{subject}是多少？"
            else:
                # Generic fallback: split on the first copula so every fact
                # sentence still yields one mechanical pair.
                match = re.search(r"^(.{2,16}?)(?:为|是|共)([^，。]{1,24})[。]?$", sentence)
                if match:
                    question = f"{match.group(1).strip()}是什么？"
                else:
                    # Third fallback: numeric fact ("共 N 项…", "…上限为 500 页").
                    numeric = re.search(r"(\d+[\d,.]*\s*[^，。\s]{0,6})", sentence)
                    keyword = _TERM_KEYWORD.search(sentence)
                    focus = numeric.group(1) if numeric else (keyword.group(1) if keyword else None)
                    if focus is None:
                        continue
                    question = f"关于{focus.strip()}，这段规定什么？"
            pairs.append(
                {
                    "source": source_name,
                    "question": question,
                    "gold_sentence": sentence,
                }
            )
    return pairs


def _scope(settings) -> VectorScope:
    return VectorScope(
        workspace_id="ctxp-wiki-zh",
        knowledge_base_id="ctxp-wiki-zh",
        document_revision_ids=("ctxp-frozen",),
    )


async def main() -> None:
    settings = Settings()
    dense = BgeM3DenseEmbedder(
        model_name=settings.knowledge_embedding_model,
        device=settings.knowledge_embedding_device,
        batch_size=settings.knowledge_embedding_batch_size,
        expected_dimension=settings.knowledge_dense_vector_size,
    )
    sparse = BgeM3SparseEncoder(model_name=settings.knowledge_embedding_model)
    index = QdrantVectorIndex(
        url=settings.qdrant_url,
        collection_name=COLLECTION,
        dense_vector_size=settings.knowledge_dense_vector_size,
        timeout_seconds=120,
    )
    index.ensure_collection()

    chunk_texts: list[tuple[str, str, int]] = []  # (text, source, ordinal)
    revision_by_source = {}
    for source_name, text in SOURCES.items():
        revision_id = uuid4()
        revision_by_source[source_name] = revision_id
        parsed = ParsedDocument((ParsedBlock(text, {"type": "text_range"}),))
        for candidate in build_deterministic_chunks(
            revision_id,
            parsed,
            chunk_size_chars=1_200,
            chunk_overlap_chars=200,
        ):
            chunk_texts.append((candidate.text, source_name, candidate.ordinal))

    records = []
    for text, source, ordinal in chunk_texts:
        chunk_id = str(uuid4())
        dense_vector = dense.embed_documents((text,))[0]
        encoding = sparse.encode_documents((text,))[0]
        records.append(
            (
                VectorRecord(
                    point_id=str(uuid4()),
                    chunk_id=chunk_id,
                    dense=dense_vector,
                    sparse=encoding,
                    payload={
                        "workspace_id": "ctxp-wiki-zh",
                        "knowledge_base_id": "ctxp-wiki-zh",
                        "document_revision_id": "ctxp-frozen",
                        "chunk_id": chunk_id,
                        "source": source,
                        "ordinal": ordinal,
                    },
                ),
                text,
            )
        )
    index.upsert(tuple(record for record, _ in records))
    print(f"indexed {len(records)} chunks from {len(SOURCES)} Chinese sources", flush=True)

    scope = _scope(settings)
    pairs = build_qa()
    delivered_hits = 0
    rows = []
    for pair in pairs:
        dense_vector = dense.embed_query(pair["question"])
        dense_hits = index.dense_search(dense_vector, scope=scope, limit=100)
        encoding = sparse.encode_query(pair["question"])
        sparse_hits = index.sparse_search(encoding, scope=scope, limit=100)
        fused = fuse_reciprocal_rank(
            dense_hits, sparse_hits, rrf_k=60, candidate_top_k=100
        )
        text_by_chunk = {record.chunk_id: text for record, text in records}
        delivered = []
        for candidate in fused[:FINAL_K]:
            text = text_by_chunk.get(candidate.chunk_id)
            if text is not None:
                delivered.append(text)
        hit = any(pair["gold_sentence"] in text for text in delivered)
        delivered_hits += hit
        rows.append({**pair, "delivered": hit})

    rate = round(100 * delivered_hits / len(pairs), 1) if pairs else 0.0
    summary = {
        "sources": len(SOURCES),
        "chunks": len(records),
        "qa_pairs": len(pairs),
        "delivered": delivered_hits,
        "delivery_rate_pct": rate,
        "gate_pct": GATE_PCT,
        "verdict": "PASS" if rate >= GATE_PCT else "FAIL",
    }
    (RUN / "chinese-delivery.json").write_text(
        json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
