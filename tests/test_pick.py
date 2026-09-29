"""Pick routing: the rules picker, the Jev backend (HTTP mocked), fallback, and the lead's pick standing."""
import json
import os
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

import support
from router import pick
from support import FAKE_KEY, RouterCase, jev_reply

OPUS, DEEP, BUILDER = "o55-builder-precise", "s55-builder-deep", "s55-builder"


class RulesPickerTest(unittest.TestCase):
    def rules(self, brief, description=""):
        return pick.rules_pick({"description": description, "brief": brief})

    def test_each_risk_rule_moves_a_brief_to_opus(self):
        cases = {
            "sends-for-someone": "Build the auto-reply engine that answers customers by email.",
            "money": "Add refunds to the checkout flow and its billing report.",
            "voice": "Reduce latency in the real-time voice pipeline.",
            "wire": "Change the wire format of the sync payload the mobile apps read.",
            "harness": "Change how the PreToolUse hook rewrites the agent call.",
            "concurrency-security": "Fix the race between two workers taking the same job lock.",
            "shared-state": "Move the shared state file to a new location.",
            "repeat-after-failure": "The previous builder failed on this and broke the parser, take over.",
        }
        self.assertEqual(set(cases), set(pick._OPUS))  # every rule has a case
        for rule, brief in cases.items():
            with self.subTest(rule=rule):
                got = self.rules(brief)
                self.assertEqual(got["picked"], OPUS)
                self.assertIn(rule, got["matched"])

    def test_a_bounded_change_goes_to_the_high_builder(self):
        for brief in ("Fix a typo in the README and update the changelog.",
                      "Change the button color on the pricing page to match the design.",
                      "Apply the three review fixes listed below to the parser test."):
            with self.subTest(brief=brief):
                self.assertEqual(self.rules(brief), {"picked": BUILDER, "matched": []})

    def test_a_new_capability_goes_to_the_deep_builder(self):
        for brief in ("Build a new module with a clear contract that exports feeds.",
                      "Add a full-stack feature: an API, a database table and its own front end.",
                      "Rename the helper across several files of the package."):
            with self.subTest(brief=brief):
                self.assertEqual(self.rules(brief)["picked"], DEEP)

    def test_opus_wins_over_deep(self):
        got = self.rules("Build a new module that takes payments.")
        self.assertEqual(got["picked"], OPUS)

    def test_etiquette_is_not_risk(self):
        for brief in ("Update the docs page. Do not restart the server, never send anything, do not touch the phone.",
                      "Rename a CSS class. Never print or copy any credentials to the log.",
                      "Tidy the changelog. Never git reset or checkout anything, no deploy."):
            with self.subTest(brief=brief):
                self.assertEqual(self.rules(brief)["picked"], BUILDER)

    def test_a_warning_does_not_hide_a_real_risk(self):
        got = self.rules("Do not restart the server. Fix the payments webhook.")
        self.assertEqual(got["picked"], OPUS)
        got = self.rules("Never let two workers publish twice: fix the race.")
        self.assertEqual(got["picked"], OPUS)

    def test_the_description_counts_too(self):
        self.assertEqual(self.rules("Small change.", description="Fix billing bug")["picked"], OPUS)

    def test_nothing_to_judge_is_unsure_so_opus(self):
        self.assertEqual(self.rules("")["picked"], OPUS)
        self.assertEqual(self.rules("   \n ", description="")["picked"], OPUS)

    def test_it_only_ever_answers_one_of_the_three_choices(self):
        for brief in ("", "x", "Fix billing", "New module", "Rename a variable", "Do not send anything"):
            self.assertIn(self.rules(brief)["picked"], pick.CHOICES)


