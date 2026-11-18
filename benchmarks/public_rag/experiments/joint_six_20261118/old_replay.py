"""Replay the frozen six-slot selector on the prior 24 questions without model calls."""

import json
import pickletools
import sqlite3
import subprocess
from contextlib import closing
from functools import partial
from statistics import median
from time import perf_counter
from types import SimpleNamespace
from uuid import UUID

from packages.agent_runtime.context_budget import ContextCategory
from packages.knowledge.citation_qa import (
    _deny_oversized,
    _pack_joint_qa_evidence,
    _render_qa_evidence,
    _select_evidence,
    citation_system_prompt,
)
from packages.knowledge.contracts import RetrievedEvidence
from packages.model_gateway.contracts import ModelCapabilities, ModelMessage

from .freeze import HERE, ROOT, as_bytes, file_hash, sha
from .score import (
    checked,
    checked_pool,
    collect,
    coverage,
    first_prompt,
    minimum_pool_chunks,
    sources_for,
    summarize,
    verify_identity,
)

PRODUCT = "eb0082c13e857f3869bbc1c92f05fa78cc817567"
RECEIPTS = ROOT / ".scratch/composite-qa-preflight/fresh.jsonl"
BINDINGS = RECEIPTS.with_name("fresh-bindings.json")
CASES = ROOT / ".scratch/merge-refill-fresh/matched.json"
INDEX = RECEIPTS.parent / "fresh-index/collection/composite/storage.sqlite"
ARCHIVES = {
    RECEIPTS: "9faa9016122c54488b21816c0de3ed93b014dcce87f0d39b187cd64dc168b113",
    BINDINGS: "a946c4d29699239fedadd3a2b34ada699d750c76e05606538c8f745c13f9fdae",
    CASES: "52e871adf5176d1f93f7c5162b34661da0b4be606b93155543215c83ebc22e55",
    INDEX: "e669584fa698e8467f2cbf77cdee2067e12ce74a6fa50fe2d23f392887301da3",
}


def point_identities():
    """Recover original document UUIDs from the archived local Qdrant collection, read-only."""
    fields = (
        "workspace_id",
        "knowledge_base_id",
        "document_id",
        "document_revision_id",
        "chunk_id",
    )
    result = {}
    with closing(sqlite3.connect(INDEX.as_uri() + "?mode=ro&immutable=1", uri=True)) as storage:
        for (blob,) in storage.execute("SELECT point FROM points"):
            # These pinned Qdrant points are pickled. Inspect their scalar
            # payload fields without instantiating objects or loading vectors.
            strings = [v for op, v, _ in pickletools.genops(blob) if "UNICODE" in op.name]
            payload = strings[strings.index("payload") + 1 :]
            ids = {key: payload[payload.index(key) + 1] for key in fields}
            checked(ids["chunk_id"] not in result, "Duplicate archived point")
            result[ids["chunk_id"]] = ids
    return result


def hydrate(rows, arms, identities, binding, revisions, workspace):
    stages = arms["candidate"]["body"]["retrieval_trace"]["stages"]
    scores = {
        stage: {r["chunk_id"]: r["score"] for r in stages[stage]["results"]}
        for stage in ("fused", "rerank")
    }
    for original in rows:
        chunk, rev = original["chunk_id"], original["revision"]
        ids = identities[chunk]
        checked(
            ids["workspace_id"] == workspace
            and ids["knowledge_base_id"] == binding["kb"]
            and ids["document_revision_id"] == rev
            and revisions[rev] == original["source_id"],
            "Archived chunk is not in the original question scope",
        )
        doc = ids["document_id"]
        yield RetrievedEvidence(
            document_id=doc,
            document_revision_id=rev,
            chunk_id=chunk,
            source=f"document:{doc}/revision:{rev}",
            locator=original["locator"],
            text=original["text"],
            retrieval_score=scores["fused"][chunk],
            rerank_score=scores["rerank"][chunk],
            # The original fresh upload created one revision per document with
            # no lifecycle annotations. Every historical baseline must match byte-for-byte.
            metadata={"effective_date": None, "superseded": False},
        )


