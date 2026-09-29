"""The hard guard: a Sonnet sub-agent never runs at max.

An agent whose file says `effort: max`, or names no effort at all (it would then inherit the lead's,
which may be max), is refused with the reason. Opus agents are never refused here.

The rule keys on the model, not on the agent's name: any agent whose `model:` line names Sonnet is
guarded, and so is a call that overrides the model to Sonnet through the Agent tool's `model` argument.
An `s55-` agent with no `model:` line is treated as Sonnet.

This module also finds agent files (project folder first, then this repo, then the user folder), which
the pick step uses for its own checks.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SONNET_EFFORTS = ("low", "medium", "high", "xhigh")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")  # an agent name is a file stem, never a path


def agent_dirs() -> list[Path]:
    """Where Claude Code loads agents from, most specific first. ROUTER_AGENTS_DIR replaces the list."""
    override = os.environ.get("ROUTER_AGENTS_DIR")
    if override:
        return [Path(override)]
    dirs = []
    project = os.environ.get("CLAUDE_PROJECT_DIR")
    if project:
        dirs.append(Path(project) / ".claude" / "agents")
    dirs += [ROOT / ".claude" / "agents", Path.home() / ".claude" / "agents"]
    return dirs


def find_agent(name: str, dirs: list[Path] | None = None) -> Path | None:
    if not NAME.match(name or ""):
        return None
    for folder in dirs if dirs is not None else agent_dirs():
        path = folder / f"{name}.md"
        if path.is_file():
            return path
    return None


def frontmatter(path: Path) -> dict[str, str] | None:
    """The `key: value` lines between the opening `---` and the next `---`. None when unreadable."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    meta: dict[str, str] = {}
    if not lines or lines[0].strip() != "---":
        return meta
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, sep, value = line.partition(":")
        if sep and key.strip() and not key.startswith((" ", "\t")):
            meta.setdefault(key.strip(), value.split(" #", 1)[0].strip().strip("'\""))
    return meta


def sonnet_effort_refusal(agent: str, model_override: str = "", dirs: list[Path] | None = None) -> str:
    """The reason to deny this Agent call, or "" when it may run."""
    override_sonnet = "sonnet" in (model_override or "").lower()
    path = find_agent(agent, dirs)
    meta = frontmatter(path) if path else {}
    if meta is None:
        return ""  # a file the CLI cannot read is the CLI's to refuse
    if path is None:
        if not override_sonnet:
            return ""  # an unknown agent is the CLI's to refuse
        label = agent or "the general-purpose agent"
        return (f"{label} was asked to run on Sonnet with no agent file to name its effort, so it would "
                f"inherit the lead's effort, and Sonnet never runs at max. Use an s55 agent whose file "
                f"says effort {', '.join(SONNET_EFFORTS[:-1])} or {SONNET_EFFORTS[-1]}.")
    model = (model_override or meta.get("model") or "").lower()
    if not ("sonnet" in model if model else agent.startswith("s55-")):
        return ""
    effort = (meta.get("effort") or "").lower()
    if effort in SONNET_EFFORTS:
        return ""
    return (f"{agent} runs on Sonnet, which never runs at max and never inherits the lead's effort: "
            f"its file must say effort {', '.join(SONNET_EFFORTS[:-1])} or {SONNET_EFFORTS[-1]} "
            f"(found {effort or 'none'}).")
