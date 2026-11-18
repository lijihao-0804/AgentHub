"""Create fresh real API snapshots; validate and copy the earlier R, B and W snapshots."""

import hashlib
import json
import shutil
import unicodedata
from pathlib import Path
from uuid import UUID

from apps.worker.tasks.knowledge import _load_chunks, _process_knowledge_ingestion
from benchmarks.retrieval.revision_reuse import arm_context, post
from benchmarks.retrieval.table_header_answers import write_entry
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.knowledge.models import KnowledgeSnapshot

ROOT = Path(__file__).resolve().parents[4]
PRIVATE = ROOT / ".scratch"
STUDY = PRIVATE / "joint-six-qa-20261118"
FRESH = PRIVATE / "joint-six-20261118/matched.json"
HISTORY = PRIVATE / "bilingual-refusal-20261118"
OLD_FREEZE = PRIVATE / "composite-qa-preflight/freeze.json"
OLD_LEDGER = PRIVATE / "composite-qa-paid/ledger-freshRBW.jsonl"
OLD_KEY = "2hop__75213_24779"
HISTORY_DIRS = {"R": "old100-live", "B": "independent", "W": "wai14-live"}
GROUPS = ("smoke", "fresh", "R", "B", "W")
FORMAL = GROUPS[1:]
HF = ROOT.parent / "hf_cache"
EMBED = HF / "models--BAAI--bge-m3/snapshots/5617a9f61b028005a4858fdac845db406aefb181"
RERANK = HF / "models--BAAI--bge-reranker-v2-m3/snapshots/953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"


def load(path):
    return json.loads(path.read_bytes())


def rows(path):
    return [json.loads(line) for line in path.read_bytes().splitlines()]


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path, value):
    with path.open("x", encoding="utf8") as out:
        write_entry(out, value)


def archive_guard():
    if sha(OLD_LEDGER) != "e3ffc69de21b0a2d9584dbd1457ab4484d34ba74b8fc8f03b652736092b8f6f5":
        raise ValueError("The sealed unknown ledger changed")
    if sha(OLD_FREEZE) != "2f32241f7fc1173524e5fff9a25e1108b5bb0edc96b8652a70637a8a8efc969e":
        raise ValueError("The prior preflight registration changed")


def material(group):
    if group in {"fresh", "smoke"}:
        if sha(FRESH) != "86bd4c52b4ae3f052f61dcb255b02c568ce2c2ab9d855b8a46286a75103985bb":
            raise ValueError("The new data set differs from its independent freeze")
        cases = load(FRESH)["cases"]
        ids = [r["id"] for r in cases]
        if len(ids) != 65 or len(set(ids)) != len(ids) or OLD_KEY in ids:
            raise ValueError("Fresh QA denominator or isolation differs")
        result = [c for c in cases if (c["cohort"] == "smoke") == (group == "smoke")]
        if len(result) != (1 if group == "smoke" else 64):
            raise ValueError("Fresh/smoke membership differs")
        return result
    if group == "W":
        return [
            dict(id=f"W{i:02}", doc_id="WAI", question=c[0], value=c[3], cell=c[4])
            for i, c in enumerate(
                load(ROOT / "benchmarks/retrieval/table_header_protocol.json")["cases"], 1
            )
        ]
    folder = "first-refusal-100-docs" if group == "R" else "bilingual-refusal-100-docs"
    return sorted(load(PRIVATE / folder / "manifest.json")["cases"], key=lambda c: c["id"])


def history_files(group):
    folder = "composite-qa-preflight" if group == "R" else "composite-qa-preflight-remaining"
    return PRIVATE / folder / (group + "-bindings.json"), PRIVATE / folder / (group + "-index")


def copy_history_index(group, target):
    _, old = history_files(group)
    if target.exists():
        return
    shutil.copytree(old, target)
    for path in old.rglob("*"):
        if path.is_file() and sha(path) != sha(target / path.relative_to(old)):
            raise ValueError("The historic index copy differs")


async def bind_history(group, cases, factory):
    state_path, _ = history_files(group)
    if sha(state_path) != load(OLD_FREEZE)["private_artifacts"][str(state_path.relative_to(ROOT))]:
        raise ValueError("Historical bindings differ from frozen bytes")
    state = load(state_path)
    previous = load(HISTORY / HISTORY_DIRS[group] / "freeze.json")["inputs"]
    async with factory() as session:
        for doc, bound in previous.items():
            snap = await session.get(KnowledgeSnapshot, UUID(bound["snapshot"]))
            chunks = await _load_chunks(factory, UUID(bound["revision"]))
            if (
                snap is None
                or snap.content_hash != bound["snapshot_hash"]
                or str(snap.knowledge_base_id) != bound["kb"]
                or not chunks
                or any(str(c.document_id) != bound["document"] for c in chunks)
                or state["revisions"][bound["revision"]] != doc
            ):
                raise ValueError(f"Historic database copy differs for {group}/{doc}")
    if state["bindings"] != {c["id"]: previous[c["doc_id"]] for c in cases}:
        raise ValueError("Historical case-to-snapshot binding changed")
    return state


async def fresh_context(folder, factory):
    path = folder / "context.json"
    if path.exists():
        return WorkspaceExecutionContext.model_validate(load(path))
    context = await arm_context(factory)
    save(path, context.model_dump(mode="json"))
    return context


async def bind_fresh(cases, folder, client, context, components, settings, queue):
    bindings, revisions = {}, {}
    base = f"/api/v1/workspaces/{context.workspace_id}/knowledge-bases"
    for case in cases:
        case_id = case["id"]
        record = folder / (hashlib.sha256(case_id.encode()).hexdigest() + ".json")
        if not record.exists():
            kb = await post(client, base, json={"name": case_id})
            url = base + "/" + kb["id"]
            sources = {}
            for p in case["paragraphs"]:
                path = (ROOT / p["path"]).resolve(strict=True)
                if not path.is_relative_to(
                    FRESH.parent
                ) or path.read_bytes().decode() != unicodedata.normalize(
                    "NFC", p["paragraph_text"]
                ):
                    raise ValueError("Registered paragraph source differs")
                uploaded = await post(
                    client,
                    url + "/documents",
                    files={"file": (path.name, path.read_bytes(), "text/plain")},
                )
                job = queue.jobs.popleft()
                if str(job) != uploaded["ingestion_job"]["id"]:
                    raise ValueError("Unexpected ingestion job")
                await _process_knowledge_ingestion(
                    job, settings, indexing_factory=lambda _: components
                )
                sources[uploaded["revision"]["id"]] = p["source_id"]
            snap = await post(client, url + "/snapshots")
            if snap["item_count"] != len(case["paragraphs"]) or len(sources) != len(
                case["paragraphs"]
            ):
                raise ValueError("Snapshot omits a registered source")
            save(
                record,
                dict(
                    id=case_id,
                    bound=dict(
                        kb=kb["id"], snapshot=snap["id"], snapshot_hash=snap["content_hash"]
                    ),
                    sources=sources,
                ),
            )
        saved = load(record)
        if saved["id"] != case_id:
            raise ValueError("Snapshot belongs to another question")
        bindings[case_id] = saved["bound"]
        revisions.update(saved["sources"])
        print(f"Bound {case_id} ({len(bindings)}/{len(cases)})", flush=True)
    return dict(context=context.model_dump(mode="json"), bindings=bindings, revisions=revisions)