def evaluate_case(case, arms, identities, bindings):
    ranked = checked_pool(arms, historical=True)
    checked(len(ranked) == 20, "Expected the entire registered top 20")
    originals = {r["chunk_id"]: r for r in ranked}
    rank = {cid: i for i, cid in enumerate(originals, 1)}
    old = arms["baseline"]
    bound = bindings["bindings"][case["id"]]
    for arm in arms.values():
        checked(
            arm["body"]["retrieval_trace"]["snapshot_id"] == bound["snapshot"], "Snapshot differs"
        )
    pool = tuple(
        hydrate(
            ranked,
            arms,
            identities,
            bound,
            bindings["revisions"],
            bindings["context"]["workspace_id"],
        )
    )
    query = case["question"]
    system = ModelMessage("system", citation_system_prompt("PROMPT_JSON"))
    render = partial(
        _render_qa_evidence,
        query,
        projection=None,
        knowledge_base_id=UUID(bound["kb"]),
        knowledge_snapshot_id=UUID(bound["snapshot"]),
    )
    # Only these two historical model-profile fields enter ContextBudgetPolicy.
    # No gateway, provider credentials or database connection are constructed.
    budget_plan = SimpleNamespace(
        chain=(
            SimpleNamespace(
                capabilities=ModelCapabilities(max_context_tokens=32_768),
                max_tokens=256,
            ),
        )
    )
    before = perf_counter()
    baseline, old_prompt = render(_select_evidence(pool[:6], max_chars=16_000))
    _deny_oversized(
        budget_plan,
        (system, ModelMessage("user", old_prompt)),
        (ContextCategory.SYSTEM_PROMPT, ContextCategory.RAG_EVIDENCE),
        response_schema=None,
    )
    baseline_ms = (perf_counter() - before) * 1_000
    before = perf_counter()
    candidate, new_prompt = _pack_joint_qa_evidence(
        query,
        pool,
        max_items=6,
        max_tokens=16_000,
        render=render,
        system_message=system,
        plan=budget_plan,
        output_mode="PROMPT_JSON",
    )
    candidate_ms = (perf_counter() - before) * 1_000

    def messages(prompt):
        return [{"role": "system", "content": system.content}, {"role": "user", "content": prompt}]

    checked(
        messages(old_prompt) == old["events"][0]["messages"],
        "Re-rendered baseline differs from archived first message",
    )
    checked(
        [s.evidence.chunk_id for s in baseline] == [s["chunk_id"] for s in old["selected"]],
        "Baseline ids differ",
    )

    # Gold paragraphs and decomposition enter the audit only after the two
    # selections and their complete first-message strings have been produced.
    sources = sources_for(case)
    gold = {g["source_id"] for g in case["gold_spans"]}
    # checked_pool compared all 20 pre-dedup trace IDs to the same 20
    # post-dedup original records. No historical candidate was discarded here.
    pool_coverage = coverage(ranked, sources, gold)
    minimum = minimum_pool_chunks(ranked, sources, gold)
    result = dict(
        id=case["id"],
        pool=pool_coverage,
        after_overlap=pool_coverage,
        pool_count=len(pool),
        pool_id_score_sha256=sha(
            as_bytes([(p.chunk_id, p.retrieval_score, p.rerank_score) for p in pool])
        ),
        min_raw_chunks_for_all_gold=minimum,
        min_retained_chunks_for_all_gold=minimum,
        local_pack_ms={"baseline": round(baseline_ms, 3), "candidate": round(candidate_ms, 3)},
    )
    for name, selected, prompt in (
        ("baseline", baseline, old_prompt),
        ("candidate", candidate, new_prompt),
    ):
        records = [
            dict(originals[s.evidence.chunk_id], id=s.id, source=s.evidence.source)
            for s in selected
        ]
        verify_identity(records, ranked)
        # first_prompt checks the actual complete numbered text against each
        # source, locator and citation id. This is a local serialization check.
        checked_message = first_prompt(
            {"events": [{"messages": messages(prompt)}], "selected": records}, case
        )
        result[name] = dict(
            **coverage(records, sources, gold),
            message_sha256=checked_message,
            selected=[
                dict(
                    slot=r["id"],
                    paragraph=int(r["source_id"].rsplit("::", 1)[1]),
                    rank=rank[r["chunk_id"]],
                    chunk_id=r["chunk_id"],
                    chars=[r["locator"]["char_start"], r["locator"]["char_end"]],
                )
                for r in records
            ],
        )
    result["hops"] = []
    for hop, step in enumerate(case["question_decomposition"], 1):
        idx = step["paragraph_support_idx"]
        source_id = next(p["source_id"] for p in case["paragraphs"] if p["idx"] == idx)
        checked(source_id in gold, "Official decomposition and supporting source differ")
        hop_result = dict(
            hop=hop,
            paragraph=idx,
            pool_ranks=[rank[r["chunk_id"]] for r in ranked if r["source_id"] == source_id],
        )
        for name in ("baseline", "candidate"):
            arm = result[name]
            hop_result[name] = dict(
                ranks=[s["rank"] for s in arm["selected"] if s["paragraph"] == idx],
                visible=source_id in arm["visible"],
                full=source_id in arm["full"],
            )
        result["hops"].append(hop_result)
    checked(len(result["hops"]) == len(gold) == 2, "Expected two official supports")
    return result


