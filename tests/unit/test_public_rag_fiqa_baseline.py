from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from benchmarks.public_rag import fiqa_baseline as fiqa
from benchmarks.public_rag.data import SourceDocument


def _state(tmp_path: Path) -> dict[str, object]:
    config = fiqa.configuration()
    return {
        "schema_version": 1,
        "dataset": "fiqa",
        "database": fiqa.DATABASE,
        "collection": fiqa.COLLECTION,
        "user_id": str(uuid4()),
        "organization_id": str(uuid4()),
        "workspace_id": str(uuid4()),
        "knowledge_base_id": str(uuid4()),
        "kb": {"id": ""},
        "snapshot_id": str(uuid4()),
        "snapshot_hash": "a" * 64,
        "documents": 2,
        "empty_documents": 1,
        "indexed_documents": 1,
        "chunks": 1,
        "chunk_sources": {"chunk-a": "doc-a"},
        "text_policy": fiqa.retrieval_identity(config)["text_policy"],
        "retrieval_identity": fiqa.retrieval_identity(config),
        "index_status": "COMPLETE",
    }


def _fix_kb(state: dict[str, object]) -> dict[str, object]:
    state["kb"] = {"id": state["knowledge_base_id"]}
    return state


def _protocol(state: dict[str, object], **updates):
    protocol = {
        "dataset_id": "fiqa",
        "split": "test",
        "expected_queries": 648,
        "evaluation_role": "independent_confirmation",
        "seed": 20261027,
        "snapshot_id": state["snapshot_id"],
        "database": fiqa.DATABASE,
        "collection": fiqa.COLLECTION,
        "frozen_development_selection": fiqa.FROZEN_DEV_SELECTION,
        "baseline_controls": fiqa._baseline_controls(),
    }
    protocol.update(updates)
    return protocol


def test_configuration_changes_only_fiqa_collection() -> None:
    scifact = fiqa.scifact_settings()
    config = fiqa.configuration()

    changed = {
        key
        for key in config.model_dump()
        if getattr(config, key) != getattr(scifact, key)
    }
    assert changed == {"knowledge_qdrant_collection"}
    assert config.knowledge_qdrant_collection == "rag_fiqa_20261022"
    assert fiqa.DATABASE == "agenthub_rag_scifact_20261021"


def test_retrieval_identity_freezes_models_chunking_and_budgets() -> None:
    identity = fiqa.retrieval_identity(fiqa.configuration())

    assert identity["dense_model"] == "BAAI/bge-m3"
    assert identity["dense_dimension"] == 1024
    assert identity["dense_normalized"] is True
    assert identity["sparse"] == "BGE-M3 tokenizer log-TF with Qdrant IDF"
    assert identity["reranker_model"] == "BAAI/bge-reranker-v2-m3"
    assert identity["reranker_precision"] == "fp16"
    assert identity["embedding_batch_size"] == identity["reranker_batch_size"] == 8
    assert identity["chunk_size_chars"] == 1200
    assert identity["chunk_overlap_chars"] == 200
    assert identity["dense_top_k"] == identity["sparse_top_k"] == 30
    assert identity["candidate_top_k"] == identity["rerank_top_k"] == 20
    assert identity["final_top_k"] == 20
    assert identity["rrf_k"] == 60
    assert identity["max_chunks_per_document"] is None


def test_chunk_recipe_uses_stripped_title_text_and_production_ids() -> None:
    revision_id = uuid4()
    doc = SourceDocument("doc-a", "  title  ", "  body  ")

    chunks = fiqa.chunk_document(revision_id, doc)

    assert len(chunks) == 1
    assert chunks[0].text == "title\n\nbody"
    assert chunks[0].locator["type"] == "text_range"
    assert chunks[0].chunk_id
    assert fiqa.chunk_document(revision_id, doc) == chunks


def test_empty_document_has_no_chunk_but_metadata_remains_valid() -> None:
    doc = SourceDocument("empty", " ", "")
    parsed = fiqa.build_parsed_document(doc)

    assert parsed.blocks[0].text == "\n\n"
    assert fiqa.chunk_document(uuid4(), doc) == ()
    metadata = fiqa._document_metadata(doc, uuid4())
    assert metadata["original_filename"] == "empty.txt"
    assert metadata["file_size"] == 2


