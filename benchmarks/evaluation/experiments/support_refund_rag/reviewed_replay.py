"""Rescore the historical DEV corpus without changing its original labels or dispatch record."""

import argparse
import json
from pathlib import Path

from .reviewed_dev import reviewed_cases
from .runtime_trial import ROOT, coverage, read, save


def assess():
    corpus = {
        d["source_id"]: (ROOT / "data" / d["path"]).read_text(encoding="utf8")
        for d in read(ROOT / "data/manifest.json")["documents"]
    }
    rows = []
    for q in reviewed_cases():
        for g in q["evidence_groups"]:
            for atom in g["all_of"]:
                if atom["quote"] not in corpus[atom["source_id"]]:
                    raise ValueError(f"absent reviewed quote for {q['case_id']}")
        prior = read(ROOT / "private/admission" / (q["case_id"] + ".json"))
        visible = []
        for msg in prior["model_view"]["messages"]:
            if msg["role"] == "tool" and msg["name"] == "search_knowledge":
                for r in json.loads(msg["content"])["data"]["results"]:
                    visible.append({**r, "source_id": r["document_name"].split("__")[0]})
        rows.append(
            {
                "case_id": q["case_id"],
                "answerability": q["answerability"],
                "original_complete": prior["gold_complete"],
                "reviewed_complete": coverage(q, visible),
                "approved_alternatives": q["evidence_groups"][1:],
            }
        )
    return {
        "scope": "Historical fixed S1 request, reviewed DEV labels only; no generation",
        "model_calls": 0,
        "rows": rows,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    save(parser.parse_args().output, assess())