def run():
    before = perf_counter()
    for path, digest in ARCHIVES.items():
        checked(file_hash(path) == digest, f"Archive differs: {path.name}")
    product_hashes = {}
    for filename in ("packages/knowledge/joint_evidence.py", "packages/knowledge/citation_qa.py"):
        frozen = subprocess.check_output(["git", "show", PRODUCT + ":" + filename], cwd=ROOT)
        checked(
            frozen == (ROOT / filename).read_bytes().replace(b"\r\n", b"\n"),
            "Product differs from freeze",
        )
        product_hashes[filename] = sha(frozen)
    identities = point_identities()
    cases = json.loads(CASES.read_bytes())["cases"]
    raw = collect(RECEIPTS)
    bindings = json.loads(BINDINGS.read_bytes())
    checked(
        len(cases) == 24 and set(raw) == {c["id"] for c in cases}, "Historical denominator differs"
    )
    results = [evaluate_case(c, raw[c["id"]], identities, bindings) for c in cases]
    recoverable = [
        r for r in results if len(r["pool"]["full"]) == 2 and len(r["baseline"]["full"]) != 2
    ]
    checked(len(recoverable) == 6, "Historical recoverable subset differs")
    elapsed = {arm: [r["local_pack_ms"][arm] for r in results] for arm in ("baseline", "candidate")}
    out = dict(
        schema=1,
        purpose="Prior 24-question development diagnostic; excluded from independent evaluation.",
        basis=(
            "Top 20 original hydrated chunks and actual per-chunk RRF/BGE scores from archived "
            "local HTTP preflight. Baseline messages equal the archived first requests. "
            "Candidate messages are reconstructed offline, without new retrieval, generation or "
            "answer scoring. Both timings include rendering and context admission."
        ),
        product_commit=PRODUCT,
        product_git_blob_sha256=product_hashes,
        private_archives_sha256={
            p.relative_to(ROOT).as_posix(): digest for p, digest in ARCHIVES.items()
        },
        rerank_model="BAAI/bge-reranker-v2-m3",
        embedding_model="BAAI/bge-m3",
        citation_output_mode="PROMPT_JSON",
        max_passages=6,
        body_budget_approx_tokens=16_000,
        first_request_baseline_byte_matches=len(results),
        new_provider_calls=0,
        changed_selections=sum(
            r["baseline"]["selected"] != r["candidate"]["selected"] for r in results
        ),
        all_arms_show_six=all(
            len(r[arm]["selected"]) == 6 for r in results for arm in ("baseline", "candidate")
        ),
        all=summarize(results),
        recoverable_six=summarize(recoverable),
        recoverable_ids=[r["id"] for r in recoverable],
        local_pack_and_render_ms={
            arm: dict(total=round(sum(t), 3), median=round(median(t), 3), max=max(t))
            for arm, t in elapsed.items()
        },
        wall_seconds_before_report_write=round(perf_counter() - before, 3),
        cases=results,
    )
    (HERE / "old_replay.json").write_bytes(as_bytes(out))
    print(
        json.dumps(
            {
                k: out[k]
                for k in (
                    "all",
                    "recoverable_six",
                    "local_pack_and_render_ms",
                    "wall_seconds_before_report_write",
                )
            }
        )
    )


if __name__ == "__main__":
    run()
