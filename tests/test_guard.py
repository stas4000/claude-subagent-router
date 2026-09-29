"""The hard guard: Sonnet never runs at max, and never inherits an effort it could not name."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from router import guard  # noqa: E402


def agent_file(folder: Path, name: str, *, model: str = "", effort: str = "", raw: str = "") -> None:
    if raw:
        text = raw
    else:
        lines = [f"name: {name}"]
        if model:
            lines.append(f"model: {model}")
        if effort:
            lines.append(f"effort: {effort}")
        text = "---\n" + "\n".join(lines) + "\n---\n\nYou are a test agent.\n"
    (folder / f"{name}.md").write_text(text, encoding="utf-8")


class GuardTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.dirs = [self.dir]

    def refusal(self, name, **kw):
        return guard.sonnet_effort_refusal(name, dirs=self.dirs, **kw)

    def test_sonnet_at_max_is_refused(self):
        agent_file(self.dir, "s55-builder", model="claude-sonnet-5-5", effort="max")
        reason = self.refusal("s55-builder")
        self.assertIn("never runs at max", reason)
        self.assertIn("max", reason)

    def test_sonnet_without_effort_is_refused(self):
        agent_file(self.dir, "s55-builder", model="claude-sonnet-5-5")
        self.assertIn("never inherits", self.refusal("s55-builder"))

    def test_sonnet_with_unknown_effort_is_refused(self):
        agent_file(self.dir, "s55-builder", model="claude-sonnet-5-5", effort="ultra")
        self.assertTrue(self.refusal("s55-builder"))

    def test_sonnet_effort_is_case_and_quote_insensitive(self):
        agent_file(self.dir, "s55-builder", model="claude-sonnet-5-5", effort='"MAX"')
        self.assertTrue(self.refusal("s55-builder"))

    def test_sonnet_below_max_is_allowed(self):
        for effort in ("low", "medium", "high", "xhigh"):
            with self.subTest(effort=effort):
                agent_file(self.dir, "s55-builder", model="claude-sonnet-5-5", effort=effort)
                self.assertEqual(self.refusal("s55-builder"), "")

    def test_opus_is_allowed_at_any_effort(self):
        for effort in ("low", "medium", "high", "xhigh", "max", ""):
            with self.subTest(effort=effort or "none"):
                agent_file(self.dir, "o55-builder-precise", model="claude-opus-5-5", effort=effort)
                self.assertEqual(self.refusal("o55-builder-precise"), "")

    def test_any_sonnet_model_is_guarded_not_only_s55_names(self):
        agent_file(self.dir, "my-agent", model="sonnet", effort="max")
        self.assertTrue(self.refusal("my-agent"))
        agent_file(self.dir, "other", model="claude-sonnet-5", effort="max")
        self.assertTrue(self.refusal("other"))

    def test_s55_name_without_a_model_line_is_treated_as_sonnet(self):
        agent_file(self.dir, "s55-x", effort="max")
        self.assertTrue(self.refusal("s55-x"))

    def test_model_override_to_sonnet_on_an_opus_agent_at_max_is_refused(self):
        agent_file(self.dir, "o55-builder-precise", model="claude-opus-5-5", effort="max")
        self.assertTrue(self.refusal("o55-builder-precise", model_override="sonnet"))
        self.assertEqual(self.refusal("o55-builder-precise", model_override="opus"), "")

    def test_model_override_to_sonnet_needs_a_named_effort(self):
        agent_file(self.dir, "o55-reviewer", model="claude-opus-5-5")
        self.assertTrue(self.refusal("o55-reviewer", model_override="sonnet"))

    def test_unknown_agent_is_left_to_the_cli(self):
        self.assertEqual(self.refusal("does-not-exist"), "")
        self.assertEqual(self.refusal(""), "")

    def test_sonnet_override_with_no_agent_file_is_refused(self):
        # no file means no named effort: the sub-agent would inherit the lead's, which may be max
        self.assertIn("inherit", self.refusal("does-not-exist", model_override="sonnet"))
        self.assertIn("general-purpose", self.refusal("", model_override="claude-sonnet-5-5"))
        self.assertEqual(self.refusal("", model_override="opus"), "")

    def test_agent_name_cannot_walk_out_of_the_folder(self):
        outside = self.dir.parent / "outside-agent.md"
        outside.write_text("---\nmodel: sonnet\neffort: max\n---\n", encoding="utf-8")
        self.addCleanup(outside.unlink)
        sub = self.dir / "agents"
        sub.mkdir()
        self.assertEqual(guard.sonnet_effort_refusal("../outside-agent", dirs=[sub]), "")

    def test_first_folder_wins_like_project_over_user(self):
        project, user = self.dir / "project", self.dir / "user"
        project.mkdir(), user.mkdir()
        agent_file(project, "s55-builder", model="sonnet", effort="high")
        agent_file(user, "s55-builder", model="sonnet", effort="max")
        self.assertEqual(guard.sonnet_effort_refusal("s55-builder", dirs=[project, user]), "")
        self.assertTrue(guard.sonnet_effort_refusal("s55-builder", dirs=[user, project]))

    def test_body_text_that_looks_like_frontmatter_is_ignored(self):
        agent_file(self.dir, "s55-builder", raw=(
            "---\nname: s55-builder\nmodel: sonnet\neffort: high\n---\n\nRules:\neffort: max\n"))
        self.assertEqual(self.refusal("s55-builder"), "")

    def test_file_without_frontmatter_is_not_parsed(self):
        agent_file(self.dir, "s55-builder", raw="no frontmatter here\n---\neffort: high\n---\n")
        self.assertTrue(self.refusal("s55-builder"))  # an s55 agent with no effort named


if __name__ == "__main__":
    unittest.main()
