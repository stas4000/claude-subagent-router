"""Pick the builder profile for each sub-task.

The lead names a builder. This module decides which of three profiles the brief really needs:

    s55-builder          Sonnet 5.5, high: bounded work that follows an existing pattern
    s55-builder-deep     Sonnet 5.5, extra high: well specified work across several files of one subsystem
    o55-builder-precise  Opus 5.5, extra high: shared state, money, auth, sends, wire formats, repeats after failure

Two backends answer, in this order:

1. Jev (a System One classifier: one HTTPS request, a probability per choice, no text back), when
   JEV_API_KEY is set. It gets 12 seconds.
2. An offline rule-based picker with the same three choices and the same risk rules, when the key is unset
   or Jev fails (timeout, HTTP error, unreadable answer).

If neither answers, or the pick is an agent the running session cannot know, the lead's pick stands.
Every decision is one line in a JSONL ledger that says which backend answered (router/report.py reads it).
Only builder briefs are routed: a builder brief can become another builder, never a reviewer.
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from router import guard

ROOT = guard.ROOT

DEFAULT_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
DEFAULT_PRICE_PER_MTOK = 0.042  # USD per million input tokens; the direct API reports tokens, not a price

#: The picker's three answers, with the text Jev sees for each.
CHOICES = {
    "s55-builder": "Sonnet 5.5 builder on high: clearly defined, bounded work that follows an existing pattern.",
    "s55-builder-deep": "Sonnet 5.5 builder on extra high: well specified work across several files of one subsystem, a new module or small package with a clear contract, a full-stack feature inside one app, with no live shared state, money, auth or sends.",
    "o55-builder-precise": "Opus 5.5 builder on extra high: connects several systems or clients, shared or live state, money/auth/security, sends on someone's behalf, a new brain or harness change, or a repeat after failure.",
}

INSTRUCTIONS = (
    "You route ONE sub-task a lead is about to hand off. Quality comes first, then tokens: pick the cheapest "
    "builder that will still do it right. You see the sub-task's short description and the start of its brief. "
    "Judge the change the builder must make, not the warnings around it (dirty worktree, do not restart, do not "
    "send are etiquette, not risk).\n\n"
    "Pick o55-builder-precise (Opus 5.5, extra high) when the change itself does ANY of these:\n"
    "- builds or changes an engine that composes, sends or publishes messages or posts for someone (publisher, "
    "auto-reply, outreach, inbox engine);\n"
    "- touches money or its measurement: payments, purchases, checkout, billing, conversion or purchase tracking, "
    "trials;\n"
    "- touches real-time voice, audio, calls or the earpiece;\n"
    "- connects a server and a client, or changes a wire format or payload other apps read;\n"
    "- adds or changes a brain, model route, harness or hook behavior, or diagnoses a runtime bug from live "
    "evidence;\n"
    "- fixes races, locks, schedulers or concurrency; enforces security, auth, permissions or tenant isolation;\n"
    "- takes over after a previous builder failed or introduced the defect.\n\n"
    "Otherwise pick s55-builder-deep (Sonnet 5.5, extra high) for a well specified NEW capability across several "
    "files: a new module, registry, runner or small package with a clear contract, or a full-stack feature inside "
    "one app (API + DB + its own front end).\n\n"
    "Otherwise pick s55-builder (Sonnet 5.5, high): clearly defined and bounded work, one file or one screen, one "
    "client's side of a feature against a fixed contract, a UI change that follows an existing pattern, applying "
    "review fixes already written out, a small script, a doc, log or test update.\n\n"
    "When unsure between a Sonnet builder and Opus, pick Opus: a wasted Opus run costs less than a broken shared "
    "path. When unsure between s55-builder and s55-builder-deep, pick s55-builder-deep."
)

PICKS = tuple(CHOICES)
#: The Opus medium builder a lead may still name (not shipped here): its brief is routed like any other.
BUILDERS = PICKS + ("o55-builder",)
BRIEF_CHARS = 1000  # the picker sees the first 1,000 characters of a brief
WAIT_S = 12.0       # a Jev call answers in about half a second; the hook itself is killed at 30


# --- ledger -----------------------------------------------------------------------------------------

def ledger_path() -> Path:
    override = os.environ.get("ROUTER_LEDGER")
    if override:
        return Path(override)
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or ROOT) / ".router" / "picks.jsonl"


def log(row: dict) -> None:
    """One line, appended. A ledger that cannot be written never blocks a decision."""
    path = ledger_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode())
        finally:
            os.close(fd)
    except OSError:
        pass


def context(tool_input: dict) -> dict:
    """What the picker sees: the short description and the start of the brief."""
    brief = " ".join(str(tool_input.get("prompt") or "").split())[:BRIEF_CHARS]
    return {"description": str(tool_input.get("description") or "")[:200], "brief": brief}


# --- backend 1: Jev over HTTPS ----------------------------------------------------------------------

class JevUnavailable(RuntimeError):
    """No key, or a URL the key must not be sent to."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a redirect would carry the bearer key to another host


