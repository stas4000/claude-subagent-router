"""The PreToolUse hook: output shape exactly as Claude Code expects, the pick, the guard, the foreground rewrite."""
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import support
from router import hook, pick
from support import RouterCase, agent_file, jev_reply

HOOK = Path(__file__).resolve().parents[1] / "router" / "hook.py"
ALLOW_KEYS = {"hookEventName", "permissionDecision", "permissionDecisionReason", "updatedInput"}
DENY_KEYS = {"hookEventName", "permissionDecision", "permissionDecisionReason"}


def payload(agent="o55-builder", prompt="Fix a typo in the README.", **extra):
    tool_input = {"subagent_type": agent, "description": "Fix typo", "prompt": prompt, **extra}
    return {"session_id": "sess-1", "hook_event_name": "PreToolUse", "tool_name": "Agent",
            "tool_use_id": "toolu_1", "cwd": "/tmp", "tool_input": tool_input}


class HookCase(RouterCase):
    """In-process: decide() with the HTTP call stubbed."""

    def hso(self, message):
        answer = hook.decide(message)
        self.assertIsNotNone(answer)
        self.assertEqual(list(answer), ["hookSpecificOutput"])
        return answer["hookSpecificOutput"]


class OutputShapeTest(HookCase):
    def test_allow_carries_exactly_the_documented_fields(self):
        out = self.hso(payload())
        self.assertEqual(set(out), ALLOW_KEYS)
        self.assertEqual((out["hookEventName"], out["permissionDecision"]), ("PreToolUse", "allow"))
        self.assertIsInstance(out["permissionDecisionReason"], str)
        self.assertIsInstance(out["updatedInput"], dict)

    def test_deny_carries_a_reason_and_no_updated_input(self):
        agent_file(self.agents, "s55-builder", model="claude-sonnet-5-5", effort="max")
        out = self.hso(payload(agent="s55-builder"))
        self.assertEqual(set(out), DENY_KEYS)
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("max", out["permissionDecisionReason"])

    def test_the_output_survives_json_and_keeps_every_other_input_field(self):
        message = payload(model="opus", run_in_background=True, isolation="worktree")
        agent_file(self.agents, "o55-builder-precise", model="claude-opus-5-5", effort="xhigh")
        out = json.loads(json.dumps(self.hso(message)))
        updated = out["updatedInput"]
        self.assertEqual(updated["subagent_type"], "s55-builder")
        self.assertIs(updated["run_in_background"], False)
        for key in ("description", "prompt", "model", "isolation"):
            self.assertEqual(updated[key], message["tool_input"][key])


class PickThroughTheHookTest(HookCase):
    def test_without_a_key_a_simple_brief_moves_to_the_fallback_pick(self):
        out = self.hso(payload(agent="o55-builder"))
        self.assertEqual(out["updatedInput"]["subagent_type"], "s55-builder")
        self.assertIn("o55-builder to s55-builder", out["permissionDecisionReason"])
        self.assertEqual(self.rows()[-1]["backend"], "rules")

    def test_jev_moves_the_brief_and_the_ledger_says_jev(self):
        self.use_jev(jev_reply(probs={"s55-builder": 0.1, "s55-builder-deep": 0.85, "o55-builder-precise": 0.05}))
        out = self.hso(payload(agent="o55-builder-precise"))
        self.assertEqual(out["updatedInput"]["subagent_type"], "s55-builder-deep")
        self.assertEqual(self.rows()[-1]["backend"], "jev")

    def test_a_pick_that_equals_the_ask_and_needs_no_rewrite_prints_nothing(self):
        self.assertIsNone(hook.decide(payload(agent="s55-builder")))

    def test_a_crashing_picker_leaves_the_leads_pick_standing(self):
        with mock.patch.object(pick, "pick", side_effect=RuntimeError("bug")):
            out = self.hso(payload(agent="o55-builder-precise", run_in_background=True))
        self.assertEqual(out["updatedInput"]["subagent_type"], "o55-builder-precise")
        self.assertIs(out["updatedInput"]["run_in_background"], False)