class JevBackendTest(RouterCase):
    def test_the_highest_probability_wins_for_each_choice(self):
        for name in pick.PICKS:
            with self.subTest(pick=name):
                probs = {n: 0.1 for n in pick.PICKS}
                probs[name] = 0.8
                self.use_jev(jev_reply(probs=probs))
                self.assertEqual(pick.pick(self.brief()), name)
                row = self.rows()[-1]
                self.assertEqual((row["backend"], row["fallback"], row["p"]), ("jev", False, 0.8))

    def test_a_plain_choice_answer_is_accepted(self):
        self.use_jev(jev_reply(choice=DEEP))
        self.assertEqual(pick.pick(self.brief()), DEEP)
        self.assertIsNone(self.rows()[-1]["p"])

    def test_jev_can_move_the_brief_off_the_leads_pick_both_ways(self):
        self.use_jev(jev_reply(probs={BUILDER: 0.9, DEEP: 0.05, OPUS: 0.05}))
        self.assertEqual(pick.pick(self.brief(agent=OPUS)), BUILDER)
        self.use_jev(jev_reply(probs={BUILDER: 0.1, DEEP: 0.1, OPUS: 0.8}))
        self.assertEqual(pick.pick(self.brief(agent=BUILDER)), OPUS)

    def test_the_request_is_a_system_one_call_with_a_bearer_key(self):
        self.use_jev(jev_reply(probs={BUILDER: 1.0}))
        long_brief = "word  \n\t " * 400  # whitespace collapses, then the first 1,000 characters go out
        pick.pick(self.brief(description="d" * 300, prompt=long_brief))
        (request,), kwargs = self.open.call_args
        self.assertEqual(request.full_url, "https://api.typesafe.ai/v1/systemone")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + FAKE_KEY)
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(kwargs["timeout"], pick.WAIT_S)
        body = json.loads(request.data)
        self.assertEqual(body["model"], "jev-latest")
        ctx = body["state"]["context"]
        self.assertEqual(len(ctx["description"]), 200)
        self.assertEqual(len(ctx["brief"]), 1000)
        self.assertNotIn("\n", ctx["brief"])
        self.assertEqual(body["questions"], {"decision": {
            "type": "choice", "instructions": pick.INSTRUCTIONS, "criteria": pick.CHOICES}})

    def test_url_and_model_can_be_overridden(self):
        os.environ["JEV_URL"] = "https://jev.example.invalid/v1/systemone"
        os.environ["JEV_MODEL"] = "jev-test"
        self.use_jev(jev_reply(probs={BUILDER: 1.0}))
        pick.pick(self.brief())
        (request,), _ = self.open.call_args
        self.assertEqual(request.full_url, "https://jev.example.invalid/v1/systemone")
        self.assertEqual(json.loads(request.data)["model"], "jev-test")

    def test_cost_comes_from_tokens_unless_the_reply_carries_a_price(self):
        self.use_jev(jev_reply(probs={BUILDER: 1.0}, tokens=1000))
        pick.pick(self.brief())
        self.assertEqual(self.rows()[-1]["cost_usd"], 0.000042)
        self.assertEqual(self.rows()[-1]["input_tokens"], 1000)
        os.environ["JEV_PRICE_PER_MTOK"] = "1"
        pick.pick(self.brief())
        self.assertEqual(self.rows()[-1]["cost_usd"], 0.001)
        self.use_jev(jev_reply(probs={BUILDER: 1.0}, tokens=1000, cost=0.5))
        pick.pick(self.brief())
        self.assertEqual(self.rows()[-1]["cost_usd"], 0.5)


