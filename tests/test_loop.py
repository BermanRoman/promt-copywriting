import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest


spec = importlib.util.spec_from_file_location("loop", Path(__file__).resolve().parents[1] / "scripts/loop.py")
loop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loop)


class LoopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.md"
        self.source.write_text("First draft.\n", encoding="utf-8")
        self.task = self.root / "task"

    def cli(self, *args, ok=True):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = loop.main([str(a) for a in args])
        self.assertEqual(code, 0 if ok else 2, err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        return json.loads(out.getvalue()) if code == 0 else err.getvalue()

    def init(self, profile="written", limit=3):
        return self.cli("init", self.task, "--source", self.source,
                        "--profile", profile, "--max-rounds", limit)

    def review(self, gate, verdict="pass", reviewer=None, ok=True, **changes):
        state = self.cli("status", self.task)
        value = {"gate": gate, "text_hash": state["text_hash"],
                 "reviewer": reviewer or gate + "-person", "verdict": verdict,
                 "explanation": "No externally verifiable claims." if gate == "facts" else "Checked.",
                 "blockers": []}
        value.update(changes)
        path = self.root / "review.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return self.cli("review", self.task, path, ok=ok)

    def change(self, value):
        (self.task / "text.md").write_text(value, encoding="utf-8")

    def test_source_existing_and_restart(self):
        self.init("simple")
        self.change("Changed")
        self.cli("init", self.task, "--source", self.source, "--profile", "simple", ok=False)
        self.assertEqual(self.source.read_text(), "First draft.\n")
        self.assertEqual((self.task / "text.md").read_text(), "Changed")
        self.cli("advance", self.task)
        self.review("editorial")
        self.assertEqual(self.review("facts")["status"], "ready")
        self.assertEqual(self.cli("status", self.task)["status"], "ready")

    def test_stale_review_and_readiness(self):
        self.init("simple")
        old = self.cli("status", self.task)["text_hash"]
        self.review("editorial")
        self.review("facts")
        self.change("Second")
        self.assertNotEqual(self.cli("status", self.task)["status"], "ready")
        self.review("facts", ok=False)
        self.cli("snapshot", self.task)
        self.assertEqual(self.cli("status", self.task)["gates"]["facts"], "pending")
        self.review("facts", text_hash=old, ok=False)
        self.assertTrue((self.task / "snapshots/001.md").exists())

    def test_reader_gates_identity_packet(self):
        self.init()
        self.review("reader", ok=False)
        self.review("editorial", reviewer="Editor")
        self.review("facts")
        self.review("reader", reviewer=" editor ", ok=False)
        packet = self.root / "packet.md"
        self.cli("reader-packet", self.task, "--output", packet)
        self.assertEqual(packet.read_bytes(), (self.task / "text.md").read_bytes())
        self.assertIn("text_hash", json.loads(Path(str(packet) + ".json").read_text()))
        self.cli("reader-packet", self.task, "--output", packet, ok=False)
        self.assertEqual(self.review("reader")["status"], "ready")
        self.review("editorial", reviewer="reader-person")
        self.assertNotEqual(self.cli("status", self.task)["status"], "ready")

    def test_video_gusar_edit_restarts_checks(self):
        self.init("video")
        self.review("gusar", ok=False)
        self.review("facts")
        self.review("editorial")
        self.review("reader", ok=False)
        self.review("gusar")
        self.review("reader", reviewer="gusar-person", ok=False)
        self.change("Gusar edit")
        self.cli("advance", self.task)
        self.review("gusar", ok=False)
        self.review("facts")
        self.review("gusar")
        self.review("reader", ok=False)
        self.review("editorial")
        self.assertEqual(self.review("reader")["status"], "ready")

    def test_round_limit_extension_and_stop(self):
        self.init("simple", limit=2)
        self.change("Second")
        self.cli("advance", self.task)
        self.review("editorial", verdict="revise")
        self.assertEqual(self.cli("status", self.task)["status"], "needs_author")
        self.change("Third")
        self.cli("advance", self.task, ok=False)
        self.cli("extend", self.task, "--max-rounds", 3, "--reason", "", ok=False)
        self.cli("extend", self.task, "--max-rounds", 3, "--reason", "Author requested another version")
        self.cli("advance", self.task)
        self.assertEqual(len(list((self.task / "events").glob("*.json"))), 1)
        self.cli("stop", self.task, "--reason", "Needs author input")
        self.assertEqual(self.cli("status", self.task)["status"], "needs_author")
        self.review("editorial", ok=False)

    def test_review_schema_and_unresolved_verdict(self):
        self.init("simple")
        self.review("facts", blockers=["Unverified"], ok=False)
        self.review("facts", blockers="bad", ok=False)
        self.review("facts", verdict="maybe", ok=False)
        self.review("facts", surprise=True, ok=False)
        self.review("facts", explanation=" ", ok=False)
        self.review("reader", ok=False)
        self.review("facts")
        self.review("editorial", verdict="blocked", blockers=["Author needed"])
        self.assertNotEqual(self.cli("status", self.task)["status"], "ready")
        self.assertEqual(self.review("editorial")["status"], "ready")

    def test_bad_json_and_missing_path(self):
        self.init("audit")
        bad = self.root / "bad.json"
        bad.write_text("{oops")
        self.cli("review", self.task, bad, ok=False)
        self.cli("status", self.root / "missing", ok=False)
        self.assertEqual(self.review("editorial")["status"], "ready")


if __name__ == "__main__":
    unittest.main()