def test_state_validation_requires_fiqa_stores_and_frozen_identity() -> None:
    state = _fix_kb(_state(Path.cwd()))
    fiqa.validate_state(state)

    state["database"] = "agenthub"
    with pytest.raises(ValueError, match="database"):
        fiqa.validate_state(state)


def test_state_validation_rejects_mismatched_kb_key() -> None:
    state = _state(Path("."))
    state["kb"] = {"id": str(uuid4())}

    with pytest.raises(ValueError, match="knowledge_base_id"):
        fiqa.validate_state(state)


def test_exact_protocol_binds_dataset_split_seed_and_arm() -> None:
    state = _fix_kb(_state(Path(".")))
    protocol = _protocol(state)
    path = Path.cwd() / ".fiqa-protocol-fixture.json"
    try:
        path.write_text(json.dumps(protocol), encoding="utf-8")
        assert fiqa._exact_protocol(path, state) == protocol
        protocol["snapshot_id"] = str(uuid4())
        path.write_text(json.dumps(protocol), encoding="utf-8")
        with pytest.raises(ValueError, match="snapshot_id"):
            fiqa._exact_protocol(path, state)
    finally:
        path.unlink(missing_ok=True)


def test_exact_protocol_requires_frozen_independent_role_and_selection() -> None:
    state = _fix_kb(_state(Path(".")))

    for key, value in (
        ("evaluation_role", "development"),
        ("frozen_development_selection", "another-selection.json"),
    ):
        protocol = _protocol(state, **{key: value})
        path = Path.cwd() / f".fiqa-protocol-{uuid4()}.json"
        try:
            path.write_text(json.dumps(protocol), encoding="utf-8")
            with pytest.raises(ValueError, match=key):
                fiqa._exact_protocol(path, state)
        finally:
            path.unlink(missing_ok=True)


def test_fresh_collection_guard_refuses_existing_and_closes_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    class ExistingClient:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        def collection_exists(self, name: str) -> bool:
            calls.append(("exists", name))
            return True

        def close(self) -> None:
            calls.append(("close",))

    monkeypatch.setattr(fiqa, "QdrantClient", ExistingClient)

    with pytest.raises(ValueError, match="absent collection"):
        fiqa.ensure_fresh_collection(fiqa.configuration())

    assert calls[-1] == ("close",)
    assert ("exists", fiqa.COLLECTION) in calls


def test_baseline_row_maps_every_stage_chunk_to_upstream_document() -> None:
    def stage(chunk_id: str, score: float):
        return SimpleNamespace(
            latency_ms=1.0,
            results=(SimpleNamespace(chunk_id=chunk_id, score=score),),
        )

    result = SimpleNamespace(
        trace=SimpleNamespace(
            dense=stage("chunk-a", 0.9),
            sparse=stage("chunk-a", 0.8),
            fusion=stage("chunk-a", 0.7),
            selection=stage("chunk-a", 0.6),
            rerank=stage("chunk-a", 0.5),
        )
    )
    row = fiqa._baseline_row("q1", 0, result, 10.0, {"chunk-a": "doc-a"})

    assert set(row) == {"query_id", "ordinal", "stages", "total_ms"}
    assert set(row["stages"]) == {"dense", "sparse", "fusion", "selection", "rerank"}
    assert row["stages"]["rerank"]["ranking"] == [
        {"chunk_id": "chunk-a", "score": 0.5, "document_id": "doc-a"}
    ]