class FallbackTest(RouterCase):
    def assert_rules_answered(self, reason_part):
        row = self.rows()[-1]
        self.assertEqual((row["backend"], row["fallback"]), ("rules", True))
        self.assertIn(reason_part, row["reason"])
        return row

    def test_no_key_uses_the_rules_and_never_calls_out(self):
        self.assertEqual(pick.pick(self.brief()), BUILDER)
        self.open.assert_not_called()
        self.assert_rules_answered("JEV_API_KEY is not set")

    def test_the_rules_pick_can_still_be_opus(self):
        self.assertEqual(pick.pick(self.brief(prompt="Fix the payments race.")), OPUS)
        self.assert_rules_answered("JEV_API_KEY")

    def test_a_timeout_falls_back_to_the_rules(self):
        self.use_jev(side_effect=lambda *a, **k: time.sleep(2))
        with mock.patch.object(pick, "WAIT_S", 0.2):
            started = time.monotonic()
            self.assertEqual(pick.pick(self.brief()), BUILDER)
        self.assertLess(time.monotonic() - started, 1.5)
        row = self.assert_rules_answered("TimeoutError")
        self.assertGreaterEqual(row["seconds"], 0.2)

    def test_an_http_error_falls_back_to_the_rules(self):
        err = urllib.error.HTTPError("https://x.invalid", 503, "Service Unavailable", {}, None)
        self.use_jev(side_effect=err)
        self.assertEqual(pick.pick(self.brief()), BUILDER)
        self.assert_rules_answered("503")

    def test_a_connection_error_falls_back_to_the_rules(self):
        self.use_jev(side_effect=urllib.error.URLError("no route"))
        self.assertEqual(pick.pick(self.brief()), BUILDER)
        self.assert_rules_answered("URLError")

    def test_an_unreadable_answer_falls_back_to_the_rules(self):
        for reply in (support.FakeResponse(b"<html>gateway</html>"),
                      support.FakeResponse({"usage": {}}),
                      support.FakeResponse({"answers": {"decision": {"choice": "gpt-builder"}}}),
                      jev_reply(probs={"gpt-builder": 1.0}),
                      jev_reply(probs={BUILDER: 1.5}),
                      jev_reply(probs={BUILDER: True}),
                      jev_reply(probs={BUILDER: float("nan")})):
            with self.subTest(reply=reply.read()[:60]):
                self.use_jev(reply)
                self.assertEqual(pick.pick(self.brief(prompt="Fix billing.")), OPUS)
                self.assertEqual(self.rows()[-1]["backend"], "rules")

    def test_a_non_https_url_never_receives_the_key(self):
        os.environ["JEV_URL"] = "http://jev.example.invalid/v1"
        self.use_jev(jev_reply(probs={DEEP: 1.0}))
        self.assertEqual(pick.pick(self.brief()), BUILDER)
        self.open.assert_not_called()
        self.assert_rules_answered("JEV_URL must be https")

    def test_a_redirect_is_never_followed_so_the_key_stays_on_one_host(self):
        handler = pick._NoRedirect()
        request = urllib.request.Request("https://api.typesafe.ai/v1/systemone")
        self.assertIsNone(handler.redirect_request(request, None, 302, "Found", {}, "https://elsewhere.invalid/"))
        self.assertTrue(any(isinstance(h, pick._NoRedirect) for h in pick._OPENER.handlers))

    def test_the_key_never_reaches_the_ledger_even_when_an_error_carries_it(self):
        self.use_jev(side_effect=RuntimeError(f"rejected token {FAKE_KEY}"))
        pick.pick(self.brief())
        self.assertNotIn(FAKE_KEY, self.ledger.read_text())
        self.assertIn("[key]", self.rows()[-1]["reason"])