_OPENER = urllib.request.build_opener(_NoRedirect)


def _choice_from(answer: dict) -> tuple[str, float | None]:
    """The winning choice and its probability, from one answer of the response."""
    if not isinstance(answer, dict):
        raise ValueError("missing answer")
    probs = answer.get("probabilities") or {}
    if probs:
        if not isinstance(probs, dict) or set(probs) - CHOICES.keys() or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and 0 <= v <= 1
            for v in probs.values()
        ):
            raise ValueError("invalid probabilities")
        chosen = max(probs, key=probs.get)
        return chosen, float(probs[chosen])
    chosen = answer.get("choice")
    if chosen not in CHOICES:
        raise ValueError("no valid choice in the answer")
    return chosen, None


def ask_jev(ctx: dict) -> dict:
    """One System One call. Raises on anything but a clean, valid answer."""
    key = os.environ.get("JEV_API_KEY", "").strip()
    if not key:
        raise JevUnavailable("JEV_API_KEY is not set")
    url = os.environ.get("JEV_URL", DEFAULT_URL)
    if not url.startswith("https://"):
        raise JevUnavailable("JEV_URL must be https")
    body = {
        "model": os.environ.get("JEV_MODEL", DEFAULT_MODEL),
        "state": {"context": ctx},
        "questions": {"decision": {"type": "choice", "instructions": INSTRUCTIONS, "criteria": CHOICES}},
    }
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with _OPENER.open(request, timeout=WAIT_S) as response:
        out = json.loads(response.read())
    answers = out.get("answers")
    if not isinstance(answers, dict):
        raise ValueError("answers must be a mapping")
    picked, probability = _choice_from(answers.get("decision"))
    usage = out.get("usage") or {}
    tokens = int(usage.get("input_tokens") or 0)
    price = float(os.environ.get("JEV_PRICE_PER_MTOK") or DEFAULT_PRICE_PER_MTOK) / 1e6
    cost = float(usage.get("cost") or 0) or tokens * price
    return {"picked": picked, "p": round(probability, 3) if probability is not None else None,
            "input_tokens": tokens, "cost_usd": round(cost, 6)}


# --- backend 2: offline rules -----------------------------------------------------------------------

#: Etiquette in a brief is not risk ("do not restart", "never send anything"): the clause is dropped first.
_ETIQUETTE = re.compile(
    r"\b(?:do not|don't|dont|never|must not|mustn't|without|no)\s+(?:git\s+)?"
    r"(?:restart|deploy|send|publish|post|push|commit|touch|revert|reset|checkout|stash|reformat|install|print|"
    r"copy|log|paste|echo|use|run|delete|remove|open|call|trigger|register)\w*\b[^.;:\n]*", re.I)

