#!/usr/bin/env python3
"""PreToolUse hook: the s55-verifier may write tests, never implementation code.

Wire it on Edit, Write, MultiEdit and NotebookEdit (.claude/settings.example.json). It acts only when the
call runs inside the verifier (the hook input carries agent_type inside a sub-agent), so the builders and
the lead are untouched. A path counts as a test when a folder is tests/test/__tests__/*Tests/*UITests, or
the file name is test_*.py, *_test.*, *.test.*, *.spec.*, *Tests.swift, or it lives in /tmp. Anything else
is denied with the reason: the finding goes back to the builder, who applies the fix.

It sees the edit tools only. A shell redirect inside the verifier is not seen; the agent's own rules
forbid it. Not in the agent's frontmatter: hooks declared there do not run in every mode (print mode with
an untrusted workspace skips them), while settings hooks do.

Malformed input exits 0 with no output, like router/hook.py.
"""
from __future__ import annotations

import json
import re
import sys

VERIFIERS = {"s55-verifier"}
TEST_PATH = re.compile(
    r"(?:^|/)(?:tests?|__tests__|[A-Za-z0-9_]*Tests|[A-Za-z0-9_]*UITests)/"
    r"|(?:^|/)test_[^/]*\.py$|_test\.[A-Za-z0-9]+$|\.(?:test|spec)\.[A-Za-z0-9]+$|Tests?\.swift$"
    r"|^/tmp/"
)


def allowed(path: str) -> bool:
    return bool(path) and bool(TEST_PATH.search(path.replace("\\", "/")))


def decide(payload: object) -> dict | None:
    if not isinstance(payload, dict) or payload.get("agent_type") not in VERIFIERS:
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
    if allowed(path):
        return None
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": (
            f"The verifier writes tests only; {path or 'this file'} is implementation code. "
            "Report the defect as a finding with the exact fix, the builder applies it."),
    }}


def main(stdin=None, stdout=None) -> int:
    try:
        payload = json.load(stdin or sys.stdin)
    except Exception:
        return 0
    answer = decide(payload)
    if answer is not None:
        json.dump(answer, stdout or sys.stdout)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
