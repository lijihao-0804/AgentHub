"""Offline paired source-span checks. Run python -m benchmarks.retrieval.chunking_probe."""

from __future__ import annotations

import hashlib
import json
import textwrap
from dataclasses import dataclass
from io import BytesIO
from statistics import median
from uuid import NAMESPACE_URL, uuid5

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from packages.core.config.settings import Settings
from packages.knowledge.chunking import normalize_chunk_text
from packages.knowledge.chunking_policy import ChunkingPolicy, ChunkingStrategy
from packages.knowledge.parser import _parse_content


@dataclass(frozen=True)
class Fixture:
    name: str
    mime: str
    source: bytes
    evidence: list[str]
    cross_paragraph: list[str]


def _rule(i: int) -> str:
    return (
        f"Clause {i:02d} applies to depot D-{i:02d}. The supervisor records the dispatch "
        "date, the original ticket reference and the applicable charge before settlement. "
        "The charge is payable after the inspection report and signed delivery receipt "
        f"have been reconciled. The maximum adjustment for depot D-{i:02d} is EUR {37 * i}."
    )


def _pdf(pages: list[str]) -> bytes:
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    for paragraph in pages:
        page = writer.add_blank_page(width=620, height=800)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        stream = DecodedStreamObject()
        lines = " T* ".join(f"({line}) Tj" for line in textwrap.wrap(paragraph, width=95))
        stream.set_data(f"BT /F1 9 Tf 13 TL 40 740 Td {lines} ET".encode("ascii"))
        page.replace_contents(stream)
    data = BytesIO()
    writer.write(data)
    return data.getvalue()


def _fixtures() -> list[Fixture]:
    rules = [_rule(i) for i in range(1, 16)]
    exception = [
        "New shipments require prior clearance.",
        "For existing shipments, the previous clearance remains valid for seven days.",
    ]
    pair = "\n\n".join(exception)
    plain = Fixture(
        "depot-policy.txt",
        "text/plain",
        "\n\n".join([pair, *rules]).encode(),
        rules + exception,
        [pair],
    )
    rows = [f"| D-{i:02d} | {rule} | {37 * i} EUR |" for i, rule in enumerate(rules, start=1)]
    markdown = "# Adjustment schedule\n\n| Depot | Terms | Fee |\n|---|---|---|\n"
    markdown += "\n".join(rows) + "\n\n## Clearance\n" + pair
    sheet = "Depot,Quarter,Cap\n" + "\n".join(f"D-{i:02d},Q2,EUR {37 * i}" for i in range(1, 9))
    layout = [
        {"type": "text", "text": "Depot limits", "text_level": 1, "page_idx": 0},
        {"type": "text", "text": "\n\n".join(rules[:5]), "page_idx": 0},
        {"type": "text", "text": "\n\n".join(rules[5:10]), "page_idx": 1},
    ]
    return [
        plain,
        Fixture("adjustments.md", "text/markdown", markdown.encode(), rows + exception, [pair]),
        Fixture(
            "quarterly.csv", "text/csv", sheet.encode(), [f"EUR {37 * i}" for i in range(1, 9)], []
        ),
        Fixture(
            "policy.pdf",
            "application/pdf",
            _pdf([" ".join(rules[:6]), " ".join(rules[6:12])]),
            rules[:12],
            [],
        ),
        Fixture(
            "policy_content_list.json",
            "application/json",
            json.dumps(layout, sort_keys=True).encode(),
            rules[:10],
            [],
        ),
    ]


def _retained(targets: list[str], excerpts: list[str]) -> int:
    # Physical PDF line wraps and paragraph whitespace are equivalent for this
    # text containment probe. Every matched phrase must still fit in one chunk.
    return sum(
        any(" ".join(target.split()) in excerpt for excerpt in excerpts) for target in targets
    )


def run() -> dict:
    settings = Settings(knowledge_chunk_size_chars=1200, knowledge_chunk_overlap_chars=200)
    report = {}
    for fixture in _fixtures():
        parsed = _parse_content(
            fixture.source, fixture.mime, max_pdf_pages=10, max_parsed_chars=50_000
        )
        # Fingerprint parsed inputs and gold spans. PDF envelope metadata may
        # vary with the writer version while the inputs to the splitters agree.
        packet = {
            "blocks": [(b.text, b.locator) for b in parsed.blocks],
            "facts": fixture.evidence,
            "cross_paragraph": fixture.cross_paragraph,
        }
        fingerprint = hashlib.sha256(
            json.dumps(packet, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        revision = uuid5(NAMESPACE_URL, f"https://agenthub.invalid/chunking-probe/{fixture.name}")
        scoped = {
            tuple(b.locator.get(k) for k in ("page", "block_index", "row", "part_index")): b
            for b in parsed.blocks
        }
        item = {
            "fixture_sha256": fingerprint,
            "evidence_total": len(fixture.evidence),
            "cross_paragraph_total": len(fixture.cross_paragraph),
            "strategies": {},
        }
        for strategy in ChunkingStrategy:
            policy = ChunkingPolicy.resolve(strategy, fixture.mime, settings)
            chunks = policy.build(revision, parsed, media_type=fixture.mime)
            lengths = [len(c.text) for c in chunks]
            exact = 0
            for c in chunks:
                loc = c.locator
                block = scoped[
                    tuple(loc.get(k) for k in ("page", "block_index", "row", "part_index"))
                ]
                if "char_start" in loc and "char_end" in loc:
                    base = block.locator.get("char_start", 0)
                    excerpt = block.text[loc["char_start"] - base : loc["char_end"] - base]
                    exact += c.text == normalize_chunk_text(excerpt)
            excerpts = [" ".join(c.text.split()) for c in chunks]
            item["strategies"][strategy.value] = {
                "cap_chars": policy.size_chars,
                "overlap_chars": policy.overlap_chars,
                "chunks": len(chunks),
                "length_min_median_max": [min(lengths), median(lengths), max(lengths)],
                "indexed_chars": sum(lengths),
                "exact_source_spans": exact,
                "single_unit_evidence_retained": _retained(fixture.evidence, excerpts),
                "cross_paragraph_evidence_retained": _retained(fixture.cross_paragraph, excerpts),
            }
        report[fixture.name] = item
    return report


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, ensure_ascii=False))
