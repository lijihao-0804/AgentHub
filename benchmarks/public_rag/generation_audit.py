"""GEN-0: deterministic five-bucket decomposition of gold-arm answer loss.

Buckets (priority R > F > G > S-UNKNOWN > E, one per question):
  R refusal calibration, F format disobedience, G granularity mismatch,
  S scoring-alias suspects (UNKNOWN only, never forced), E reading errors.

Loss unit: 1 - token_f1 per question against the frozen references. The
classifier reuses the project scorer's normalization so bucket judgments and
F1 share one vocabulary.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from benchmarks.public_rag.consumer_answer_metrics import _answer_tokens

_REFUSAL_START = re.compile(
    r"^(insufficient evidence|no (relevant )?evidence|cannot (answer|determine|find)|"
    r"not (enough|sufficient)|unable to (answer|determine)|the evidence (does|doesn't|does not))",
    re.IGNORECASE,
)
_POLARITY = {"yes", "no", "true", "false"}


@dataclass(frozen=True)
class BucketedAnswer:
    query_id: str
    prediction: str
    unanswerable: bool
    references: tuple[str, ...]
    token_f1: float
    bucket: str
    note: str = ""


def _tokens(text: str) -> list[str]:
    return _answer_tokens(text)


def _best_alias(prediction: str, references: Sequence[str]) -> tuple[float, list[str]]:
    """Return (best F1, best alias tokens) using the project scorer's rules."""
    from benchmarks.public_rag.consumer_answer_metrics import _token_f1

    prediction_tokens = _tokens(prediction)
    best_f1, best_tokens = 0.0, []
    for reference in references:
        reference_tokens = _tokens(reference)
        score = _token_f1(prediction_tokens, reference_tokens)
        if score > best_f1 or (score == best_f1 and len(reference_tokens) > len(best_tokens)):
            best_f1, best_tokens = score, reference_tokens
    return best_f1, best_tokens


def _subset_multiset(superset: list[str], subset: list[str]) -> bool:
    from collections import Counter

    counts = Counter(superset)
    for token, needed in Counter(subset).items():
        if counts.get(token, 0) < needed:
            return False
    return True


def _is_polarity_question(references: Sequence[str]) -> bool:
    alias_tokens = [_tokens(reference) for reference in references]
    nonempty = [tokens for tokens in alias_tokens if tokens]
    if not nonempty:
        return False
    return all(len(tokens) == 1 and tokens[0] in _POLARITY for tokens in nonempty)


def classify(
    *,
    query_id: str,
    prediction: str,
    unanswerable: bool,
    references: Sequence[str],
    token_f1: float,
) -> BucketedAnswer:
    if token_f1 >= 0.999:
        return BucketedAnswer(
            query_id, prediction, unanswerable, tuple(references), token_f1, "NONE"
        )
    refusal = unanswerable or bool(_REFUSAL_START.match(prediction.strip() or ""))
    if refusal and any(reference.strip() for reference in references):
        return BucketedAnswer(query_id, prediction, unanswerable, tuple(references), token_f1, "R")

    _, best_alias_tokens = _best_alias(prediction, references)
    prediction_tokens = _tokens(prediction)

    # F1: polarity questions answered correctly up front, then embellished.
    if _is_polarity_question(references):
        polarity_token = best_alias_tokens[0] if best_alias_tokens else ""
        if polarity_token and prediction_tokens and prediction_tokens[0] == polarity_token:
            return BucketedAnswer(
                query_id, prediction, unanswerable, tuple(references), token_f1, "F",
                note="polarity correct, extra discourse",
            )
    # F2: every reference token already hit, but the prediction is >= 3x longer.
    if best_alias_tokens and _subset_multiset(prediction_tokens, best_alias_tokens):
        if len(prediction_tokens) >= 3 * len(best_alias_tokens):
            return BucketedAnswer(
                query_id, prediction, unanswerable, tuple(references), token_f1, "F",
                note="all reference tokens hit, prediction >= 3x reference length",
            )
        # Correct answer present plus extra detail: granularity, not format.
        return BucketedAnswer(
            query_id, prediction, unanswerable, tuple(references), token_f1, "G",
            note="reference subset of prediction",
        )

    # S: alias/synonym suspects -- flagged UNKNOWN, never forced into a bucket.
    if best_alias_tokens and prediction_tokens:
        overlap = len(set(prediction_tokens) & set(best_alias_tokens)) / max(
            len(set(best_alias_tokens)), 1
        )
        if 0.0 < overlap < 0.5:
            return BucketedAnswer(
                query_id, prediction, unanswerable, tuple(references), token_f1, "S",
                note="alias-suspect: partial lexical overlap (UNKNOWN)",
            )
    return BucketedAnswer(query_id, prediction, unanswerable, tuple(references), token_f1, "E")


