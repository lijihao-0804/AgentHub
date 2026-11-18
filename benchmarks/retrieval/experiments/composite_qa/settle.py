"""Reconcile the paid one-shot ledger and prepare arm-blind answer/citation review packets."""

import argparse
import json
import random
import re
import unicodedata
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

import httpx

from benchmarks.retrieval.table_header_answers import receipt_cost
from benchmarks.retrieval.table_header_local_qa import score as score_table
from packages.knowledge.citation_qa import _InvalidCitationAnswer, resolve_citation_answer

from .freeze import rows, sha
from .runner import GROUPS, PREFLIGHT, PRIVATE, load, material, save
from .wire import MODEL, digest, reservation


def verify_union(case, item, selected, ranked):
    loc = selected["locator"]
    if item["excerpt"] != selected["text"]:
        raise ValueError("The final composite citation was truncated.")
    spans = {v["chunk_id"]: v for v in ranked}
    parts = item.get("components", [])
    if len(parts) != 2 or loc["components"] != [
        {"chunk_id": p["chunk_id"], "locator": p["locator"]} for p in parts
    ]:
        raise ValueError("Composite component identities or coordinates changed.")
    for part in parts:
        original = spans[part["chunk_id"]]
        if (original["locator"], original["revision"], original["source_id"], original["text"]) != (
            part["locator"],
            selected["revision"],
            selected["source_id"],
            part["excerpt"],
        ):
            raise ValueError("Composite provenance differs from original ranked chunk.")
    if "paragraphs" in case:
        paragraph = next(p for p in case["paragraphs"] if p["source_id"] == selected["source_id"])
        source = (PRIVATE.parent / paragraph["path"]).read_bytes().decode()
        if source[loc["char_start"] : loc["char_end"]] != item["excerpt"]:
            raise ValueError("Composite excerpt differs from the registered source bytes.")


def stage_results(case, row):
    first = row["events"][0]
    selected = {v["id"]: v for v in row["selected"]}
    try:
        parsed = resolve_citation_answer(
            case["question"],
            first["content"],
            visible_ids=set(selected),
            output_limit_reached=first["finish"] == "length",
        )
        first_answer = dict(
            valid=True,
            answer=parsed.answer,
            refused=parsed.insufficient_evidence,
            citations=[selected[i] for i in parsed.citation_ids],
        )
    except _InvalidCitationAnswer:
        first_answer = dict(valid=False, answer="", refused=False, citations=[])
    final = dict(valid=False, answer="", refused=False, citations=[])
    if row["http"] == 200:
        body = row["body"]
        citations = []
        for item in body["citations"]:
            s = selected[item["id"]]
            if (s["chunk_id"], s["revision"], s["locator"]) != (
                item["chunk_id"],
                item["document_revision_id"],
                item["locator"],
            ):
                raise ValueError("Final citation identity differs from presented source")
            if not s["text"].startswith(item["excerpt"]):
                raise ValueError("Final citation excerpt differs from source")
            if s["locator"].get("type") == "source_union":
                verify_union(case, item, s, row["ranked"])
            citations.append(dict(s, text=item["excerpt"]))
        final = dict(
            valid=True,
            answer=body["answer"],
            refused=body["insufficient_evidence"],
            citations=citations,
        )
    return {"first": first_answer, "final": final}


def normalized(text):
    text = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(re.findall(r"\w+", text))


def has_alias(answer, aliases):
    return any(
        " " + normalized(alias) + " " in " " + normalized(answer) + " "
        for alias in aliases
        if normalized(alias)
    )


def covers(span, cited):
    ends = sorted(
        (
            x["locator"]["char_start"],
            min(x["locator"]["char_end"], x["locator"]["char_start"] + len(x["text"])),
        )
        for x in cited
        if x["source_id"] == span["source_id"] and isinstance(x["locator"].get("char_start"), int)
    )
    end = span["start"]
    for left, right in ends:
        if left > end:
            break
        end = max(end, right)
    return end >= span["end"]


def screen(group, case, result):
    if not result["valid"]:
        return dict(value=False, support=False, strict=False)
    if group == "W":
        response = httpx.Response(
            200,
            json=dict(
                answer=result["answer"],
                insufficient_evidence=result["refused"],
                citations=[
                    {"locator": v["locator"], "excerpt": v["text"]} for v in result["citations"]
                ],
            ),
        )
        w = score_table(response, case["value"], case["cell"])
        return dict(value=w["value_correct"], support=w["cited_cell"], strict=w["joint"])
    if result["refused"]:
        good = not case.get("answerable", True)
        return dict(value=good, support=good, strict=good)
    answer = re.sub(r"\[\d+\]", "", result["answer"])
    if group == "fresh":
        value = has_alias(answer, [case["answer"], *case["answer_aliases"]])
        support = all(covers(g, result["citations"]) for g in case["gold_spans"])
    else:
        doc = (
            PRIVATE
            / ("first-refusal-100-docs" if group == "R" else "bilingual-refusal-100-docs")
            / (case["doc_id"] + ".txt")
        ).read_text(encoding="utf8")
        value = case["answerable"] and all(has_alias(answer, f["aliases"]) for f in case["facets"])

        def collapse(t):
            return " ".join(t.split())

        support = case["answerable"] and all(
            any(
                collapse(doc[left:right]) in collapse(x["text"])
                for left, right in facet["support"]
                for x in result["citations"]
                if x["source_id"] == case["doc_id"]
            )
            for facet in case["facets"]
        )
    return dict(value=value, support=support, strict=value and support)


