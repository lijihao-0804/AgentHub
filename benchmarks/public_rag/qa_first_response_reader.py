"""Send first responses in frozen per-case order using separate original/candidate API processes."""

import hashlib
from contextlib import ExitStack

from benchmarks.public_rag.qa_first_response_budget import assemble
from benchmarks.public_rag.qa_first_response_cases import read
from benchmarks.public_rag.qa_first_response_live import capture
from benchmarks.public_rag.qa_first_response_replay import SOURCE_FILES, server
from scripts.support_api_smoke import authenticate


def capture_reader_scope(
    *,
    directory,
    public,
    protocol,
    scope,
    sources,
    collection,
    owner,
    dataset,
    mapping,
    profiles,
    model,
):
    cases = [c for c in read(protocol)["cases"] if c["scope"] == scope]
    with ExitStack() as stack:
        clients = {}
        for variant, source in sources.items():
            client, trace = stack.enter_context(
                server(source, directory, f"{scope}-{variant}", collection)
            )
            hashes = {
                f: hashlib.sha256((source / f).read_bytes()).hexdigest() for f in SOURCE_FILES
            }
            clients[variant] = client, trace, hashes
        for case in cases:
            for arm in case["arm_order"]:
                variant = "strict" if arm == "C" else "original"
                profile = profiles["512" if arm == "A" else "2048"]
                client, trace, hashes = clients[variant]
                authenticate(client, owner)
                capture(
                    client,
                    state=dict(model=model, profile=profile),
                    dataset=dataset,
                    cases=[case],
                    mapping=mapping,
                    arm=arm,
                    protocol=protocol,
                    trace_file=trace,
                    destination=directory / f"{arm}-{case['id']}.json",
                    source=hashes,
                )
    assemble(directory, public, protocol, scopes=[scope], arms=("A", "B", "C"))