class LeadStandsTest(RouterCase):
    def test_the_leads_pick_stands_when_both_backends_fail(self):
        self.use_jev(side_effect=urllib.error.URLError("down"))
        with mock.patch.object(pick, "rules_pick", side_effect=RuntimeError("bug")):
            self.assertEqual(pick.pick(self.brief(agent=OPUS)), OPUS)
        row = self.rows()[-1]
        self.assertEqual((row["backend"], row["picked"], row["fallback"]), ("lead", OPUS, True))
        self.assertIn("URLError", row["reason"])
        self.assertIn("rules: RuntimeError", row["reason"])

    def test_an_agent_the_session_cannot_know_is_not_offered(self):
        asked = support.agent_file(self.agents, OPUS, model="claude-opus-5-5", effort="xhigh")
        picked = support.agent_file(self.agents, BUILDER, model="claude-sonnet-5-5", effort="high")
        os.utime(asked, (1000, 1000))
        os.utime(picked, (3000, 3000))
        self.use_jev(jev_reply(probs={BUILDER: 0.9, DEEP: 0.05, OPUS: 0.05}))
        self.assertEqual(pick.pick(self.brief(agent=OPUS), started=2000.0), OPUS)
        row = self.rows()[-1]
        self.assertEqual((row["backend"], row["picked"], row["fallback"]), ("lead", OPUS, True))
        self.assertIn("newer than this session", row["reason"])
        # the same pick is fine once the session started after both files
        self.assertEqual(pick.pick(self.brief(agent=OPUS), started=4000.0), BUILDER)

    def test_late_agent_only_says_yes_when_it_can_show_it(self):
        asked = support.agent_file(self.agents, OPUS, model="claude-opus-5-5", effort="xhigh")
        picked = support.agent_file(self.agents, BUILDER, model="claude-sonnet-5-5", effort="high")
        for path, when in ((asked, 1000), (picked, 3000)):
            os.utime(path, (when, when))
        late = pick.late_agent
        self.assertTrue(late(OPUS, BUILDER, 2000.0))                       # asked known, picked newer
        self.assertFalse(late(OPUS, BUILDER, 4000.0))                      # both older than the session
        self.assertFalse(late(OPUS, BUILDER, None))                        # session start unknown
        self.assertFalse(late(OPUS, BUILDER, 500.0))                       # even the lead's agent is newer: cannot tell
        self.assertFalse(late("o55-builder", BUILDER, 2000.0))             # lead's agent has no file: cannot tell
        self.assertTrue(late(OPUS, DEEP, 2000.0))                          # picked has no file at all

    def test_a_pick_equal_to_the_ask_is_never_second_guessed(self):
        self.assertEqual(pick.pick(self.brief(agent=BUILDER), started=1.0), BUILDER)
        self.assertEqual(self.rows()[-1]["backend"], "rules")

    def test_only_builder_briefs_are_routed(self):
        for agent in ("s55-verifier", "o55-reviewer", "s55-scout", "Explore", ""):
            with self.subTest(agent=agent):
                self.use_jev(jev_reply(probs={OPUS: 1.0}))
                self.assertEqual(pick.pick(self.brief(agent=agent)), agent)
        self.open.assert_not_called()
        self.assertEqual(self.rows(), [])

    def test_the_medium_opus_builder_name_is_routed_like_any_other(self):
        self.assertEqual(pick.pick(self.brief(agent="o55-builder")), BUILDER)
        self.assertEqual(self.rows()[-1]["asked"], "o55-builder")


class LedgerTest(RouterCase):
    def test_one_line_per_decision_with_no_brief_text(self):
        secret_brief = "Rename the variable frobnicate_widget everywhere it appears in the script."
        pick.pick(self.brief(description="d" * 300, prompt=secret_brief), session_id="s1", tool_use_id="t1")
        pick.pick(self.brief(prompt="Fix billing."), session_id="s1", tool_use_id="t2")
        lines = self.ledger.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        first = json.loads(lines[0])
        self.assertEqual(len(first["description"]), 120)
        self.assertEqual((first["session_id"], first["tool_use_id"], first["asked"]), ("s1", "t1", "o55-builder"))
        for field in ("at", "seconds", "backend", "picked", "fallback"):
            self.assertIn(field, first)
        self.assertNotIn("frobnicate_widget", self.ledger.read_text())

    def test_an_unwritable_ledger_never_blocks_a_decision(self):
        os.environ["ROUTER_LEDGER"] = str(self.tmp)  # a directory
        self.assertEqual(pick.pick(self.brief()), BUILDER)


if __name__ == "__main__":
    unittest.main()