def reconcile(directory):
    freeze = load(PREFLIGHT / "freeze.json")
    required = {k for k, p in freeze["plan"].items() if p["send"]}
    (ledger,) = directory.glob("ledger-*.jsonl")
    events = rows(ledger)
    ids, by_key = set(), defaultdict(list)
    totals = Counter()
    if len(events) % 2:
        raise ValueError("Open reservation: do not resume")
    for n, (reserve, settle) in enumerate(zip(events[::2], events[1::2], strict=True), 1):
        if (reserve["state"], settle["state"], reserve["call"], settle["call"]) != (
            "reserved",
            "confirmed",
            n,
            n,
        ):
            raise ValueError("Unconfirmed or duplicate physical attempt")
        key = reserve["key"]
        if (
            key not in required
            or key != settle["key"]
            or reserve["attempt"] != settle["attempt"]
            or settle["id"] in ids
        ):
            raise ValueError("Unregistered key or duplicate provider receipt")
        receipt = dict(
            id=settle["id"],
            created=settle["created"],
            model=MODEL,
            usage=dict(
                prompt_tokens=settle["input"],
                completion_tokens=settle["output"],
                total_tokens=settle["input"] + settle["output"],
                prompt_tokens_details={"cached_tokens": settle["cached"]},
            ),
        )
        inp, out, cache, cost = receipt_cost(
            receipt, MODEL, max_input=reserve["input_bound"], max_output=256
        )
        if (
            cost != Decimal(settle["estimated_cny"])
            or Decimal(reserve["cny"]) != reservation(reserve["input_bound"])
            or cost > Decimal(reserve["cny"])
            or settle["attempt"] != len(by_key[key]) + 1
        ):
            raise ValueError("Usage differs from frozen tariff or attempt sequence")
        ids.add(settle["id"])
        by_key[key].append(settle["id"])
        totals.update(dict(input=inp, output=out, cached=cache, cny=cost, calls=1))
    if set(by_key) != required or len(ids) > freeze["max_wire"]:
        raise ValueError("Physical calls omit a question or exceed the ceiling")
    units, keys, counts = {}, [], Counter()
    for group in GROUPS:
        cases = {c["id"]: c for c in material(group)}
        for row in rows(directory / (group + ".jsonl")):
            case = cases[row["id"]]
            key = row["id"] + "/" + row["arm"]
            if [e["id"] for e in row["events"]] != by_key.pop(key):
                raise ValueError("HTTP attempts differ from confirmed receipts")
            for stage, r in stage_results(case, row).items():
                scored = screen(group, case, r)
                visible = [
                    dict(id=x["id"], text=x["text"], locator=x["locator"], source=x["source_id"])
                    for x in row["selected"]
                ]
                packet = dict(
                    question=case["question"],
                    answer=r["answer"],
                    refused=r["refused"],
                    valid=r["valid"],
                    cited=[x["id"] for x in r["citations"]],
                    cited_excerpts=[
                        dict(
                            id=x["id"], text=x["text"], locator=x["locator"], source=x["source_id"]
                        )
                        for x in r["citations"]
                    ],
                    citation_instruction="Judge cited support only from cited_excerpts.",
                    visible=visible,
                    gold={
                        k: case[k]
                        for k in (
                            "answer",
                            "answer_aliases",
                            "answerable",
                            "target",
                            "facets",
                            "language",
                            "cell",
                            "value",
                            "gold_spans",
                        )
                        if k in case
                    },
                )
                uid = digest(packet)
                units[uid] = dict(uid=uid, **packet)
                keys.append(dict(id=row["id"], arm=row["arm"], stage=stage, uid=uid, screen=scored))
                counts[group, row["arm"], stage] += scored["strict"]
    if by_key:
        raise ValueError("No logical answer record for some physical receipts")
    save(
        directory / "billing.json",
        {k: str(v) if isinstance(v, Decimal) else v for k, v in totals.items()}
        | dict(unique_receipts=len(ids), unknown=0, ledger_sha256=sha(ledger)),
    )
    save(directory / "review-keys.json", keys)
    packets = list(units.values())
    random.Random(20261118).shuffle(packets)
    save(directory / "blind-packets.json", packets)
    print(
        json.dumps(
            {
                "screen": {"/".join(k): v for k, v in counts.items()},
                "billing": load(directory / "billing.json"),
            }
        )
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("directory", type=Path)
    reconcile(p.parse_args().directory)