class GuardThroughTheHookTest(HookCase):
    def test_sonnet_at_max_is_denied(self):
        agent_file(self.agents, "s55-builder", model="claude-sonnet-5-5", effort="max")
        self.assertEqual(self.hso(payload(agent="s55-builder"))["permissionDecision"], "deny")

    def test_sonnet_without_an_effort_is_denied(self):
        agent_file(self.agents, "s55-builder-deep", model="claude-sonnet-5-5")
        out = self.hso(payload(agent="s55-builder-deep", prompt="Build a new module with a clear contract."))
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("none", out["permissionDecisionReason"])

    def test_opus_is_allowed_at_any_effort(self):
        for effort in ("low", "medium", "high", "xhigh", "max", ""):
            with self.subTest(effort=effort or "none"):
                agent_file(self.agents, "o55-builder-precise", model="claude-opus-5-5", effort=effort)
                message = payload(agent="o55-builder-precise", prompt="Fix the payments race.", run_in_background=True)
                out = self.hso(message)
                self.assertEqual(out["permissionDecision"], "allow")
                self.assertEqual(out["updatedInput"]["subagent_type"], "o55-builder-precise")

    def test_the_agent_the_router_picked_is_the_one_checked(self):
        agent_file(self.agents, "s55-builder", model="claude-sonnet-5-5")  # no effort
        out = self.hso(payload(agent="o55-builder"))  # the rules move it to s55-builder
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("s55-builder", out["permissionDecisionReason"])

    def test_agents_the_router_does_not_route_are_guarded_too(self):
        agent_file(self.agents, "s55-verifier", model="claude-sonnet-5-5", effort="max")
        self.assertEqual(self.hso(payload(agent="s55-verifier"))["permissionDecision"], "deny")
        agent_file(self.agents, "s55-verifier", model="claude-sonnet-5-5", effort="medium")
        self.assertIsNone(hook.decide(payload(agent="s55-verifier")))

    def test_a_sonnet_model_override_cannot_smuggle_an_opus_max_agent_past_the_guard(self):
        agent_file(self.agents, "o55-reviewer", model="claude-opus-5-5", effort="max")
        self.assertIsNone(hook.decide(payload(agent="o55-reviewer")))
        self.assertEqual(self.hso(payload(agent="o55-reviewer", model="sonnet"))["permissionDecision"], "deny")


class ForegroundTest(HookCase):
    def test_a_background_agent_is_rewritten_to_the_foreground(self):
        agent_file(self.agents, "s55-verifier", model="claude-sonnet-5-5", effort="medium")
        out = self.hso(payload(agent="s55-verifier", run_in_background=True))
        self.assertIs(out["updatedInput"]["run_in_background"], False)
        self.assertEqual(out["updatedInput"]["subagent_type"], "s55-verifier")
        self.assertIn("foreground", out["permissionDecisionReason"])

    def test_the_switch_leaves_run_in_background_alone(self):
        os.environ["ROUTER_FOREGROUND"] = "0"
        agent_file(self.agents, "s55-verifier", model="claude-sonnet-5-5", effort="medium")
        self.assertIsNone(hook.decide(payload(agent="s55-verifier", run_in_background=True)))
        out = self.hso(payload(agent="o55-builder", run_in_background=True))  # a pick still rides through
        self.assertIs(out["updatedInput"]["run_in_background"], True)


class SilenceTest(HookCase):
    def test_other_tools_and_malformed_payloads_are_ignored(self):
        for message in ({**payload(), "tool_name": "Bash"}, {**payload(), "tool_input": "x"},
                        {"tool_name": "Agent"}, [], "text", None, 7):
            with self.subTest(message=str(message)[:40]):
                self.assertIsNone(hook.decide(message))

    def test_main_prints_nothing_and_exits_zero_on_bad_stdin(self):
        for text in ("", "not json", "[1,2"):
            out = io.StringIO()
            self.assertEqual(hook.main(io.StringIO(text), out), 0)
            self.assertEqual(out.getvalue(), "")

    def test_main_writes_the_decision_as_json(self):
        out = io.StringIO()
        self.assertEqual(hook.main(io.StringIO(json.dumps(payload())), out), 0)
        self.assertEqual(json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"], "allow")


class CommandLineTest(unittest.TestCase):
    """The real script over a pipe, the way Claude Code runs it. No key is set, so nothing can reach the network."""

    def run_hook(self, stdin, agents_dir):
        env = {k: v for k, v in os.environ.items() if k not in support.JEV_VARS}
        env.update(ROUTER_AGENTS_DIR=str(agents_dir), ROUTER_LEDGER=str(Path(agents_dir) / "picks.jsonl"),
                   JEV_URL="https://jev.invalid/v1/systemone")
        return subprocess.run([sys.executable, str(HOOK)], input=stdin, env=env, capture_output=True,
                              text=True, timeout=30)

    def setUp(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.agents = Path(tmp.name)

    def test_o55_builder_without_a_key_prints_an_allow_with_the_fallback_pick(self):
        done = self.run_hook(json.dumps(payload(agent="o55-builder")), self.agents)
        self.assertEqual(done.returncode, 0, done.stderr)
        out = json.loads(done.stdout)["hookSpecificOutput"]
        self.assertEqual(set(out), ALLOW_KEYS)
        self.assertEqual(out["permissionDecision"], "allow")
        self.assertEqual(out["updatedInput"]["subagent_type"], "s55-builder")
        row = json.loads((self.agents / "picks.jsonl").read_text().splitlines()[-1])
        self.assertEqual((row["backend"], row["fallback"]), ("rules", True))

    def test_s55_builder_whose_file_says_max_prints_a_deny(self):
        agent_file(self.agents, "s55-builder", model="claude-sonnet-5-5", effort="max")
        done = self.run_hook(json.dumps(payload(agent="s55-builder")), self.agents)
        self.assertEqual(done.returncode, 0, done.stderr)
        out = json.loads(done.stdout)["hookSpecificOutput"]
        self.assertEqual(set(out), DENY_KEYS)
        self.assertEqual(out["permissionDecision"], "deny")

    def test_garbage_and_other_tools_print_nothing(self):
        for stdin in ("", "{", json.dumps({**payload(), "tool_name": "Read"})):
            done = self.run_hook(stdin, self.agents)
            self.assertEqual((done.returncode, done.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
