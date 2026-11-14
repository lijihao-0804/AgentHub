from benchmarks.public_rag.article_parent_methods import select_parent_chunks


def baseline(docs):
    return [
        {"chunk_id": f"c{i}", "document_id": d, "score": 1 / (1 + i)} for i, d in enumerate(docs)
    ]


def test_parent_replacement_preserves_prefix_original_sources_and_budget():
    old = baseline(list("abcdefgh") + list("abcdefghijij"))
    parents = ["a", "k", "k", "m", "n", "p", "q"]
    chunks = {d: f"child-{d}" for d in "kmnpq"}
    pool = select_parent_chunks(old, parents, chunks)
    assert len(pool) == len(old) == 20
    assert pool[:8] == old[:8]
    assert pool[-4:] == [
        {"chunk_id": f"child-{d}", "document_id": d, "score": old[-1]["score"] / (2 + i)}
        for i, d in enumerate("kmnp")
    ]
    assert {h["document_id"] for h in old} <= {h["document_id"] for h in pool}
    assert pool[:-4] == [h for i, h in enumerate(old) if i not in {14, 15, 18, 19}]


def test_parent_replacement_clips_to_real_novel_parents_and_top20():
    old = baseline(["a"] * 20)
    assert select_parent_chunks(old, ["a"] * 20 + ["x"], {}) == old
    pool = select_parent_chunks(old, ["x", "x", "a"], {"x": "child-x"})
    assert pool[:-1] == old[:-1]
    assert pool[-1]["chunk_id"] == "child-x"


def test_no_replaceable_duplicates():
    old = baseline([str(i) for i in range(20)])
    assert select_parent_chunks(old, ["new"], {}) == old


def test_best_dense_child_checks_later_chunks_and_stable_id_ties():
    import numpy as np

    from benchmarks.public_rag.article_parent_run import best_dense_children

    chunks = [{"chunk_id": c} for c in ("z", "a", "f", "b")]
    matrix = np.array([[1.0, 0.0], [1.0, 0.0], [0.0, 1.0], [1.0, 0.0]])
    selected = best_dense_children(
        ["d", "e"], {"d": [0, 1], "e": [2, 3]}, chunks, matrix, np.array([1.0, 0.0])
    )
    assert selected == {"d": "a", "e": "b"}


def test_source_and_official_sentence_visibility_can_disagree():
    from benchmarks.public_rag.article_parent_report import measures

    chunks = {"a": {"char_start": 0, "char_end": 15}, "b": {"char_start": 10, "char_end": 40}}
    old = [{"chunk_id": "a", "document_id": "d"}, {"chunk_id": "b", "document_id": "d"}]
    state = measures(old, old[:1], chunks, {"d"}, {"d": [{"sentences": [0]}]}, {"d": [(10, 20)]})
    assert state == {"source20": ["d"], "source5": ["d"], "evidence20": ["d"], "evidence5": []}
