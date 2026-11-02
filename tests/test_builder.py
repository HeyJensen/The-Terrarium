import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from shared.logger import DecisionLog

from agents.builder import run as builder
from agents.common import bot
from agents.common.approvals import ApprovalQueue

REPO = Path(builder.__file__).resolve().parents[2]


class Builder(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.queue = ApprovalQueue(self.tmp / "approvals")
        self.cfg = bot.load_config("builder", settings={})
        self.log = DecisionLog("builder", log_dir=self.tmp / "logs")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def fake_repo(self):
        root = self.tmp / "repo"
        (root / "agents" / "good").mkdir(parents=True)
        (root / "agents" / "bad").mkdir()
        (root / "agents" / "common").mkdir()
        (root / "tests").mkdir()
        for f in builder.REQUIRED_FILES:
            (root / "agents" / "good" / f).write_text("")
        (root / "agents" / "bad" / "run.py").write_text("x = 1  # TODO handle errors\n")
        (root / "agents" / "manifest.json").write_text(json.dumps({"agents": [{"name": "good"}, {"name": "ghost"}]}))
        (root / "tests" / "test_x.py").write_text("from agents.good import run\n")
        return root

    def test_audit_flags_house_rule_breaks(self):
        root = self.fake_repo()
        old = self.queue.submit("studio", "listing", "old", {})
        old["created_at"] = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(timespec="seconds")
        self.queue._write(old)
        found = builder.audit(root, self.queue, self.cfg)
        text = [(f["severity"], f["bot"], f["finding"]) for f in found]
        self.assertIn(("high", "bad", "missing prompt.md"), text)
        self.assertIn(("high", "bad", "not in agents/manifest.json"), text)
        self.assertIn(("high", "ghost", "in the manifest but has no folder"), text)
        self.assertIn(("medium", "bad", "no test imports it"), text)
        self.assertIn(("medium", "studio", "1 drafts waiting over 3 days for approval"), text)
        self.assertTrue(any(s == "low" and "TODO" in f for s, _, f in text))
        self.assertFalse(any(b == "good" for _, b, _ in text))
        self.assertEqual(found[0]["severity"], "high")
        self.assertIn("## High", builder.audit_markdown(found))

    def test_real_repo_has_no_high_findings(self):
        found = builder.audit(REPO, self.queue, self.cfg)
        self.assertEqual([f for f in found if f["severity"] == "high"], [])

    def test_ideas_ranked_deduped_and_flagged(self):
        ideas = builder.load_ideas()
        items = builder.propose_ideas(self.cfg, ideas, ["social", "pinterest"], self.queue, self.log)
        self.assertEqual(len(items), 3)
        names = [i["draft"]["name"] for i in items]
        self.assertNotIn("pinterest", names)  # already a bot
        self.assertNotIn("signals", names)  # blocked
        scores = [i["draft"]["score"] for i in items]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertTrue(all(i["kind"] == "bot_idea" and i["status"] == "pending" for i in items))
        self.assertTrue(any("account" in f for i in items for f in i["flags"]))
        again = builder.propose_ideas(self.cfg, ideas, ["social", "pinterest"], self.queue, self.log)
        self.assertFalse({i["draft"]["id"] for i in again} & set(i["draft"]["id"] for i in items))

    def test_blocked_idea_never_proposed(self):
        ideas = builder.load_ideas()
        cfg = {**self.cfg, "ideas_per_run": 99, "min_idea_score": -99}
        items = builder.propose_ideas(cfg, ideas, [], self.queue, self.log)
        self.assertNotIn("signals", [i["draft"]["name"] for i in items])

    def test_inbox_ideas_join_catalog(self):
        row = {"id": "x1", "name": "xbot", "title": "X", "pitch": "p", "potential": "5", "speed": "5",
               "reuse": "5", "cost": "1", "risk": "1"}
        ideas = builder.load_ideas(inbox_rows=[row, {"id": "broken"}])
        self.assertEqual(builder.score([i for i in ideas if i["id"] == "x1"][0]), 13)
        self.assertNotIn("broken", [i["id"] for i in ideas])

    def test_scaffold_only_for_approved_ideas(self):
        root = self.fake_repo()
        idea = builder.load_ideas()[0]
        item = self.queue.submit("builder", "bot_idea", "t", {**idea, "score": 9})
        with self.assertRaises(ValueError):
            builder.scaffold(item, root)
        item = self.queue.decide(item["id"], "approved")
        d = builder.scaffold(item, root)
        for f in builder.REQUIRED_FILES:
            self.assertTrue((d / f).exists(), f)
        cfg = json.loads((d / "agent.json").read_text())["config_schema"]
        self.assertEqual(cfg["publishing_mode"]["default"], "draft_only")
        entry = json.loads((root / "agents" / "manifest.json").read_text())["agents"][-1]
        self.assertEqual((entry["name"], entry["enabled"]), (idea["name"], False))
        compile((d / "run.py").read_text(), "run.py", "exec")
        with self.assertRaises(FileExistsError):
            builder.scaffold(item, root)


if __name__ == "__main__":
    unittest.main()
