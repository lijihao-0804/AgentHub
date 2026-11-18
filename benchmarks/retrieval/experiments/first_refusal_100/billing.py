"""Reconcile the completed independent and WAI runs into a public cost summary."""

import argparse
import hashlib
import json
import re
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path

from packages.core.canonical.json_parse import load_unique_json

from .dataset import canonical_digest

ARMS = ("baseline", "candidate")


def fingerprint(value, digits=64):
    if not isinstance(value, str) or not re.fullmatch(rf"[0-9a-f]{{{digits}}}", value):
        raise ValueError("Invalid source or artifact fingerprint.")
    return value


def amount(value):
    try:
        number = Decimal(value) if isinstance(value, str) else None
    except InvalidOperation:
        number = None
    if number is None or not number.is_finite() or number < 0:
        raise ValueError("Expected a nonnegative decimal amount.")
    return number


def add_receipts(data, groups, seen, reserve, *, wai_arm=None, expected=None):
    entries = [load_unique_json(line.decode()) for line in data.splitlines()]
    if not entries or len(entries) % 2:
        raise ValueError("The ledger contains a missing reservation or confirmation.")
    if expected is not None and len(entries) != 2 * len(expected):
        raise ValueError("The WAI ledger and completed results have different call counts.")
    attempts = {arm: Counter() for arm in ARMS}
    pairs = zip(entries[::2], entries[1::2], strict=True)
    for call, (reserved, confirmed) in enumerate(pairs, 1):
        if (
            reserved["state"] != "reserved"
            or confirmed["state"] != "confirmed"
            or any(type(r["call"]) is not int or r["call"] != call for r in (reserved, confirmed))
            or amount(reserved["cny"]) != reserve
        ):
            raise ValueError("The reservation and confirmed physical call do not match.")
        arm = wai_arm
        if arm is None:
            question, _, arm = confirmed["key"].rpartition("/")
            if not question or arm not in ARMS or reserved["key"] != confirmed["key"]:
                raise ValueError("The reservation and confirmation have different question arms.")
            attempts[arm][question] += 1
            attempt = attempts[arm][question]
            if attempt > 2 or any(
                type(r["attempt"]) is not int or r["attempt"] != attempt
                for r in (reserved, confirmed)
            ):
                raise ValueError("Missing, duplicated or out-of-order question attempt.")
        receipt = confirmed["id"]
        if not isinstance(receipt, str) or not receipt.strip() or receipt in seen:
            raise ValueError("Missing or repeated provider receipt across the campaign.")
        seen.add(receipt)
        inp, cached, out = (confirmed[k] for k in ("input", "cached", "output"))
        estimate = amount(confirmed["estimated_cny"])
        if (
            any(type(v) is not int for v in (inp, cached, out))
            or not 0 <= cached <= inp
            or inp < 1
            or out < 0
            or estimate > reserve
        ):
            raise ValueError("Invalid usage counts or cost above reservation.")
        if expected is not None:
            other = expected[call - 1]
            if (call, inp, cached, out, estimate) != (
                other["call"],
                other["input"],
                other["cached"],
                other["output"],
                amount(other["estimated_cny"]),
            ):
                raise ValueError("The WAI ledger and completed results disagree on usage or cost.")
        groups[arm].update(
            confirmed_calls=1,
            input_tokens=inp,
            cached_input_tokens=cached,
            output_tokens=out,
            gross_reserved_cny=reserve,
            estimated_cny=estimate,
        )
    return attempts


def combine(groups):
    total = Counter()
    for group in groups:
        total.update(group)
    return total


