"""S3 / R2-N1: committed samples are auditable and verifiable offline."""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
V3 = os.path.abspath(os.path.join(HERE, ".."))
for _p in (ROOT, V3):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from benchmarks.v3.training import cli, minicpm  # noqa: E402
from benchmarks.v3.training.messages import to_native_messages  # noqa: E402


class SampleAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="mail-sft-samples-")
        cls.sample_dir = os.path.join(cls.tmp, "samples")
        cls.report = cli.build_slice(cls.tmp, seed=7,
                                     sample_dir=cls.sample_dir)
        cls.files = sorted(os.path.join(cls.sample_dir, n)
                           for n in os.listdir(cls.sample_dir))

    def test_render_matches_committed_native_render(self):
        for path in self.files:
            sample = json.load(open(path, encoding="utf-8"))
            ex = sample["example"]
            rendered = minicpm.render_messages(
                to_native_messages(ex["messages"]),
                tools=ex.get("tools") or None)
            self.assertEqual(rendered, sample["native_render"], path)

    def test_verification_context_matches_source_id(self):
        for path in self.files:
            sample = json.load(open(path, encoding="utf-8"))
            ctx = sample["verification_context"]
            self.assertEqual(ctx["source_id"], sample["example"]["source_id"])
            self.assertIsNotNone(ctx["source"])

    def test_decision_has_no_invented_think(self):
        for path in self.files:
            sample = json.load(open(path, encoding="utf-8"))
            ex = sample["example"]
            if ex["task"] != "decision":
                continue
            for m in ex["messages"]:
                if m["role"] == "assistant":
                    self.assertFalse(m.get("think"))

    def test_cli_verify_succeeds_on_committed_sample(self):
        for path in self.files:
            rc = cli.main(["verify", path])
            self.assertEqual(rc, 0, path)

    def test_cli_verify_missing_context_is_structured(self):
        # a bare decision example with no sidecar and no --sources
        bare = {"task": "decision", "schema_version": "mail-sft-0.1",
                "example_id": "x", "source_id": "nope",
                "messages": [], "identities": {}}
        path = os.path.join(self.tmp, "bare.json")
        with open(path, "w") as f:
            json.dump(bare, f)
        rc = cli.main(["verify", path])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