def test_atomic_state_file_replaces_complete_json() -> None:
    path = Path.cwd() / f".fiqa-state-{uuid4()}.json"
    try:
        fiqa.atomic_dump(path, {"status": "PENDING"})
        fiqa.atomic_dump(path, {"status": "COMPLETE"})

        assert json.loads(path.read_text(encoding="utf-8")) == {"status": "COMPLETE"}
        assert not path.with_suffix(".json.tmp").exists()
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_prepare_reads_corpus_only_and_resumes_final_state_without_reseeding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = Path.cwd() / f".fiqa-state-{uuid4()}.json"
    state = _fix_kb(_state(Path.cwd()))
    state["index_status"] = "PENDING"
    fiqa.atomic_dump(state_path, state)
    args = SimpleNamespace(
        data=Path.cwd() / "data",
        state=state_path,
        output=Path.cwd() / "output",
        protocol=None,
    )
    index = AsyncMock()
    monkeypatch.setattr(
        fiqa, "_seed_corpus", AsyncMock(side_effect=AssertionError("reseeded"))
    )
    monkeypatch.setattr(fiqa, "_index_seed", index)

    try:
        await fiqa.prepare(args)

        index.assert_awaited_once_with(args, state, finalize=True)
    finally:
        state_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_index_resume_uses_bounded_snapshot_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _fix_kb(_state(Path(".")))
    state["chunks"] = 1
    state["chunk_sources"] = {"chunk-a": "doc-a"}
    observed = {"batch": [], "records": 0, "upserts": 0, "finalized": None}

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Engine:
        async def dispose(self) -> None:
            return None

    components = SimpleNamespace(
        dense=SimpleNamespace(warm=lambda: None),
        sparse=SimpleNamespace(warm=lambda: None),
        index=SimpleNamespace(
            ensure_collection=lambda: None,
            upsert=lambda records: observed.update(
                upserts=observed["upserts"] + 1,
                records=observed["records"] + len(records),
            ),
            close=lambda: None,
        ),
    )
    chunk = SimpleNamespace(chunk_id="chunk-a")

    async def resolve(_session, _state):
        return SimpleNamespace(
            workspace_id=uuid4(),
            knowledge_base_id=uuid4(),
            documents=2,
            empty_documents=1,
        )

    async def inventory(_session, _state):
        observed["inventory"] = True
        return 1, 1

    async def batches(_session, _state):
        observed["batch"].append(chunk.chunk_id)
        yield [chunk]

    args = SimpleNamespace(state=Path.cwd() / f".fiqa-state-{uuid4()}.json")
    monkeypatch.setattr(fiqa, "create_database", lambda _url: (Engine(), lambda: Session()))
    monkeypatch.setattr(fiqa, "production_retrieval_components", lambda _config: components)
    monkeypatch.setattr(fiqa, "_resolve_seeded_corpus", resolve)
    monkeypatch.setattr(fiqa, "_seed_inventory", inventory)
    monkeypatch.setattr(fiqa, "_seed_chunk_batches", batches)
    monkeypatch.setattr(
        fiqa,
        "_vector_records",
        lambda _claim, records, _dense, _sparse: tuple(records),
    )
    try:
        await fiqa._index_seed(args, state, finalize=False)
    finally:
        args.state.unlink(missing_ok=True)

    assert observed["inventory"] is True
    assert observed["batch"] == ["chunk-a"]
    assert observed["records"] == observed["upserts"] == 1


@pytest.mark.asyncio
async def test_prepare_refuses_complete_final_state() -> None:
    state_path = Path.cwd() / f".fiqa-state-{uuid4()}.json"
    try:
        fiqa.atomic_dump(state_path, _fix_kb(_state(Path.cwd())))
        args = SimpleNamespace(
            data=Path.cwd(),
            state=state_path,
            output=Path.cwd(),
            protocol=None,
        )

        with pytest.raises(ValueError, match="already complete"):
            await fiqa.prepare(args)
    finally:
        state_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_prepare_checks_fresh_collection_before_reading_corpus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = Path.cwd() / f".fiqa-state-{uuid4()}.json"

    def existing(_config) -> None:
        raise ValueError("collection exists")

    def forbidden_load(_path):
        pytest.fail("corpus opened before fresh collection guard")

    monkeypatch.setattr(fiqa, "ensure_fresh_collection", existing)
    monkeypatch.setattr(fiqa, "_load_corpus", forbidden_load)
    args = SimpleNamespace(
        data=Path.cwd() / "data",
        state=state_path,
        output=Path.cwd(),
        protocol=None,
    )

    with pytest.raises(ValueError, match="collection exists"):
        await fiqa.prepare(args)