def settle(independent, wai_baseline, wai_candidate, *, manifest=None):
    files = {}

    def read(folder, label, name):
        data = (folder / name).read_bytes()
        files[f"{label}/{name}"] = hashlib.sha256(data).hexdigest()
        return data

    execution = load_unique_json(read(independent, "independent", "execution.json").decode())
    freeze = load_unique_json(read(independent, "independent", "freeze.json").decode())
    count = freeze["registered_questions"]
    if (
        execution["offline"] is not False
        or freeze["offline"] is not False
        or execution["unknowns"] != 0
        or count != 100
        or freeze["max_wire_calls"] != 4 * count
    ):
        raise ValueError("Expected the completed, paid 100-question campaign.")
    reserve = amount(freeze["max_cny"]) / freeze["max_wire_calls"]
    independent_groups = {arm: Counter() for arm in ARMS}
    wai_groups = {arm: Counter() for arm in ARMS}
    seen = set()
    attempts = add_receipts(
        read(independent, "independent", "receipts.jsonl"), independent_groups, seen, reserve
    )
    if (
        len(attempts["baseline"]) != count
        or attempts["baseline"].keys() != attempts["candidate"].keys()
    ):
        raise ValueError("The two ledgers do not cover every independent question in both arms.")
    for group in independent_groups.values():
        group.update(first_http_requests=count, corrections=group["confirmed_calls"] - count)
    subtotal = combine(independent_groups.values())
    if (
        execution["first_http_requests"] != subtotal["first_http_requests"]
        or execution["confirmed_wire_calls"] != subtotal["confirmed_calls"]
        or execution["internal_corrections"] != subtotal["corrections"]
        or amount(execution["estimated_cny"]) != subtotal["estimated_cny"]
    ):
        raise ValueError("The independent ledger disagrees with the completed execution audit.")

    for arm, folder, n_questions in (
        ("baseline", wai_baseline, 4),
        ("candidate", wai_candidate, 14),
    ):
        cohort = f"wai-{arm}"
        result = load_unique_json(read(folder, cohort, "results.json").decode())
        if result["arm"] != arm or len(result["cases"]) != n_questions:
            raise ValueError("The WAI result does not match the registered cohort.")
        expected = []
        for case in result["cases"]:
            per_request = case["generated"]
            if not 1 <= per_request["calls"] == len(per_request["receipts"]) <= 2:
                raise ValueError("Expected one or two confirmed receipts per WAI question.")
            expected.extend(per_request["receipts"])
        add_receipts(
            read(folder, cohort, "receipts.jsonl"),
            wai_groups,
            seen,
            reserve,
            wai_arm=arm,
            expected=expected,
        )
        group = wai_groups[arm]
        group.update(
            first_http_requests=n_questions, corrections=group["confirmed_calls"] - n_questions
        )

    documents = {key: fingerprint(doc["source_sha256"]) for key, doc in freeze["inputs"].items()}
    if len(documents) != count:
        raise ValueError("The frozen source documents do not cover the registered cohort.")
    if manifest is not None:
        raw = manifest.read_bytes()
        upload = load_unique_json(raw.decode())
        if (
            canonical_digest(sorted(upload["cases"], key=lambda case: case["id"]))
            != freeze["cases_sha256"]
            or {case["id"] for case in upload["cases"]} != attempts["baseline"].keys()
            or len(upload["documents"]) != count
            or {doc["id"]: doc["sha256"] for doc in upload["documents"]} != documents
        ):
            raise ValueError("The supplied upload manifest differs from the executed sample.")
        files["upload_manifest"] = hashlib.sha256(raw).hexdigest()
    weights = freeze["model_files_sha256"]
    total = combine([subtotal, *wai_groups.values()])
    return {
        "source_commit": fingerprint(freeze["source_commit"], 40),
        "cases_sha256": fingerprint(freeze["cases_sha256"]),
        "documents_sha256": canonical_digest(documents),
        "prompt_sha256": {arm: fingerprint(freeze["prompt_sha256"][arm]) for arm in ARMS},
        "weight_sha256": {
            "embedding": fingerprint(weights["embedding"]["pytorch_model.bin"]),
            "reranker": fingerprint(weights["reranker"]["model.safetensors"]),
        },
        "file_sha256": files,
        "independent": {**independent_groups, "total": subtotal},
        "wai": {**wai_groups, "total": combine(wai_groups.values())},
        "total": {**total, "distinct_provider_receipts": len(seen), "unconfirmed_calls": 0},
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("independent", "wai-baseline", "wai-candidate", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument(
        "--manifest", type=Path, help="Also fingerprint the original upload manifest."
    )
    args = parser.parse_args()
    result = settle(args.independent, args.wai_baseline, args.wai_candidate, manifest=args.manifest)
    args.output.write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf8")
