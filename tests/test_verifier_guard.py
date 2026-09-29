"""The verifier writes tests, never implementation code."""
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from router import verifier_guard as vg  # noqa: E402


def call(agent_type, path, key="file_path"):
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Edit", "tool_input": {key: path}}
    if agent_type:
        payload["agent_type"] = agent_type
    return payload


class VerifierGuardTest(unittest.TestCase):
    def test_implementation_paths_are_denied_inside_the_verifier(self):
        for path in ("/repo/router/pick.py", "/repo/src/app.js", "README.md", "/repo/contest/main.py", ""):
            with self.subTest(path=path):
                answer = vg.decide(call("s55-verifier", path))
                out = answer["hookSpecificOutput"]
                self.assertEqual(set(out), {"hookEventName", "permissionDecision", "permissionDecisionReason"})
                self.assertEqual(out["permissionDecision"], "deny")

    def test_test_paths_are_allowed(self):
        for path in ("/repo/tests/test_pick.py", "/repo/test/helpers.py", "/repo/web/__tests__/a.js",
                     "/repo/web/app.test.ts", "/repo/web/app.spec.js", "/repo/pkg/parser_test.go",
                     "/repo/ios/AppTests.swift", "/repo/ios/AppUITests/Flow.swift", "/tmp/scratch/check.py",
                     "C:\\repo\\tests\\test_x.py".replace("C:", "")):
            with self.subTest(path=path):
                self.assertIsNone(vg.decide(call("s55-verifier", path)))

    def test_notebooks_are_judged_by_their_path_too(self):
        self.assertIsNotNone(vg.decide(call("s55-verifier", "/repo/analysis.ipynb", key="notebook_path")))
        self.assertIsNone(vg.decide(call("s55-verifier", "/repo/tests/check.ipynb", key="notebook_path")))

    def test_everyone_else_is_untouched(self):
        for agent_type in ("s55-builder", "o55-builder-precise", "o55-reviewer", "", None):
            with self.subTest(agent_type=agent_type):
                self.assertIsNone(vg.decide(call(agent_type, "/repo/router/pick.py")))

    def test_malformed_input_is_ignored(self):
        for message in ("x", [], None, {"agent_type": "s55-verifier"}, {"agent_type": "s55-verifier", "tool_input": 3}):
            self.assertIsNone(vg.decide(message))
        out = io.StringIO()
        self.assertEqual(vg.main(io.StringIO("not json"), out), 0)
        self.assertEqual(out.getvalue(), "")

    def test_main_prints_the_deny_as_json(self):
        out = io.StringIO()
        vg.main(io.StringIO(json.dumps(call("s55-verifier", "/repo/router/pick.py"))), out)
        self.assertEqual(json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()