#: The instructions' risk bullets, as keywords. Deliberately broad: a false Opus costs a little, a false
#: Sonnet on shared state costs a broken path.
_OPUS_RULES = {
    "sends-for-someone": (
        r"\b(?:publisher|publishing engine|auto-?repl(?:y|ies)|outreach|inbox (?:engine|automation|sender)|autoposter"
        r"|mailer|newsletters?)\b"
        r"|\b(?:send|sends|sending|compose|composes|composing|publish|publishes|publishing)\s+(?:\w+\s+){0,3}?"
        r"(?:emails?|messages?|posts?|dms?|notifications?|sms|replies)\b"),
    "money": (
        r"\b(?:payments?|purchases?|checkout|billing|invoic\w+|stripe|paypal|refunds?|subscriptions?"
        r"|conversion tracking|purchase tracking|free trials?)\b"),
    "voice": r"\b(?:voice|audio|earpiece|telephony|webrtc|phone calls?|real-?time)\b",
    "wire": (
        r"\b(?:wire format|payloads?|protocol|api contract|schema|websockets?|grpc|breaking change"
        r"|server and (?:a |the )?client|client and (?:a |the )?server|ios and android"
        r"|(?:several|multiple) (?:systems|clients|services|apps))\b"),
    "harness": (
        r"\b(?:hooks?|harness|model rout\w+|system prompt|brain|pretooluse|agent (?:definition|file|config)"
        r"|live (?:logs?|evidence)|production (?:bug|incident|logs?|outage)|runtime bug|root cause)\b"),
    "concurrency-security": (
        r"\b(?:races?|race conditions?|deadlocks?|locks?|lockfiles?|mutex|schedulers?|cron|concurren\w+"
        r"|thread-?safe|auth|authn|authz|authenticat\w+|authorization|oauth\w*|permissions?|tenants?"
        r"|tenancy|isolation|security|credentials?|encrypt\w*|csrf|xss|sql injection)\b"),
    "shared-state": (
        r"\b(?:shared (?:state|file|database|db|cache|config)|live (?:state|data|database)"
        r"|state files?|migrations?)\b"),
    "repeat-after-failure": (
        r"\b(?:(?:previous|earlier|prior|first) (?:builder|attempt|run|pass|fix)\b[^.\n]{0,60}"
        r"\b(?:failed|broke|introduced|regress\w*)|builder (?:already )?failed|already failed|took over"
        r"|taking over|take over|introduced the (?:defect|bug|regression))\b"),
}
_DEEP_RULES = {
    "new-capability": (
        r"\b(?:new (?:module|package|library|registry|runner|service|cli|subsystem|engine|framework|feature"
        r"|capability|app)|from scratch|small package|full-?stack|clear contract)\b"),
    "several-files": (
        r"\b(?:(?:several|multiple|many|\d+) (?:files|modules|components|screens)"
        r"|across (?:the |all )?(?:\w+ ){0,2}(?:files|modules|packages))\b"),
    "api-db-ui": r"\b(?:api|endpoints?|backend|server)\b[^.]{0,80}\b(?:database|db|sql|tables?)\b[^.]{0,80}"
                 r"\b(?:front-?end|ui|pages?|screens?|client)\b",
}
_OPUS = {name: re.compile(rx, re.I) for name, rx in _OPUS_RULES.items()}
_DEEP = {name: re.compile(rx, re.I) for name, rx in _DEEP_RULES.items()}


def rules_pick(ctx: dict) -> dict:
    """The offline picker: the instructions' rules as keywords. Same three answers as Jev."""
    text = _ETIQUETTE.sub(" ", f"{ctx.get('description', '')}\n{ctx.get('brief', '')}")
    if not text.strip():
        return {"picked": "o55-builder-precise", "matched": ["nothing-to-judge"]}  # unsure: Opus
    for rules, picked in ((_OPUS, "o55-builder-precise"), (_DEEP, "s55-builder-deep")):
        matched = [name for name, rx in rules.items() if rx.search(text)]
        if matched:
            return {"picked": picked, "matched": matched}
    return {"picked": "s55-builder", "matched": []}


# --- the session guard ------------------------------------------------------------------------------