def audit_run(
    rows: Sequence[Mapping],
    *,
    arm: str = "gold_budgeted",
    references_by_query: Mapping[str, Sequence[str]] | None = None,
) -> dict:
    references_by_query = references_by_query or {}
    bucketed: list[BucketedAnswer] = []
    for row in rows:
        arm_data = row.get("arms", {}).get(arm)
        if not isinstance(arm_data, Mapping):
            continue
        api = arm_data.get("api", {})
        if api.get("status") != "VALID":
            continue
        bucketed.append(
            classify(
                query_id=row["query_id"],
                prediction=api.get("answer") or "",
                unanswerable=bool(api.get("unanswerable")),
                references=references_by_query.get(row["query_id"])
                or row.get("answers")
                or [],
                token_f1=float(arm_data.get("token_f1", 0.0)),
            )
        )
    loss_by_bucket: dict[str, float] = {}
    count_by_bucket: dict[str, int] = {}
    examples: dict[str, list[dict]] = {}
    for item in bucketed:
        if item.bucket == "NONE":
            continue
        lost = 1.0 - item.token_f1
        loss_by_bucket[item.bucket] = loss_by_bucket.get(item.bucket, 0.0) + lost
        count_by_bucket[item.bucket] = count_by_bucket.get(item.bucket, 0) + 1
        examples.setdefault(item.bucket, []).append(
            {
                "query_id": item.query_id,
                "prediction": item.prediction[:220],
                "references": list(item.references)[:3],
                "token_f1": round(item.token_f1, 4),
                "note": item.note,
            }
        )
    total_loss = sum(loss_by_bucket.values())
    shares = {
        bucket: {
            "lost_points": round(loss, 3),
            "share_pct": round(100 * loss / total_loss, 2) if total_loss else 0.0,
            "questions": count_by_bucket.get(bucket, 0),
        }
        for bucket, loss in sorted(loss_by_bucket.items(), key=lambda kv: -kv[1])
    }
    s_unknown_share = sum(
        row["share_pct"] for bucket, row in shares.items() if bucket in ("S",)
    )
    biggest = max(shares, key=lambda bucket: shares[bucket]["share_pct"]) if shares else None
    stop_scoring_dominates = s_unknown_share >= 50.0
    stop_no_dominant = bool(shares) and all(row["share_pct"] < 10.0 for row in shares.values())
    return {
        "questions": len(bucketed),
        "total_lost_points": round(total_loss, 3),
        "buckets": shares,
        "examples": {bucket: values[:3] for bucket, values in examples.items()},
        "recoverable_upper_bound_points": round(
            loss_by_bucket.get("F", 0.0)
            + loss_by_bucket.get("G", 0.0)
            + 0.5 * loss_by_bucket.get("R", 0.0),
            3,
        ),
        "stop_conditions": {
            "scoring_dominates_s_ge_50pct": stop_scoring_dominates,
            "no_dominant_bucket_all_below_10pct": stop_no_dominant,
            "biggest_bucket": biggest,
        },
    }


def main() -> None:
    run_dir = Path(__file__).parent / "results" / "generation-audit-20261025"
    source = (
        Path(__file__).parent
        / "results"
        / "consumer-rag-20261023"
        / "confirmation-answers-1"
        / "run.jsonl"
    )
    rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()]
    # References live in the frozen dataset, exactly as the replay harness
    # resolves them (consumer_replay.load -> queries[qid]["answers"]).
    from argparse import Namespace

    from benchmarks.public_rag.scenario_runner import load

    meta_path = (
        Path(__file__).parent
        / "results"
        / "consumer-rag-20261023"
        / "confirmation-candidates-1"
        / "meta.json"
    )
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    data = load(
        Namespace(
            dataset=meta["dataset"],
            split=meta["split"],
            data=Path("data/external/rag-public-20261021/musique/extracted/data"),
        )
    )
    references = {
        query["query_id"]: query.get("answers") or [] for query in data["queries"]
    }
    report = audit_run(rows, references_by_query=references)
    (run_dir / "generation-audit-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    printable = {k: v for k, v in report.items() if k != "examples"}
    print(json.dumps(printable, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
