import hashlib
import json
from decimal import Decimal

import pytest

from benchmarks.retrieval.experiments.first_refusal_100.billing import settle
from benchmarks.retrieval.experiments.first_refusal_100.dataset import canonical_digest

PRICE = Decimal("0.0001042")
RESERVE = "0.018432"


def save(path, record):
    path.write_text(json.dumps(record), encoding="utf8")


def write_lines(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf8")


def read_lines(path):
    return [json.loads(line) for line in path.read_bytes().splitlines()]


@pytest.fixture
def run_dirs(tmp_path):
    dirs = tuple(tmp_path / name for name in ("independent", "wai-baseline", "wai-candidate"))
    for directory, count in zip(dirs, (100, 4, 14), strict=True):
        directory.mkdir()
        rows, cases = [], []
        independent = directory == dirs[0]
        arms = ("baseline", "candidate") if independent else (directory.name.removeprefix("wai-"),)
        for question in range(count):
            for arm in arms:
                expected = []
                for attempt in range(1, 3 if question == 0 and arm == "baseline" else 2):
                    call = len(rows) // 2 + 1
                    key = {"call": call}
                    if independent:
                        key.update(key=f"q-{question}/{arm}", attempt=attempt)
                    usage = dict(call=call, input=50, cached=10, output=3, estimated_cny=str(PRICE))
                    rows.append({**key, "state": "reserved", "cny": RESERVE})
                    rows.append(
                        {
                            **key,
                            **usage,
                            "state": "confirmed",
                            "id": f"PRIVATE-{directory.name}-{call}",
                        }
                    )
                    expected.append(usage)
                cases.append({"generated": {"calls": len(expected), "receipts": expected}})
        write_lines(directory / "receipts.jsonl", rows)
        if not independent:
            save(directory / "results.json", {"arm": arms[0], "cases": cases, "private": "PRIVATE"})

    ind = dirs[0]
    sample = sorted(({"id": f"q-{i}"} for i in range(100)), key=lambda case: case["id"])
    docs = [
        {"id": f"doc-{i}", "sha256": hashlib.sha256(str(i).encode()).hexdigest()}
        for i in range(100)
    ]
    save(
        tmp_path / "manifest.json",
        {"cases": sample, "documents": docs, "private": "PRIVATE-upload"},
    )
    save(
        ind / "freeze.json",
        {
            "offline": False,
            "source_commit": "a" * 40,
            "registered_questions": 100,
            "cases_sha256": canonical_digest(sample),
            "max_wire_calls": 400,
            "max_cny": "7.3728",
            "model_files_sha256": {
                "embedding": {"pytorch_model.bin": "e" * 64, "PRIVATE-extra-file": "PRIVATE"},
                "reranker": {"model.safetensors": "f" * 64},
            },
            "prompt_sha256": {"baseline": "a" * 64, "candidate": "c" * 64},
            "inputs": {
                d["id"]: {"kb": "PRIVATE-tenant", "source_sha256": d["sha256"]} for d in docs
            },
            "profile_id": "PRIVATE-profile",
        },
    )
    save(
        ind / "execution.json",
        {
            "offline": False,
            "first_http_requests": 200,
            "confirmed_wire_calls": 201,
            "internal_corrections": 1,
            "estimated_cny": str(PRICE * 201),
            "unknowns": 0,
        },
    )
    return dirs


def test_aggregates_all_arms_and_publishes_fingerprints_without_identifiers(run_dirs, tmp_path):
    manifest = tmp_path / "manifest.json"
    report = settle(*run_dirs, manifest=manifest)
    assert report["independent"]["baseline"]["confirmed_calls"] == 101
    assert report["independent"]["candidate"]["confirmed_calls"] == 100
    assert report["wai"]["baseline"]["confirmed_calls"] == 5
    assert report["wai"]["candidate"]["confirmed_calls"] == 14
    assert report["total"] == {
        "confirmed_calls": 220,
        "distinct_provider_receipts": 220,
        "unconfirmed_calls": 0,
        "first_http_requests": 218,
        "corrections": 2,
        "input_tokens": 11_000,
        "cached_input_tokens": 2200,
        "output_tokens": 660,
        "estimated_cny": PRICE * 220,
        "gross_reserved_cny": Decimal(RESERVE) * 220,
    }
    assert (
        report["file_sha256"]["upload_manifest"]
        == hashlib.sha256(manifest.read_bytes()).hexdigest()
    )
    assert "PRIVATE" not in json.dumps(report, default=str)


def test_does_not_label_an_unrelated_upload_manifest_as_the_campaign_input(run_dirs, tmp_path):
    manifest = tmp_path / "manifest.json"
    data = json.loads(manifest.read_bytes())
    data["documents"][0]["sha256"] = "a" * 64
    save(manifest, data)
    with pytest.raises(ValueError, match="differs from the executed sample"):
        settle(*run_dirs, manifest=manifest)


def test_refuses_a_provider_id_reused_across_ledgers(run_dirs):
    ind, wai, _ = run_dirs
    path = wai / "receipts.jsonl"
    rows = read_lines(path)
    rows[1]["id"] = read_lines(ind / "receipts.jsonl")[1]["id"]
    write_lines(path, rows)
    with pytest.raises(ValueError, match="repeated provider receipt"):
        settle(*run_dirs)


@pytest.mark.parametrize("missing", [1, 2])
def test_catches_an_unconfirmed_or_entirely_missing_final_wai_call(run_dirs, missing):
    path = run_dirs[1] / "receipts.jsonl"
    write_lines(path, read_lines(path)[:-missing])
    with pytest.raises(
        ValueError, match="missing reservation or confirmation|different call counts"
    ):
        settle(*run_dirs)


@pytest.mark.parametrize("key,value", [("attempt", 2), ("cached", 51), ("estimated_cny", "NaN")])
def test_refuses_invalid_independent_usage_or_attempt(run_dirs, key, value):
    path = run_dirs[0] / "receipts.jsonl"
    rows = read_lines(path)
    rows[1][key] = value
    write_lines(path, rows)
    with pytest.raises(ValueError):
        settle(*run_dirs)


def test_reconciles_to_the_finished_execution_not_just_a_ledger_total(run_dirs):
    path = run_dirs[0] / "execution.json"
    execution = json.loads(path.read_bytes())
    execution["estimated_cny"] = str(PRICE * 201 - Decimal("0.0000001"))
    save(path, execution)
    with pytest.raises(ValueError, match="completed execution audit"):
        settle(*run_dirs)
