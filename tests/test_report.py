"""The ledger report: the math, torn lines, and a round trip through the real picker."""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

import support
from router import pick, report
from support import RouterCase, jev_reply


def row(picked, seconds=None, backend="jev", cost=None, fallback=False):
    out = {"picked": picked, "backend": backend, "fallback": fallback}
    if seconds is not None:
        out["seconds"] = seconds
    if cost is not None:
        out["cost_usd"] = cost
    return out


ROWS = [
    row("s55-builder", 0.4, cost=0.00004),
    row("s55-builder-deep", 0.6, cost=0.00004),
    row("o55-builder-precise", 0.0, backend="rules", fallback=True),
    row("o55-builder-precise", 9.0, backend="lead", fallback=True),  # a slow outlier: median, not mean
]


class SummarizeTest(unittest.TestCase):
    def test_the_numbers(self):
        s = report.summarize(ROWS)
        self.assertEqual(s["picks"], 4)
        self.assertEqual(s["by_profile"], {"o55-builder-precise": 2, "s55-builder": 1, "s55-builder-deep": 1})
        self.assertEqual(s["sonnet_pct"], 50.0)
        self.assertEqual(s["by_backend"], {"jev": 2, "lead": 1, "rules": 1})
        self.assertEqual(s["fallbacks"], 2)
        self.assertEqual(s["median_seconds"], 0.5)        # median of 0.0, 0.4, 0.6, 9.0 (the mean is 2.5)
        self.assertEqual(s["median_seconds_jev"], 0.5)    # median of 0.4, 0.6
        self.assertEqual(s["cost_total_usd"], 0.00008)
        self.assertEqual(s["cost_per_pick_usd"], 0.00002)  # over all four picks, the free ones included

    def test_median_of_an_odd_count_and_the_share_to_sonnet(self):
        rows = [row("s55-builder", 9.0), row("s55-builder", 0.1), row("o55-builder-precise", 0.5)]
        s = report.summarize(rows)
        self.assertEqual(s["median_seconds"], 0.5)  # the mean would be 3.2
        self.assertEqual(s["sonnet_pct"], 66.7)

    def test_rows_without_numbers_or_with_junk_numbers_do_not_break_it(self):
        rows = [row("s55-builder"), {"picked": "s55-builder", "seconds": "fast", "cost_usd": True},
                {"picked": "o55-builder-precise", "seconds": 2.0, "cost_usd": 0.5}]
        s = report.summarize(rows)
        self.assertEqual((s["median_seconds"], s["cost_total_usd"]), (2.0, 0.5))
        self.assertEqual(s["by_backend"], {"jev": 1, "unknown": 2})

    def test_an_empty_ledger(self):
        s = report.summarize([])
        self.assertEqual((s["picks"], s["sonnet_pct"], s["median_seconds"], s["cost_per_pick_usd"]),
                         (0, None, None, None))
        self.assertEqual(report.render(s), "no picks in the ledger yet")

    def test_render_names_every_number(self):
        text = report.render(report.summarize(ROWS))
        for part in ("picks: 4", "o55-builder-precise", "(50.0%)", "to Sonnet: 50.0%", "jev 2, lead 1, rules 1",
                     "fallbacks (Jev did not answer): 2", "median seconds a pick: 0.5 (Jev only: 0.5)",
                     "$0.0001 total", "$0.00002 a pick"):
            self.assertIn(part, text)


class LoadTest(unittest.TestCase):
    def test_torn_and_foreign_lines_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "picks.jsonl"
            path.write_text("\n".join([json.dumps(row("s55-builder", 0.1)), '{"picked": "o55-builder-pre',
                                       "", "[1, 2]", '{"no": "pick"}', '{"picked": 3}',
                                       json.dumps(row("o55-builder-precise", 0.2))]), encoding="utf-8")
            self.assertEqual([r["picked"] for r in report.load(path)], ["s55-builder", "o55-builder-precise"])
            self.assertEqual(report.load(Path(tmp) / "missing.jsonl"), [])


class RoundTripTest(RouterCase):
    def test_what_the_picker_writes_the_report_reads(self):
        self.use_jev(jev_reply(probs={"s55-builder": 0.9, "s55-builder-deep": 0.05, "o55-builder-precise": 0.05}))
        pick.pick(self.brief())
        pick.pick(self.brief())
        self.use_jev(side_effect=OSError("down"))
        pick.pick(self.brief(prompt="Fix the payments race."))
        out = io.StringIO()
        self.assertEqual(report.main([str(self.ledger), "--json"], out), 0)
        s = json.loads(out.getvalue())
        self.assertEqual(s["picks"], 3)
        self.assertEqual(s["by_backend"], {"jev": 2, "rules": 1})
        self.assertEqual(s["by_profile"], {"o55-builder-precise": 1, "s55-builder": 2})
        self.assertEqual(s["fallbacks"], 1)
        self.assertAlmostEqual(s["cost_total_usd"], 0.000084)

    def test_the_default_ledger_is_the_hooks_own(self):
        self.use_jev(jev_reply(probs={"s55-builder": 1.0}))
        pick.pick(self.brief())
        out = io.StringIO()
        report.main([], out)
        self.assertIn("picks: 1", out.getvalue())


if __name__ == "__main__":
    unittest.main()
