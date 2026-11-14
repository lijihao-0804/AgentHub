"""Focused regression checks for first-response reuse and first-dispatch fencing."""

import json
import tempfile
import unittest
from pathlib import Path

from benchmarks.public_rag.article_live_capture import capture, exclusive


class FirstResponseTests(unittest.TestCase):
    def test_matching_complete_capture_only(self):
        class Response:
            is_success = False
            status_code = 502

            def json(self):
                return {"error": {"code": "INVALID_OUTPUT"}}

        class Client:
            calls = 0

            def post(self, *args, **kwargs):
                self.calls += 1
                return Response()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "results.json"
            protocol = path.with_name("protocol.json")
            protocol.write_text("v1")
            ds = {
                "workspace": "ws",
                "kb": "kb",
                "snapshot": "snapshot",
                "snapshot_sha256": "hash",
                "count": 1,
            }
            cases = [{"id": "a", "query": "question"}]
            mapping = {"c": {"source_id": "doc", "revision": "rev", "ordinal": 0, "text": "source"}}
            client = Client()
            args = [client, ds, "profile", "model", "baseline", cases, mapping, path, protocol]
            capture(*args)
            capture(*args)
            for ix, value in [
                (1, {**ds, "snapshot_sha256": "new"}),
                (2, "new-profile"),
                (3, "different-model"),
                (4, "other-arm"),
                (5, [{"id": "a", "query": "new question"}]),
                (6, {"c": {**mapping["c"], "revision": "other"}}),
            ]:
                changed = args.copy()
                changed[ix] = value
                with self.assertRaises(ValueError):
                    capture(*changed)
            saved = json.loads(path.read_bytes())
            for field, modified in (("http", "not a status"), ("id", "unknown")):
                bad = json.loads(path.read_bytes())
                bad["rows"][0][field] = modified
                path.write_text(json.dumps(bad))
                with self.assertRaises(ValueError):
                    capture(*args)
                path.write_text(json.dumps(saved))
            protocol.write_text("changed policy")
            with self.assertRaises(ValueError):
                capture(*args)
            protocol.write_text("v1")
            bad = json.loads(path.read_bytes())
            bad["rows"][0]["citations"] = [
                {
                    "chunk_id": "c",
                    "revision": "fake",
                    "source": "doc",
                    "ordinal": 0,
                    "excerpt": "source",
                }
            ]
            path.write_text(json.dumps(bad))
            with self.assertRaises(ValueError):
                capture(*args)
            path.write_text(json.dumps(saved))
            with exclusive(path), self.assertRaises(FileExistsError):
                capture(*args)
            receipt = json.loads(path.read_bytes())
            receipt["rows"][0]["state"] = "pending"
            path.write_text(json.dumps(receipt))
            with self.assertRaises(RuntimeError):
                capture(*args)
            self.assertEqual(client.calls, 1)


if __name__ == "__main__":
    unittest.main()
