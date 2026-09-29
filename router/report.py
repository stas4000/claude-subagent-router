#!/usr/bin/env python3
"""Read the pick ledger and say what the router did.

    python3 router/report.py [ledger.jsonl] [--json]

Picks per profile, the share that went to Sonnet, median seconds a pick, total and per-pick cost, and
how often Jev did not answer (the offline rules, or the lead's own pick, stood in). The ledger defaults
to the hook's own (ROUTER_LEDGER, else .router/picks.jsonl in the project).
"""
from __future__ import annotations

import json
import statistics
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from router import pick  # noqa: E402


def load(path: Path) -> list[dict]:
    """Every readable row; a torn or foreign line is skipped, never fatal."""
    rows = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("picked"), str):
            rows.append(row)
    return rows


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 3) if values else None


def summarize(rows: list[dict]) -> dict:
    total = len(rows)
    by_profile = Counter(row["picked"] for row in rows)
    by_backend = Counter(str(row.get("backend") or "unknown") for row in rows)
    sonnet = sum(n for name, n in by_profile.items() if name.startswith("s55-"))
    seconds = [s for row in rows if (s := _number(row.get("seconds"))) is not None]
    jev_seconds = [s for row in rows if row.get("backend") == "jev" and (s := _number(row.get("seconds"))) is not None]
    cost = sum(c for row in rows if (c := _number(row.get("cost_usd"))) is not None)
    return {
        "picks": total,
        "by_profile": dict(sorted(by_profile.items())),
        "sonnet_pct": round(100 * sonnet / total, 1) if total else None,
        "by_backend": dict(sorted(by_backend.items())),
        "fallbacks": sum(1 for row in rows if row.get("fallback")),
        "median_seconds": _median(seconds),
        "median_seconds_jev": _median(jev_seconds),
        "cost_total_usd": round(cost, 6),
        "cost_per_pick_usd": round(cost / total, 6) if total else None,
    }


def render(s: dict) -> str:
    if not s["picks"]:
        return "no picks in the ledger yet"
    lines = [f"picks: {s['picks']}"]
    lines += [f"  {name:<22} {n:>4}  ({100 * n / s['picks']:.1f}%)" for name, n in s["by_profile"].items()]
    lines.append(f"to Sonnet: {s['sonnet_pct']}%")
    lines.append("answered by: " + ", ".join(f"{name} {n}" for name, n in s["by_backend"].items()))
    lines.append(f"fallbacks (Jev did not answer): {s['fallbacks']}")
    seconds = f"median seconds a pick: {s['median_seconds']}"
    if s["median_seconds_jev"] is not None:
        seconds += f" (Jev only: {s['median_seconds_jev']})"
    lines.append(seconds)
    lines.append(f"cost: ${s['cost_total_usd']:.4f} total, ${s['cost_per_pick_usd']:.5f} a pick")
    return "\n".join(lines)


def main(argv: list[str] | None = None, stdout=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in args
    paths = [a for a in args if a != "--json"]
    summary = summarize(load(Path(paths[0]) if paths else pick.ledger_path()))
    print(json.dumps(summary, indent=2) if as_json else render(summary), file=stdout or sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
