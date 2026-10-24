import json

from benchmarks.public_rag.paper_qa_visibility import extract_visible_records


def _message(content: str, role: str = "tool") -> dict:
    return {"role": role, "content": content}


def _preview_message(preview: str) -> dict:
    return _message(
        json.dumps(
            {
                "data": {"preview": preview, "truncated": True},
                "status": "SUCCESS",
                "truncated": True,
                "trust": "UNTRUSTED",
            }
        )
    )


def test_reference_only_records_are_not_visible_evidence() -> None:
    content = json.dumps(
        {
            "data": {
                "evidence_refs": [{"chunk_id": "ref-only"}],
                "results": [
                    {"chunk_id": "empty", "snippet": ""},
                    {"chunk_id": "missing"},
                    {"snippet": "no id"},
                ],
            }
        }
    )

    assert extract_visible_records([_message(content)]) == []


def test_complete_and_cut_records_decode_completed_snippet_escapes() -> None:
    preview = (
        '{"evidence_refs":[{"chunk_id":"ref-only"}],"results":['
        '{"chunk_id":"complete","snippet":"first \\"quoted\\" \\\\ \\u2603","score":0.5},'
        '{"chunk_id":"partial","snippet":"quote: \\" slash: \\\\ snow: \\u2603 '
        'cut: \\u26'
    )

    assert extract_visible_records([_preview_message(preview)]) == [
        {"chunk_id": "complete", "snippet": 'first "quoted" \\ ☃', "score": 0.5},
        {"chunk_id": "partial", "snippet": 'quote: " slash: \\ snow: ☃ cut: '},
    ]


def test_cut_inside_escape_never_invents_the_missing_character() -> None:
    preview = (
        '{"results":[{"chunk_id":"escaped","snippet":"visible prefix\\'
        'nsecond line\\u00'
    )

    result = extract_visible_records([_preview_message(preview)])

    assert result == [{"chunk_id": "escaped", "snippet": "visible prefix\nsecond line"}]
    assert result[0]["snippet"] != "visible prefix\nsecond line\x00"


def test_truncated_snippet_keeps_only_the_actual_visible_suffix() -> None:
    preview = '{"results":[{"chunk_id":"partial","snippet":"The retained ending'

    assert extract_visible_records([_preview_message(preview)]) == [
        {"chunk_id": "partial", "snippet": "The retained ending"}
    ]


def test_normal_structured_results_are_extracted_from_tool_messages() -> None:
    content = json.dumps(
        {
            "data": {
                "results": [
                    {
                        "chunk_id": "direct",
                        "snippet": "Normal visible text.",
                        "document_name": "paper.txt",
                    }
                ],
                "evidence_refs": [{"chunk_id": "ref-only"}],
            }
        }
    )
    messages = [
        _message(json.dumps({"data": {"results": [{"chunk_id": "x", "snippet": "x"}]}}), "user"),
        _message(content),
    ]

    assert extract_visible_records(messages) == [
        {
            "chunk_id": "direct",
            "snippet": "Normal visible text.",
            "document_name": "paper.txt",
        }
    ]


def test_truncation_before_results_produces_no_records() -> None:
    preview = '{"evidence_refs":[{"chunk_id":"ref-only"}],"resu'

    assert extract_visible_records([_preview_message(preview)]) == []