@pytest.mark.asyncio
async def test_prepare_resumes_sentinel_with_same_namespaces_and_clears_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = Path.cwd() / f".fiqa-state-{uuid4()}.json"
    sentinel_path = fiqa._prepare_sentinel_path(state_path)
    sentinel = fiqa._seed_sentinel_state()
    fiqa.atomic_dump(sentinel_path, sentinel)
    observed = {}

    def fresh(_config) -> None:
        return None

    def load(_path):
        return {}

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Engine:
        async def dispose(self) -> None:
            return None

    async def seed(_session, _corpus, namespaces):
        observed["namespaces"] = namespaces
        seeded = SimpleNamespace(
            user_id=uuid4(),
            organization_id=uuid4(),
            workspace_id=uuid4(),
            knowledge_base_id=uuid4(),
            snapshot_id=uuid4(),
            snapshot_hash="a" * 64,
            documents=1,
            empty_documents=1,
            chunks=0,
            chunk_sources={},
        )
        fiqa.atomic_dump(state_path, fiqa._snapshot_state(seeded, fiqa.configuration()))
        return seeded

    index = AsyncMock()
    monkeypatch.setattr(fiqa, "ensure_fresh_collection", fresh)
    monkeypatch.setattr(fiqa, "_load_corpus", load)
    monkeypatch.setattr(fiqa, "create_database", lambda _url: (Engine(), lambda: Session()))
    monkeypatch.setattr(fiqa, "_seed_corpus", seed)
    monkeypatch.setattr(fiqa, "_index_seed", index)
    args = SimpleNamespace(
        data=Path.cwd(),
        state=state_path,
        output=Path.cwd(),
        protocol=None,
    )
    try:
        await fiqa.prepare(args)

        assert observed["namespaces"] == (
            UUID(sentinel["document_id_namespace"]),
            UUID(sentinel["revision_id_namespace"]),
        )
        assert not sentinel_path.exists()
    finally:
        state_path.unlink(missing_ok=True)
        sentinel_path.unlink(missing_ok=True)


def test_seed_ids_are_stable_across_retry_with_same_namespaces() -> None:
    namespaces = (uuid4(), uuid4())
    first = fiqa._seed_document_ids("upstream", namespaces)
    retry = fiqa._seed_document_ids("upstream", namespaces)
    identities = fiqa._seed_identity_ids(namespaces)

    assert first == retry
    assert len(set(first)) == 2
    assert len(set(identities)) == 4
    assert set(first).isdisjoint(identities)


@pytest.mark.asyncio
async def test_seed_corpus_adopts_existing_frozen_rows_without_inserting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespaces = (uuid4(), uuid4())
    ids = fiqa._seed_identity_ids(namespaces)
    expected = object()
    recover = AsyncMock(return_value=expected)

    class Session:
        async def scalar(self, _statement):
            return ids[3]

        def add(self, _row) -> None:
            pytest.fail("duplicate insert")

        async def execute(self, *_args, **_kwargs) -> None:
            pytest.fail("duplicate batched insert")

    monkeypatch.setattr(fiqa, "_recover_seeded_corpus", recover)

    actual = await fiqa._seed_corpus(Session(), {}, namespaces)

    assert actual is expected
    recover.assert_awaited_once()


@pytest.mark.asyncio
async def test_seed_corpus_flushes_parent_tables_before_long_document_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table_order: list[str] = []

    class Session:
        async def scalar(self, _statement):
            return None

        def add(self, _row) -> None:
            return None

        async def flush(self) -> None:
            return None

        async def execute(self, statement, rows) -> None:
            table_order.append(statement.table.name)
            assert rows

        async def commit(self) -> None:
            return None

    async def context(*_args):
        return object()

    async def snapshot(*_args):
        return SimpleNamespace(snapshot_id=uuid4(), content_hash="b" * 64)

    monkeypatch.setattr(fiqa, "_authorized_context", context)
    monkeypatch.setattr(
        fiqa.KnowledgeSnapshotService,
        "create_current_snapshot",
        snapshot,
    )
    corpus = {"long": SourceDocument("long", "title", "x" * 250_000)}

    seeded = await fiqa._seed_corpus(Session(), corpus, (uuid4(), uuid4()))

    assert seeded.documents == 1
    assert seeded.chunks >= fiqa.INSERT_BATCH_SIZE
    assert table_order.index("documents") < table_order.index("document_revisions")
    assert table_order.index("document_revisions") < table_order.index("document_chunks")


