"""Run interleaved 512/2048 first HTTP responses using the two frozen budget profiles."""

from pathlib import Path

from benchmarks.public_rag.article_live_capture import write
from benchmarks.public_rag.qa_first_response_cases import read
from benchmarks.public_rag.qa_first_response_live import capture


def capture_budget_pair(
    client, *, case, profiles, model, dataset, mapping, protocol, trace_file, directory, source
):
    """Persist each dispatch separately, including unknown outcomes after transport failure."""
    first = case["first_arm"]
    other = "2048" if first == "512" else "512"
    for label in (first, other):
        destination = directory / f"{label}-{case['id']}.json"
        capture(
            client,
            state={"profile": profiles[label], "model": model},
            dataset=dataset,
            cases=[case],
            mapping=mapping,
            arm=label,
            protocol=protocol,
            trace_file=trace_file,
            destination=destination,
            source=source,
        )


def assemble(directory: Path, output: Path, protocol: Path, scopes=None, arms=("512", "2048")):
    cases = read(protocol)["cases"]
    for scope in scopes or dict.fromkeys(c["scope"] for c in cases):
        for arm in arms:
            selected = [c for c in cases if c["scope"] == scope]
            reports = [read(directory / f"{arm}-{c['id']}.json") for c in selected]
            common = {k: v for k, v in reports[0].items() if k != "rows"}
            if any({k: v for k, v in r.items() if k != "rows"} != common for r in reports):
                raise ValueError("Budget cohort identity drift")
            rows = [r["rows"][0] for r in reports]
            if any(row["state"] != "returned" for row in rows):
                raise ValueError("Ambiguous budget dispatch")
            write(output / f"{arm}-{scope}.json", dict(**common, rows=rows))
