"""Independent CPU cross-encoder stress replay (no network, SQL, or generation)."""

import json
import os
from uuid import NAMESPACE_URL, uuid5

from packages.knowledge.adapters.reranker import BgeReranker
from packages.knowledge.chunking import build_deterministic_chunks
from packages.knowledge.citation_qa import _evidence_prompt, _select_evidence
from packages.knowledge.contracts import RerankCandidate, RetrievedEvidence
from packages.knowledge.parser import ParsedBlock, ParsedDocument
from packages.knowledge.source_merge import propose_source_refill

from .freeze import OUT, ROOT


def main():
    import torch

    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    torch.set_num_threads(4)
    model = (
        ROOT.parent
        / "hf_cache/models--BAAI--bge-reranker-v2-m3/snapshots"
        / "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"
    )
    scorer = BgeReranker(model_name=str(model), device="cpu")
    cases = json.loads((OUT / "matched.json").read_bytes())["cases"]
    with (OUT / "pressure.jsonl").open("x", encoding="utf-8") as out:
        for case in cases:
            pool = []
            for p in case["paragraphs"]:
                paragraph = (ROOT / p["path"]).read_bytes().decode()
                parsed = ParsedDocument(
                    (
                        ParsedBlock(
                            text=paragraph,
                            locator={
                                "type": "text_range",
                                "char_start": 0,
                                "char_end": len(paragraph),
                            },
                        ),
                    )
                )
                rev = uuid5(NAMESPACE_URL, p["source_id"])
                chunks = build_deterministic_chunks(
                    rev, parsed, chunk_size_chars=320, chunk_overlap_chars=80
                )
                for c in chunks:
                    pool.append(
                        RetrievedEvidence(
                            p["source_id"],
                            str(rev),
                            c.chunk_id,
                            p["source_id"],
                            c.locator,
                            c.text,
                            0.0,
                            None,
                            {},
                        )
                    )
            scores = scorer.rerank(
                case["question"], [RerankCandidate(e.chunk_id, e.text) for e in pool]
            )
            order = sorted(zip(scores, pool, strict=True), key=lambda x: (-x[0], x[1].chunk_id))
            ranked = tuple(e for _, e in order[:20])
            proposal = propose_source_refill(ranked)
            for arm in ("baseline", "candidate"):
                records = proposal[0] if arm == "candidate" and proposal else ranked[:6]
                chosen = _select_evidence(records, max_chars=16000)
                assert len(chosen) == 6
                prompt = _evidence_prompt(case["question"], chosen)

                def record(e):
                    return dict(
                        source_id=e.source, chunk_id=e.chunk_id, locator=e.locator, text=e.text
                    )

                row = dict(
                    id=case["id"],
                    arm=arm,
                    eligible=bool(proposal),
                    ranked=[record(e) for e in ranked],
                    selected=[record(c.evidence) for c in chosen],
                    events=[dict(messages=[{}, dict(content=prompt)])],
                )
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
            out.flush()


if __name__ == "__main__":
    main()
