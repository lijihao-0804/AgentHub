"""Audit previously exposed first HTTP receipts for answer/insufficiency contradictions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from benchmarks.public_rag.qa_first_response_review import index, load
from benchmarks.public_rag.qa_first_response_verify import canon, events_of, require, sha
from packages.knowledge.refusal_consistency import explicit_abstention

DIRECTORY = Path(__file__).resolve().parent / "results/published"
HISTORY = DIRECTORY / "evidence-retention-20261114"
OUT = DIRECTORY / "refusal-consistency-20261114/historical-diagnosis.json"
VIETNAM = "891c4af5bb77d6b8635ec4109572de3401b60631"
COUNT = "c78f18606524539e4c573481e5bf1e0a242cc33c"
PYTHON = "891cab2e41d6ba962778bda297592c916b432226"

# Each quote is checked against the exact excerpt returned by the old B arm.
# These are historical notes, never inputs to the rule or an independent review.
NOTES = {
    VIETNAM: (
        "explicit_abstention_with_background",
        "Both answers explicitly leave the requested platform unidentified. The cited records "
        "attribute the data to shared-task organizers and describe the comments/posts. Neither "
        "names the originating platform. Mentions of Twitter in other visible passages refer to "
        "other research or a different paper. The old B flag made the background a formal answer.",
        {3: "unlabeled dataset for social media comments or posts", 5: "providing the dataset"},
    ),
    "a8f1029f6766bffee38a627477f61457b2d6ed5c": (
        "explicit_abstention_with_related_procedure",
        "B says the origin of human intuitions is missing and then correctly describes checking "
        "metrics against them. Simulations do not explain how human judgments were elicited. A "
        "also calls the simulation a way of obtaining the intuitions. The deterministic rule "
        "leaves both texts intact because the following substantive explanation cannot be "
        "resolved by its narrow grammatical checks.",
        {1: "We conduct a series of simulations", 2: "corresponds well with human intuition"},
    ),
    "6adde6bc3e27a32eac5daa57d30ab373f77690be": (
        "supported_bounded_answer_official_negative",
        "Unlike the opening of A, B limits the comparative claim to some prior studies. The "
        "authors explicitly make that comparison. Its qualification preserves a grounded answer.",
        {3: "sensitive to the context, contrarily to some previous works"},
    ),
    "81686454f215e28987c7ad00ddce5ffe84b37195": (
        "supported_answer_official_negative",
        "Binary and CRF token classification are explicitly identified as the supervised models.",
        {2: "Binary or CRF classification of tokens as products is effective"},
    ),
    "4fcc668eb3a042f60c4ce2e7d008e7923b25b4fc": (
        "supported_bounded_answer_official_negative",
        "Google N-grams is a named corpus for the historical applications. Restricting the "
        "answer to that documented source answers the request at the level the text supports.",
        {5: "we use the diachronic embeddings trained on the Google N-grams corpus"},
    ),
    "7a70fb11cb3449749f0c2c06101965bf5d02c54a": (
        "supported_answer_official_negative",
        "The paper explicitly identifies engineered and learned text representations and how "
        "the framework combines and evaluates them.",
        {1: "a framework for studying learned and engineered representations of text"},
    ),
    "9e2e5918608a2911b341d4887f58a4595d7d1429": (
        "incomplete_observed_amount_in_place_of_requirement",
        "Approximately 160 hours is documented per language in the experiment. The cited "
        "passages do not establish how much is required. A and B report the observed amount "
        "and do not expressly decline the original question.",
        {1: "each language has around 160 hours training data"},
    ),
    "e78a47aec37d9a3bec5a18706b0a462c148c118b": (
        "unsupported_substitution_of_population",
        "The requested population is the languages observed in collected tweets. The only "
        "count in either answer is the number of languages and variants used to translate the "
        "query terms. The count is sourced, but using it for the requested population is "
        "unsupported; the evidence does not show the reader invented a number.",
        {1: "We translated them to 53 languages and variants using Bing translator"},
    ),
}


def lf_digest(path):
    return sha(path.read_text(encoding="utf8"))


def describe(row, verdict, query):
    (reader,) = events_of(row, "reader")
    (retrieval,) = events_of(row, "retrieval")
    entries = {entry["number"]: entry for entry in reader["entries"]}
    require(
        reader["chunks"] == retrieval["reranked"] and len(entries) == len(reader["chunks"]) == 6,
        "The old reader and final six original chunks disagree",
    )
    calls = events_of(row, "http_wire")
    require(
        len(calls) == row["physical_model_requests"]
        and all(c["prompt_sha256"] == reader["prompt_sha256"] for c in calls),
        "The visible input differs from the recorded provider request",
    )
    citations = []
    for c in row["citations"]:
        entry = entries[c["number"]]
        require(
            c["chunk_id"] == entry["chunk_id"]
            and c["document_id"] == entry["document_id"]
            and c["revision"] == entry["document_revision_id"]
            and c["locator"] == entry["locator"]
            and c["excerpt"]
            == entry["text"][c["excerpt_start_in_chunk"] : c["excerpt_end_in_chunk"]]
            and c["excerpt_sha256"] == sha(c["excerpt"]),
            "The old public citation differs from the corresponding reader record",
        )
        citations.append(
            {k: c[k] for k in ("number", "source", "ordinal", "chunk_id", "excerpt_sha256")}
        )
    normalized = None if row["insufficient"] else explicit_abstention(query, row["answer"])
    return {
        "http": row["http"],
        "answer": row["answer"],
        "insufficient_evidence": row["insufficient"],
        "citations": citations,
        "old_blinded_verdict": verdict["verdict"],
        "old_supporting_citations": verdict["supporting_citations"],
        "physical_model_requests": len(calls),
        "provider_finish_reasons": [e["finish_reason"] for e in events_of(row, "response")],
        "reader_prompt_sha256": reader["prompt_sha256"],
        "public_response_sha256": row["response_sha256"],
        "deterministic_rewrite_on_identical_public_fields": normalized,
    }


def build():
    protocol = load(HISTORY / "protocol.json")
    cases = index(protocol["cases"])
    old = load(HISTORY / "review.json")["rows"]
    receipts = {}
    files = [HISTORY / x for x in ("protocol.json", "review.json")]
    for scope in ("independent", "historical"):
        for arm in "AB":
            path = HISTORY / "live" / f"{arm}-{scope}.json"
            receipt = load(path)
            require(
                receipt["arm"] == arm and receipt["protocol_sha256"] == lf_digest(files[0]),
                "Historical receipt identity",
            )
            receipts[scope, arm] = index(receipt["rows"])
            files.append(path)
    negative_answered = {
        qid
        for qid, row in receipts["independent", "B"].items()
        if cases[qid]["negative"] and row["http"] == 200 and row["insufficient"] is False
    }
    require(
        set(NOTES) == negative_answered,
        "Eight-case diagnosis no longer covers every negative answered by B",
    )
    rows = {}
    for qid in sorted(NOTES):
        query = cases[qid]["query"]
        kind, meaning, quotes = NOTES[qid]
        row = receipts["independent", "B"][qid]
        chunks = {c["number"]: c for c in row["citations"]}
        require(
            all(quote in chunks[n]["excerpt"] for n, quote in quotes.items()),
            "Quote outside returned source",
        )
        rows[qid] = {
            "query": query,
            "official_unanswerable": True,
            "diagnostic_category": kind,
            "diagnosis": meaning,
            "short_witnesses_from_B": [{"citation": n, "text": q} for n, q in quotes.items()],
            **{
                arm: describe(receipts["independent", arm][qid], old[qid][label], query)
                for arm, label in (("A", "baseline"), ("B", "candidate"))
            },
        }
    controls = {}
    for qid in (COUNT, PYTHON):
        controls[qid] = {
            "query": cases[qid]["query"],
            **{
                arm: describe(
                    receipts["historical", arm][qid], old[qid][label], cases[qid]["query"]
                )
                for arm, label in (("A", "baseline"), ("B", "candidate"))
            },
        }
    return {
        "scope": (
            "Previously exposed development data from evidence-retention-20261114 only. A uses "
            "whole-chunk retrieval. B adds evidence-sentence retrieval. Both used the old reader."
        ),
        "interpretation": (
            "The HTTP answer, flag, cited excerpts, input digest, physical request count and "
            "finish reason were recorded. Original provider response bodies and first-attempt "
            "JSON are absent. Each of the eight B responses followed one provider POST. In the "
            "old terminal code the public flag is copied from the parsed flag and matching "
            "numbered citations are required. That reconstruction does not recover provider "
            "bytes or hidden reasoning. The deterministic rewrite field is a counterfactual "
            "computed from the public fields and returns the replacement refusal text or null "
            "for an unchanged reply."
        ),
        "coverage_limits": (
            "This candidate recognizes certain explicit English or Chinese whole-question "
            "abstentions and a restricted English single-entity pattern. It does not perform "
            "source entailment or resolve other languages and paraphrases. Among the eight old "
            "positive B responses to official negatives, it changes only the platform response. "
            "The human-intuitions response also declines the main question but explains a "
            "related procedure. Automatically suppressing all replies with missing-information "
            "language would also discard answers with a known bound, named subset, or partial "
            "definition. The training-hour requirement and tweet-language population require "
            "a separate interpretation of the question and source. Four other replies answer "
            "the question within a scope supported by their citations despite the official label."
        ),
        "source_sha256_lf": {
            str(p.relative_to(HISTORY)).replace("\\", "/"): lf_digest(p) for p in files
        },
        "original_official_negatives": 18,
        "original_B_official_negatives_answered": len(negative_answered),
        "original_B_correct_official_refusals": sum(
            old[i]["candidate"]["verdict"] == "refusal" and cases[i]["negative"]
            for i in receipts["independent", "B"]
        ),
        "official_negatives_answered_by_B": rows,
        "other_exposed_controls": controls,
    }


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--write", action="store_true")
    cli.add_argument("--output", type=Path, default=OUT)
    args = cli.parse_args()
    result = build()
    if args.write:
        args.output.write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf8", newline="\n"
        )
    else:
        require(canon(result) == canon(load(args.output)), "Published historical diagnosis differs")
    print(f"Verified {len(NOTES)} previously answered official negatives and two exposed controls.")


if __name__ == "__main__":
    main()
