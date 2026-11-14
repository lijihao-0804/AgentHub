"""Build an arm-blinded, de-duplicated manual evidence review packet."""

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PUBLIC = ROOT / "benchmarks/public_rag/results/published/article-recall-20261114/live"
WORK = ROOT / ".scratch/article-live-20261114"


def read(p):
    return json.loads(p.read_bytes())


def write(p, value):
    p.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf8")


def build(scope, *, raw_dir=PUBLIC, output=WORK):
    protocol = read(PUBLIC / "protocol-public.json")
    if scope == "qasper":
        from benchmarks.public_rag.article_live_replay import project

        train = ROOT / "data/external/rag-public-20261021/qasper/extracted/qasper-train-v0.3.json"
        recovered = {c["id"]: c["evidence"] for c in project(train)[1]}
        for c in protocol["cases"]:
            if c["id"] in recovered:
                c["evidence"] = recovered[c["id"]]
    cases = {r["id"]: r for r in protocol["cases"] if r["scope"] == scope}
    blind = {}
    key = {}
    for arm in protocol["arms"]:
        rows = read(raw_dir / f"{scope}-{arm}.json")["rows"]
        assert {r["id"] for r in rows} == set(cases)
        key[arm] = {}
        for r in rows:
            assert r["state"] == "returned"
            c = cases[r["id"]]
            item = {
                "id": r["id"],
                "query": c["query"],
                "reference_answers": c.get("answers"),
                "reference_evidence": c.get("evidence"),
                "reference_docs": c["gold_documents"],
                "http": r["http"],
                "answer": r.get("answer"),
                "insufficient": r.get("insufficient"),
                "citations": [
                    {k: v for k, v in h.items() if k in {"number", "source", "excerpt"}}
                    for h in r.get("citations", [])
                ],
            }
            digest = hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()[:12]
            blind[digest] = item
            key[arm][r["id"]] = digest
    write(output / f"{scope}-blinded.json", dict(sorted(blind.items())))
    write(output / f"{scope}-key.json", key)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scope", choices=("full", "qasper"))
    parser.add_argument(
        "--raw-dir",
        type=Path,
        required=True,
        help="Directory of original, uncompressed capture outputs.",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    build(args.scope, raw_dir=args.raw_dir, output=args.output)
