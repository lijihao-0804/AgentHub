"""Run with Python 3.10+ alone, including under -I -S."""

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from uuid import uuid4

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("scifact_public_audit", HERE / "verify.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class PortableAuditTests(unittest.TestCase):
    def test_macro_weights_queries_equally_and_preserves_gains_and_losses(self):
        rows = [
            {"query": "1", "gold": {"7"}, "baseline": ({"7"},), "new": (set(),)},
            {"query": "2", "gold": {"8", "9"}, "baseline": (set(),), "new": ({"8"},)},
        ]
        counts, macro, gains, losses = audit.compare(rows, "new", 0)
        self.assertEqual(macro, (0.5, 0.25, -0.25))
        self.assertEqual((counts["gained"], counts["lost"]), (1, 1))
        self.assertEqual((gains, losses), ([("2", "8")], [("1", "7")]))

    def test_official_evidence_requires_both_annotation_and_returned_source(self):
        row = {"cohort": "development", "query": "1", "gold": ["7"], "annotated": []}
        row.update({arm: [["7"], [], ["7"], []] for arm in audit.ARMS})
        with self.assertRaisesRegex(ValueError, "unannotated"):
            audit.validate(row)

    def test_fresh_directory_needs_only_public_ledger_and_frozen_reports(self):
        files = (
            "cohort-freeze.json",
            "development.json",
            "confirmation.json",
            "rerank-report.json",
            "rerank-protocol.json",
            "rationale-visibility.json",
            "protocol.json",
            "protected-followup.json",
        )
        clean = Path.cwd() / (".scifact-portable-test-" + uuid4().hex)
        clean.mkdir()
        try:
            portable = clean / "portable"
            portable.mkdir()
            for filename in files:
                shutil.copyfile(HERE.parent / filename, clean / filename)
            for filename in ("verify.py", "hits.jsonl", "provenance.json"):
                shutil.copyfile(HERE / filename, portable / filename)
            run = subprocess.run(
                [sys.executable, "-I", "-S", str(portable / "verify.py"), "--json"],
                cwd=clean,
                capture_output=True,
                text=True,
                check=True,
            )
            result = json.loads(run.stdout)
            protected = result["confirmation"]["bge_protected_refill4"]["pool_rationales"]
            self.assertEqual(protected["losses"], [["178", "23557241"]])
            rows = (portable / "hits.jsonl").read_bytes().splitlines()
            rows[1] = rows[0]
            edited = b"\n".join(rows) + b"\n"
            (portable / "hits.jsonl").write_bytes(edited)
            with self.assertRaisesRegex(ValueError, "ledger SHA-256"):
                audit.verify(portable)
            meta = audit.read(portable / "provenance.json")
            meta["canonical_hits_sha256"] = hashlib.sha256(edited).hexdigest()
            (portable / "provenance.json").write_text(json.dumps(meta), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate or missing query"):
                audit.verify(portable)
        finally:
            shutil.rmtree(clean)


if __name__ == "__main__":
    unittest.main()
