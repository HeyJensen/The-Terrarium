import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from shared.logger import DecisionLog

from agents.common import bot
from agents.common.approvals import ApprovalQueue
from agents.common.board import Board
from agents.common.brain import ClaudeBrain, TemplateBrain, get_brain
from agents.content import run as content
from agents.etsy import run as etsy
from agents.social import run as social
from agents.studio import run as studio

SAMPLES = Path(bot.__file__).with_name("samples")


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.board = Board(self.tmp / "board")
        self.queue = ApprovalQueue(self.tmp / "approvals")
        self.cfg = {a: bot.load_config(a, settings={}) for a in ("social", "etsy", "studio", "content")}
        self.brain = TemplateBrain()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def log(self, agent):
        return DecisionLog(agent, log_dir=self.tmp / "logs")

    def inbox(self, sample):
        d = self.tmp / "inbox" / sample
        d.mkdir(parents=True)
        shutil.copy(SAMPLES / sample, d / sample)
        return bot.read_inbox("x", inbox_dir=d)

    def run_research(self):
        social.scan(self.cfg["social"], self.board, self.log("social"), self.inbox("trends.csv"))
        return etsy.scan(self.cfg["etsy"], self.board, self.log("etsy"), self.inbox("keywords_export.csv"),
                         state_dir=self.tmp / "etsy")

    def test_inbox_normalises_headers_and_moves_files(self):
        rows = self.inbox("keywords_export.csv")
        self.assertEqual(rows[0]["avg_searches"], "4200")
        self.assertEqual(rows[0]["etsy_competition"], "18,450")
        self.assertEqual(bot.num("$24.10"), 24.10)
        self.assertEqual(bot.num("18,450"), 18450)
        self.assertEqual(list((self.tmp / "inbox" / "keywords_export.csv").glob("*.csv")), [])

    def test_social_scan_filters_low_scores_and_dedupes(self):
        posted = social.scan(self.cfg["social"], self.board, self.log("social"), self.inbox("trends.csv"))
        topics = [p["trend"] for p in posted]
        self.assertNotIn("gardening", topics)
        self.assertEqual(topics[0], "taylor swift")  # highest score first
        again = social.scan(self.cfg["social"], self.board, self.log("social"),
                            [{"topic": "Pickleball", "score": "90"}])
        self.assertEqual(again, [])

    def test_social_dedupe_expires(self):
        social.scan(self.cfg["social"], self.board, self.log("social"), [{"topic": "x", "score": "90"}])
        later = datetime.now(timezone.utc) + timedelta(days=8)
        self.assertEqual(len(social.scan(self.cfg["social"], self.board, self.log("social"),
                                         [{"topic": "x", "score": "90"}], now=later)), 1)

    def test_etsy_scores_filters_and_boosts_trends(self):
        niches = self.run_research()
        names = [n["niche"] for n in niches]
        self.assertNotIn("dog mom shirt", names)  # 142k competing listings
        self.assertIn("sourdough starter guide", names)
        pickle = next(n for n in niches if n["niche"] == "pickleball shirt")
        self.assertIsNotNone(pickle["trend_id"])
        self.assertEqual(pickle["score"], round(etsy.opportunity(4200, 18450) * 1.25, 1))
        todo = (self.tmp / "etsy" / "keywords_to_check.txt").read_text().split()
        self.assertIn("mom", todo)  # "dog mom" trend had no usable keyword row
        self.assertEqual(self.board.unread("trends", "etsy"), [])

    def test_studio_drafts_follow_etsy_limits_and_disclose(self):
        self.run_research()
        items = studio.draft(self.cfg["studio"], self.board, self.queue, self.log("studio"), self.brain)
        self.assertTrue(items)
        for i in items:
            lst = i["draft"]["listing"]
            self.assertEqual(i["status"], "pending")
            self.assertLessEqual(len(lst["title"]), 140)
            self.assertLessEqual(len(lst["tags"]), 13)
            self.assertTrue(all(len(t) <= 20 for t in lst["tags"]))
            if i["draft"]["product_type"] == "tshirt":
                self.assertIn("production partner", lst["description"])
                self.assertIn("AI", lst["description"])
                self.assertGreaterEqual(lst["price_usd"], 22.0)
        self.assertEqual(len({i["draft"]["design_key"] for i in items}), len(items))  # no repeats across niches
        self.assertEqual(studio.draft(self.cfg["studio"], self.board, self.queue, self.log("studio"), self.brain), [])

    def test_studio_reads_product_type_from_keyword(self):
        cfg = self.cfg["studio"]
        self.assertEqual(studio.subject("funny sourdough shirt"), "sourdough")
        self.assertEqual(studio.product_types_for(cfg, "pickleball shirt"), ["tshirt"])
        self.assertEqual(studio.product_types_for(cfg, "sourdough starter guide"), ["printable"])
        self.assertEqual(studio.product_types_for(cfg, "pickleball"), ["tshirt", "printable"])
        guide = studio.printable_drafts(cfg, {"niche": "sourdough starter guide"}, self.brain)
        self.assertEqual(guide[0]["document"]["pages"], studio.PRINTABLES["guide"])

    def test_trademark_terms_are_flagged(self):
        self.assertEqual(bot.ip_flags("Funny Pickleball Shirt"), [])
        self.assertIn("possible trademark: taylor swift", bot.ip_flags("Taylor Swift Eras shirt"))
        self.assertEqual(bot.ip_flags("Fordham shirt"), [])  # whole words only
        self.board.post("niches", "etsy", {"niche": "disney mom shirt", "avg_price": 25})
        items = studio.draft(self.cfg["studio"], self.board, self.queue, self.log("studio"), self.brain)
        self.assertTrue(all(i["flags"] for i in items))

    def test_content_drafts_have_disclosure_first_and_no_duplicates(self):
        self.run_research()
        items = content.draft(self.cfg["content"], self.board, self.queue, self.log("content"), self.brain)
        self.assertEqual(len(items), self.cfg["content"]["posts_per_run"])
        md = items[0]["draft"]["markdown"].splitlines()
        self.assertEqual(md[2], content.DISCLOSURE)
        self.assertIn("[[amazon:", items[0]["draft"]["markdown"])
        self.board.post("niches", "etsy", {"niche": items[0]["draft"]["keywords"][0]})
        self.assertEqual(content.draft(self.cfg["content"], self.board, self.queue, self.log("content"), self.brain), [])

    def test_social_promotes_only_approved_items_once(self):
        self.run_research()
        items = studio.draft(self.cfg["studio"], self.board, self.queue, self.log("studio"), self.brain)
        self.queue.decide(items[0]["id"], "approved")
        self.queue.decide(items[1]["id"], "rejected")
        drafts = social.promote(self.cfg["social"], self.queue, self.log("social"), self.brain, state_dir=self.tmp / "s")
        self.assertEqual(len(drafts), len(self.cfg["social"]["platforms"]))
        self.assertTrue(all(d["draft"]["promotes"] == items[0]["id"] for d in drafts))
        x = next(d for d in drafts if d["draft"]["platform"] == "x")
        self.assertLessEqual(len(x["draft"]["text"]), 280)
        self.assertEqual(social.promote(self.cfg["social"], self.queue, self.log("social"), self.brain,
                                        state_dir=self.tmp / "s"), [])

    def test_draft_only_lock(self):
        for agent in ("social", "etsy", "studio", "content"):
            self.assertIsNone(bot.publishing_problem(self.cfg[agent]))
            self.assertIn("not allowed", bot.publishing_problem({**self.cfg[agent], "publishing_mode": "live"}))
        self.assertEqual(bot.load_config("studio", settings={"studio": {"designs_per_niche": 1}})["designs_per_niche"], 1)

    def test_claude_brain_needs_approval(self):
        self.assertIsInstance(get_brain("template"), TemplateBrain)
        with self.assertRaises(RuntimeError):
            ClaudeBrain()

    def test_approval_decisions(self):
        item = self.queue.submit("studio", "etsy_listing", "t", {})
        with self.assertRaises(ValueError):
            self.queue.decide(item["id"], "pending")
        self.queue.decide(item["id"], "approved", note="ok")
        self.assertEqual(self.queue.list(status="approved")[0]["note"], "ok")

    def test_every_agent_registered_with_contract_files(self):
        root = Path(bot.__file__).resolve().parents[2]
        manifest = json.loads((root / "agents" / "manifest.json").read_text())
        names = {a["name"] for a in manifest["agents"]}
        for agent in ("social", "etsy", "studio", "content"):
            self.assertIn(agent, names)
            for f in ("agent.json", "prompt.md", "run.py"):
                self.assertTrue((root / "agents" / agent / f).exists(), f"{agent}/{f}")


if __name__ == "__main__":
    unittest.main()
