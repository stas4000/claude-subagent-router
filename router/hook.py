#!/usr/bin/env python3
"""PreToolUse hook on the Agent tool: route the builder, refuse Sonnet at max, keep sub-agents in the foreground.

For every Agent call, in this order:

1. A builder brief goes to the picker (router/pick.py), which may switch it between s55-builder,
   s55-builder-deep and o55-builder-precise. The switch rides updatedInput.subagent_type.
2. The agent that will run is checked by the guard (router/guard.py): a Sonnet agent whose file says
   effort max, or names no effort, is denied with the reason.
3. The call is rewritten to run in the foreground. A lead that hands work to a background sub-agent and
   then ends its turn loses that work: in print mode the sub-agent's notification arrives after the
   lead's final result and never wakes it. Parallel Agent calls in one message still run concurrently.
   Set ROUTER_FOREGROUND=0 to leave run_in_background alone.

Reads the hook JSON on stdin and prints a PreToolUse decision on stdout, or nothing when the call needs
no change. It never fails a tool call: malformed input, or any other tool, exits 0 with no output.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from router import guard, pick  # noqa: E402


def _decision(kind: str, reason: str, updated_input: dict | None = None) -> dict:
    out = {"hookEventName": "PreToolUse", "permissionDecision": kind, "permissionDecisionReason": reason}
    if updated_input is not None:
        out["updatedInput"] = updated_input
    return {"hookSpecificOutput": out}


def decide(payload: object) -> dict | None:
    """The hook's answer for one payload, or None when the call passes untouched."""
    if not isinstance(payload, dict) or payload.get("tool_name") != "Agent":
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    asked = str(tool_input.get("subagent_type") or "")
    picked = asked
    try:
        picked = pick.pick(tool_input, session_id=str(payload.get("session_id") or ""),
                           tool_use_id=str(payload.get("tool_use_id") or ""), started=pick.cli_started())
    except Exception:  # the lead's pick stands
        pass
    try:
        refusal = guard.sonnet_effort_refusal(picked, str(tool_input.get("model") or ""))
    except Exception:
        refusal = ""
    if refusal:
        return _decision("deny", refusal)
    foreground = os.environ.get("ROUTER_FOREGROUND") != "0"
    updated = dict(tool_input)
    reasons = []
    if picked != asked:
        updated["subagent_type"] = picked
        reasons.append(f"router moved {asked} to {picked}")
    if foreground and tool_input.get("run_in_background"):
        reasons.append("sub-agents run in the foreground: a turn that ends on background work loses it")
    if not reasons:
        return None
    if foreground:
        updated["run_in_background"] = False
    return _decision("allow", "; ".join(reasons), updated)


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