def cli_started(pid: int | None = None) -> float | None:
    """Start time of the Claude Code process this hook runs under: the nearest ancestor whose command
    line names claude. None when it cannot be told (not Linux, no such ancestor)."""
    try:
        boot = next(float(line.split()[1]) for line in open("/proc/stat") if line.startswith("btime "))
        tick = os.sysconf("SC_CLK_TCK")
        pid = pid or os.getppid()
        for _ in range(8):
            if pid <= 1:
                return None
            stat = open(f"/proc/{pid}/stat").read()
            fields = stat[stat.rindex(")") + 2:].split()
            argv = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
            if any(b"claude" in part.rsplit(b"/", 1)[-1] for part in argv[:2]):
                return boot + int(fields[19]) / tick
            pid = int(fields[1])
    except (OSError, ValueError, IndexError, StopIteration):
        return None
    return None


def late_agent(asked: str, picked: str, started: float | None, dirs: list[Path] | None = None) -> bool:
    """True when the running session cannot know the picked agent. A CLI loads its agents once, at
    start: an agent file added later is not offered and the call fails "Agent type not found".

    It only says yes when it can show the session knew the lead's own agent (that file is older than
    the session) and the picked one is missing or newer. Anything it cannot tell is allowed."""
    if started is None:
        return False
    asked_file = guard.find_agent(asked, dirs)
    try:
        if asked_file is None or asked_file.stat().st_mtime > started:
            return False
        picked_file = guard.find_agent(picked, dirs)
        return picked_file is None or picked_file.stat().st_mtime > started
    except OSError:
        return False


# --- the decision -----------------------------------------------------------------------------------

def _describe(exc: BaseException, key: str = "") -> str:
    text = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    return (text.replace(key, "[key]") if key else text)[:160]


def _ask_with_deadline(ctx: dict) -> dict:
    """ask_jev on a daemon thread, so a stuck connect cannot hold the hook past WAIT_S."""
    out: list = []

    def run() -> None:
        try:
            out.append(ask_jev(ctx))
        except Exception as exc:  # no key, provider error, unreadable answer: the rules answer instead
            out.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(WAIT_S)
    got = out[0] if out else TimeoutError("no answer in time")
    if isinstance(got, BaseException):
        raise got
    return got


def pick(tool_input: dict, *, session_id: str = "", tool_use_id: str = "", started: float | None = None,
         dirs: list[Path] | None = None) -> str:
    """The profile the sub-agent runs as. Anything but a builder brief passes through untouched."""
    asked = str(tool_input.get("subagent_type") or "")
    if asked not in BUILDERS:
        return asked
    row = {"at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
           "session_id": session_id, "tool_use_id": tool_use_id, "asked": asked,
           "description": str(tool_input.get("description") or "")[:120]}
    ctx = context(tool_input)
    key = os.environ.get("JEV_API_KEY", "").strip()
    clock = time.monotonic()
    answer, reason = None, ""
    try:
        answer = {"backend": "jev", **_ask_with_deadline(ctx)}
    except Exception as exc:
        reason = _describe(exc, key)
        try:
            answer = {"backend": "rules", **rules_pick(ctx)}
        except Exception as rules_exc:  # the lead's pick stands
            reason += "; rules: " + _describe(rules_exc)
    row["seconds"] = round(time.monotonic() - clock, 3)
    if answer and answer["picked"] != asked and late_agent(asked, answer["picked"], started, dirs):
        reason = (reason + "; " if reason else "") + f"{answer['picked']} is newer than this session"
        answer = None
    if answer is None:
        row.update(backend="lead", picked=asked, fallback=True, reason=reason)
    else:
        row.update(answer, fallback=answer["backend"] != "jev")
        if reason:
            row["reason"] = reason
    log(row)
    return row["picked"]


if __name__ == "__main__":
    # echo '{"subagent_type":"o55-builder","description":"...","prompt":"..."}' | python3 -m router.pick
    import sys
    brief = json.loads(sys.argv[1]) if len(sys.argv) > 1 else json.load(sys.stdin)
    print(pick(brief, session_id="cli", tool_use_id="cli", started=cli_started()))