@pytest.mark.asyncio
async def test_baseline_writes_exposure_before_test_loader_and_rejects_protocol_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = Path.cwd() / f".fiqa-state-{uuid4()}.json"
    protocol_path = Path.cwd() / f".fiqa-protocol-{uuid4()}.json"
    output = Path.cwd() / f".fiqa-output-{uuid4()}"
    exposure = output / "test-exposures.jsonl"
    try:
        state = _fix_kb(_state(Path.cwd()))
        fiqa.atomic_dump(state_path, state)
        protocol = _protocol(state, snapshot_id=str(uuid4()))
        fiqa.atomic_dump(protocol_path, protocol)

        def forbidden_load(*_args, **_kwargs):
            pytest.fail("labels opened")

        loader = forbidden_load
        monkeypatch.setattr(fiqa, "load_beir", loader)
        args = SimpleNamespace(
            state=state_path,
            protocol=protocol_path,
            output=output,
            data=Path.cwd() / "data",
        )

        with pytest.raises(ValueError, match="snapshot_id"):
            await fiqa.baseline(args)

        assert not exposure.exists()
    finally:
        state_path.unlink(missing_ok=True)
        protocol_path.unlink(missing_ok=True)
        if output.exists():
            output.rmdir()


@pytest.mark.asyncio
async def test_baseline_freezes_source_and_records_label_access_before_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = Path.cwd() / f".fiqa-state-{uuid4()}.json"
    protocol_path = Path.cwd() / f".fiqa-protocol-{uuid4()}.json"
    output = Path.cwd() / f".fiqa-output-{uuid4()}"
    baseline_path, _, source_path, exposure = fiqa._baseline_paths(output)
    try:
        state = _fix_kb(_state(Path.cwd()))
        fiqa.atomic_dump(state_path, state)
        fiqa.atomic_dump(protocol_path, _protocol(state))
        args = SimpleNamespace(
            state=state_path,
            protocol=protocol_path,
            output=output,
            data=Path.cwd() / "data",
        )

        def failing_loader(*_args, **_kwargs):
            last = json.loads(exposure.read_text(encoding="utf-8").splitlines()[-1])
            assert last["event"] == "INDEPENDENT_TEST_LABEL_ACCESS"
            assert last["arm"] == "baseline"
            assert last["state_snapshot_id"] == state["snapshot_id"]
            assert source_path.is_file()
            raise RuntimeError("fake loader reached")

        monkeypatch.setattr(fiqa, "load_beir", failing_loader)
        with pytest.raises(RuntimeError, match="fake loader reached"):
            await fiqa.baseline(args)
        assert not baseline_path.exists()
        assert source_path.is_file()
    finally:
        state_path.unlink(missing_ok=True)
        protocol_path.unlink(missing_ok=True)
        if output.exists():
            for path in output.iterdir():
                path.unlink()
            output.rmdir()


def test_source_zip_can_be_created_before_query_loop() -> None:
    source = Path.cwd() / f".fiqa-source-{uuid4()}.zip"
    try:
        fiqa._write_measured_source(
            source,
            {
                "commit": "deadbeef",
                "dirty": True,
                "source_files": ["benchmarks/public_rag/fiqa_baseline.py"],
            },
        )

        assert source.is_file()
        with fiqa.zipfile.ZipFile(source) as archive:
            assert "benchmarks/public_rag/fiqa_baseline.py" in archive.namelist()
            provenance = json.loads(archive.read("provenance.json"))
        assert provenance["commit"] == "deadbeef"
    finally:
        source.unlink(missing_ok=True)
