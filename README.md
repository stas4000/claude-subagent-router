# claude-subagent-router

A Claude Code sub-agent setup where an Opus 5.5 lead hands sub-tasks to Sonnet 5.5 or Opus 5.5 builders by effort level. A PreToolUse hook picks the builder for each sub-task. A Jev classifier makes the pick, and an offline rule-based picker answers when Jev is not available. A hard guard makes sure Sonnet never runs at max effort.

It is the setup we run ourselves, packaged so you can copy it. Python 3.10+, standard library only.

## Why

We ran every sub-agent on Opus 5.5, because we never compromise on quality. Then we measured Sonnet 5.5 at each effort level on real coding tasks, and added Sonnet where it is just as good: Sonnet 5.5 on high and extra high passed the same hidden tests as max, on a fraction of the output tokens (numbers in [Measured](#measured)). Work that touches shared state, money, auth, sends or a wire format stays on Opus.

## How it fits together

```
              lead (Opus 5.5)
                   |  Agent call: a builder name plus the brief
                   v
        PreToolUse hook   router/hook.py
                   |  asks router/pick.py
                   v
         Jev classifier   (offline rules when Jev is off or down)
                   |  rewrites subagent_type; router/guard.py checks the result
      +------------+-----------------+
      v            v                 v
 s55-builder   s55-builder-deep   o55-builder-precise
 Sonnet high   Sonnet xhigh       Opus xhigh
      +------------+-----------------+
                   v
             s55-verifier    Sonnet medium, may add tests, never edits implementation
                   v
             o55-reviewer    Opus high, read-only, the final gate
```

`s55-scout` (Sonnet, low, read-only) sits outside the chain: the lead uses it to find code instead of reading many files itself.

| Agent | Model | Effort | Job |
| --- | --- | --- | --- |
| `s55-builder` | claude-sonnet-5-5 | high | bounded work that follows an existing pattern |
| `s55-builder-deep` | claude-sonnet-5-5 | xhigh | well specified work across several files of one subsystem |
| `o55-builder-precise` | claude-opus-5-5 | xhigh | shared state, money, auth, sends, wire formats, repeats after failure |
| `s55-verifier` | claude-sonnet-5-5 | medium | checks the brief's acceptance clauses, adds tests |
| `o55-reviewer` | claude-opus-5-5 | high | final gate, read-only, PASS or FAIL with exact fixes |
| `s55-scout` | claude-sonnet-5-5 | low | read-only search |

All of them follow the same working rules: the brief is the contract, run the acceptance check, write the test red first, no destructive git, no deploy or send unless the brief names it. The verifier never edits implementation code and the reviewer never edits anything.

## Install

1. Copy `.claude/agents/` and `router/` into your project (or clone this repo and work in it). Restart Claude Code: a session loads its agents once, at start.
2. Merge the `hooks` block of `.claude/settings.example.json` into your `.claude/settings.json`. It wires `router/hook.py` on the `Agent` tool and `router/verifier_guard.py` on the edit tools.
3. Optional: `export JEV_API_KEY` with your key for the Jev System One endpoint. Without it the offline picker answers.

Then tell your lead to hand build work to a builder and let the router choose. In your `CLAUDE.md`:

```
Hand build sub-tasks to o55-builder-precise. A router may move the brief to s55-builder or
s55-builder-deep. After each build run s55-verifier, then o55-reviewer as the final gate.
Use s55-scout to find code.
```

## How the pick works

1. Only builder briefs are routed (`s55-builder`, `s55-builder-deep`, `o55-builder-precise`, and `o55-builder`, an Opus medium builder a lead may still name). A builder brief can become another builder, never a reviewer or verifier. Everything else passes through untouched.
2. The picker sees the short description and the first 1,000 characters of the brief.
3. **Jev.** With `JEV_API_KEY` set, `router/pick.py` sends one HTTPS request to `https://api.typesafe.ai/v1/systemone`: the context, plus one multiple-choice question with the three profiles and the routing instructions (the `INSTRUCTIONS` text in `router/pick.py`). Jev is a System One model: it returns a probability per choice and no text. The highest wins. The call gets 12 seconds. The key goes only to an `https` URL and redirects are never followed.
4. **Offline rules.** When the key is unset, or Jev times out, errors or returns something unreadable, `rules_pick` answers. It uses the same three choices and the same risk rules as the instructions, as keywords, after dropping etiquette such as "do not restart" and "never send anything", which is not risk. Any risk keyword means Opus, a new module or a change across several files means the deep builder, everything else means the high builder, and an empty brief means Opus. It is deliberately broad: when unsure it picks Opus. It is a keyword approximation of the routing rules, not the trained picker, and it is not measured here.
5. **The lead's pick stands** when neither backend answers, or when the pick is an agent the running session cannot know (an agent file added after Claude Code started fails with "Agent type not found").
6. The guard checks the agent that will run (next section).
7. The call is rewritten to run in the foreground. A lead that hands work to a background sub-agent and then ends its turn loses that work: in print mode the sub-agent's notification arrives after the lead's final result and never wakes it. Parallel Agent calls in one message still run concurrently. Set `ROUTER_FOREGROUND=0` to turn this off.
8. One line goes to the ledger, `.router/picks.jsonl` under `$CLAUDE_PROJECT_DIR` (else under this repo; override with `ROUTER_LEDGER`): time, session, tool call, the agent asked for, the agent picked, which backend answered (`jev`, `rules` or `lead`), probability, seconds, cost, and why Jev did not answer. It holds the call's short `description` but never the brief text.

The hook prints a PreToolUse decision (`hookSpecificOutput` with `permissionDecision`, `permissionDecisionReason` and, for an allow, `updatedInput`) or nothing when the call needs no change. It never fails a tool call: malformed input exits 0 with no output.

What the routing instructions ask for:

- **Opus (`o55-builder-precise`)** when the change builds or changes an engine that composes, sends or publishes messages or posts for someone; touches money or its measurement; touches real-time voice, audio or calls; connects a server and a client or changes a wire format; changes a model route, harness or hook, or diagnoses a runtime bug from live evidence; fixes races, locks, schedulers or concurrency, or enforces security, auth, permissions or tenant isolation; or takes over after a builder failed.
- **Sonnet extra high (`s55-builder-deep`)** for a well specified new capability across several files: a new module or small package with a clear contract, or a full-stack feature inside one app.
- **Sonnet high (`s55-builder`)** for clearly defined, bounded work: one file or screen, a UI change that follows a pattern, review fixes already written out, a small script, a doc or test update.
- When unsure between Sonnet and Opus, Opus. When unsure between the two Sonnet builders, the deep one.

## Rule: Sonnet never runs at max

`router/guard.py` denies an Agent call when the agent that would run is Sonnet and its file says `effort: max`, names no effort at all (it would inherit the lead's, which may be max), or names an effort that is not `low`, `medium`, `high` or `xhigh`. The reason goes back to the lead. Opus agents are never refused, at any effort.

- It keys on the model, not the name: any agent whose `model:` line names Sonnet is guarded, and an `s55-` agent with no `model:` line is treated as Sonnet.
- It checks the agent after the router's pick, and it checks agents the router does not route (the verifier, the scout, your own).
- A `model` override on the Agent call counts: `model: sonnet` on an Opus agent at max is denied, and so is a Sonnet override with no agent file to name an effort.
- Agent files are looked up in `ROUTER_AGENTS_DIR` if set, else `$CLAUDE_PROJECT_DIR/.claude/agents`, this repo's `.claude/agents`, then `~/.claude/agents`.

It reads the agent file, so it cannot see an effort set somewhere else at run time (a settings file or environment override for the whole session). Keep max out of those too.

## Report

```
python3 router/report.py            # reads the default ledger (or ROUTER_LEDGER)
python3 router/report.py path.jsonl # another ledger
python3 router/report.py --json     # the same numbers as JSON
```

It prints the picks per profile, the share that went to Sonnet, who answered (Jev, the rules, or the lead's own pick standing), how many picks were fallbacks, the median seconds a pick (and for Jev alone), and the total and per-pick cost. Cost is the input tokens Jev reports at `JEV_PRICE_PER_MTOK` (default 0.042 USD per million tokens), or the price in the reply when there is one. The offline rules cost nothing.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `JEV_API_KEY` | unset | key for the Jev endpoint; unset means the offline rules answer |
| `JEV_URL` | `https://api.typesafe.ai/v1/systemone` | must be `https` |
| `JEV_MODEL` | `jev-latest` | model name sent to Jev |
| `JEV_PRICE_PER_MTOK` | `0.042` | USD per million input tokens, for the cost column |
| `ROUTER_LEDGER` | `$CLAUDE_PROJECT_DIR/.router/picks.jsonl` | where the pick ledger is written |
| `ROUTER_AGENTS_DIR` | unset | look for agent files only here |
| `ROUTER_FOREGROUND` | on | `0` leaves `run_in_background` alone |

## Tests

```
python3 -m unittest discover -s tests -v
```

No test touches the network: the HTTP call is replaced in every test that could make one.

## Measured

All numbers below are ours, from 29 Sept 2026 or earlier as labeled.

**Effort test.** Source: our own runs through the Claude CLI, 29 Sept 2026. 72 runs on 3 coding tasks (an HTML pricing section, interval merge, an expression parser), with Sonnet 5.5, Opus 5.5 and Sonnet 5 each at medium, high, xhigh and max, 2 runs each, scored by hidden tests. Sonnet 5.5:

| Effort | Hidden tests passed | Output tokens per task |
| --- | --- | --- |
| medium | 39/40 | 1.2k |
| high | 40/40 | 2.0k |
| xhigh | 40/40 | 3.1k |
| max | 40/40 | 23k |

At $10 per million output tokens, high is about $0.02 of output per task and max is about $0.23. Short tasks: a direction, not a leaderboard.

**Picker.** Source: our own labeled set, 29 Sept 2026. Trained on 36 real labeled sub-task briefs. The best version scored 0.95 on the training briefs and 0.71 on the held-out ones. When unsure it picks Opus (quality first). The labeled briefs are private and not in this repo; the instructions the picker runs on are.

**Live.** Source: our own pick ledger, since 27 Sept 2026. 92 picks. Jev answered 91, and 1 fell back to the lead's pick. Median 0.44 s a pick, $0.0037 in total, about $0.00004 a pick. (In our own setup a failed Jev call keeps the lead's pick. This repo adds the offline rules in between.)

## License

MIT, see [LICENSE](LICENSE).

Built with Claude Opus 5.5.
