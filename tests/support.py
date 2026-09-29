"""Shared test setup: an isolated environment, a temp ledger and agents folder, and a stubbed HTTP call.

No test here reaches the network: every RouterCase replaces the opener the Jev backend uses.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from router import pick  # noqa: E402

JEV_VARS = ("JEV_API_KEY", "JEV_URL", "JEV_MODEL", "JEV_PRICE_PER_MTOK", "CLAUDE_PROJECT_DIR",
            "ROUTER_FOREGROUND", "ROUTER_LEDGER", "ROUTER_AGENTS_DIR")
FAKE_KEY = "test-key-not-a-real-secret"


class FakeResponse:
    def __init__(self, payload):
        self._body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def jev_reply(probs=None, choice=None, tokens=1000, cost=None):
    answer = {}
    if probs is not None:
        answer["probabilities"] = probs
    if choice is not None:
        answer["choice"] = choice
    usage = {"input_tokens": tokens}
    if cost is not None:
        usage["cost"] = cost
    return FakeResponse({"answers": {"decision": answer}, "usage": usage})


def agent_file(folder, name, *, model="", effort=""):
    lines = [f"name: {name}"] + ([f"model: {model}"] if model else []) + ([f"effort: {effort}"] if effort else [])
    path = Path(folder) / f"{name}.md"
    path.write_text("---\n" + "\n".join(lines) + "\n---\n\nYou are a test agent.\n", encoding="utf-8")
    return path


class RouterCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.agents = self.tmp / "agents"
        self.agents.mkdir()
        self.ledger = self.tmp / "picks.jsonl"
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for var in JEV_VARS:
            os.environ.pop(var, None)
        os.environ["ROUTER_LEDGER"] = str(self.ledger)
        os.environ["ROUTER_AGENTS_DIR"] = str(self.agents)
        patcher = mock.patch.object(pick._OPENER, "open", side_effect=RuntimeError("unstubbed HTTP call"))
        self.open = patcher.start()
        self.addCleanup(patcher.stop)
        no_session = mock.patch.object(pick, "cli_started", return_value=None)
        no_session.start()
        self.addCleanup(no_session.stop)

    def use_jev(self, response=None, side_effect=None):
        os.environ["JEV_API_KEY"] = FAKE_KEY
        if side_effect is not None:
            self.open.side_effect = side_effect
        else:
            self.open.side_effect = None
            self.open.return_value = response

    def rows(self):
        if not self.ledger.exists():
            return []
        return [json.loads(line) for line in self.ledger.read_text(encoding="utf-8").splitlines()]

    def brief(self, agent="o55-builder", description="Sub-task", prompt="Fix a typo in the README."):
        return {"subagent_type": agent, "description": description, "prompt": prompt}
