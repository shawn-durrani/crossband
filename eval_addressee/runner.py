"""CLI for the addressee trial (#648).

    .venv/bin/python -m eval_addressee --mock
    .venv/bin/python -m eval_addressee --judge rules --judge jev --judge claude --env ~/dev/crossband/.env

A trial only. It never touches a live chat and decides nothing. Each judge
says, for every made-up moment, who the turn is for, whether the AIs owe a
reply, and which seat it names. The report counts the mistakes that matter:
a turn for the AIs judged not for them (a question left unanswered), a turn
not for them judged for them (a needless round), and a reply forced or
missed."""

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path

from backend import addressee, jev

OWNER = "Alex"
SEATS = ("Claude", "GPT")
ROSTER = ("Alex", "Sam", "Dave", "Mateo")
CASES = Path(__file__).with_name("cases.json")
JUDGES = ("rules", "jev", "claude", "mock")
JEV_PER_M_INPUT = 0.042


def load_cases(path=CASES):
    cases = json.loads(Path(path).read_text())
    ids = [c["id"] for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("case ids must be unique")
    for c in cases:
        e = c["expected"]
        if e["for"] not in addressee.FOR or e["asks"] not in addressee.ASKS \
                or e["seat"] not in {"all", "none", *SEATS}:
            raise ValueError(f"{c['id']}: bad expected {e}")
        for p in c["room"]:
            if p not in ROSTER:
                raise ValueError(f"{c['id']}: {p} isn't on the made-up roster")
    return cases


def state_of(c):
    return addressee.describe(OWNER, list(SEATS), c["room"], c["recent"], c["who"],
                              c["text"])


def _answer(c, got, latency_s, cost=0.0, error=""):
    return {"id": c["id"], "category": c["category"], "expected": c["expected"],
            "got": got, "latency_s": latency_s, "cost_usd": cost, "error": error}


def judge_rules(cases):
    """Today: every turn goes to every seat. A "?" or a named seat means a
    reply is owed (passes.may_pass), and a turn that opens with seat names
    picks those seats (engine._vocative_responders)."""
    from backend import engine
    roster = [{"slug": s.lower(), "name": s} for s in SEATS]
    out = []
    for c in cases:
        named = engine._vocative_responders(c["text"], roster)
        seat = named[0]["name"] if len(named) == 1 else "all"
        asks = "yes" if "?" in c["text"] or named else "no"
        out.append(_answer(c, {"for": "ais", "asks": asks, "seat": seat}, 0.0))
    return out


async def judge_jev(cases, timeout):
    if not jev.api_key():
        return [_answer(c, {}, 0.0, error="no TYPESAFE_API_KEY") for c in cases]
    out = []
    for c in cases:
        got = await jev.ask(state_of(c), addressee.jev_questions(list(SEATS)),
                            timeout_s=timeout)
        cost = (got["usage"].get("input_tokens") or 0) * JEV_PER_M_INPUT / 1_000_000
        out.append(_answer(c, addressee.parse_jev(got["answers"], list(SEATS)),
                           got["ms"] / 1000, cost, got.get("error", "")))
    return out


async def judge_claude(cases, model, timeout):
    from backend.config import Settings
    from backend.llm_util import price_utility_call, utility_complete_with_usage
    cfg = Settings().as_cfg()
    out = []
    for c in cases:
        done = await utility_complete_with_usage(
            addressee.claude_prompt(state_of(c), list(SEATS)), cfg, max_tokens=60,
            model=model, timeout=timeout)
        cost = (price_utility_call(model, done, cfg)[0] or 0.0) if done.text else 0.0
        out.append(_answer(c, addressee.parse_claude(done.text, list(SEATS)),
                           done.latency_s, cost, "" if done.text else "no answer"))
    return out


def judge_mock(cases):
    return [_answer(c, dict(c["expected"]), 0.0) for c in cases]


def summarise(answers):
    n = len(answers)
    def right(a, k):
        return a["got"].get(k) == a["expected"][k]
    for_ais = [a for a in answers if a["expected"]["for"] == "ais"]
    not_ais = [a for a in answers if a["expected"]["for"] != "ais"]
    owed = [a for a in answers if a["expected"]["asks"] == "yes"]
    not_owed = [a for a in answers if a["expected"]["asks"] == "no"]
    lat = sorted(a["latency_s"] for a in answers if not a["error"])
    by_cat = {}
    for a in answers:
        got = by_cat.setdefault(a["category"], [0, 0])
        got[0] += all(right(a, k) for k in ("for", "asks", "seat"))
        got[1] += 1
    return {
        "cases": n,
        "all_right": sum(all(right(a, k) for k in ("for", "asks", "seat")) for a in answers),
        "for_right": sum(right(a, "for") for a in answers),
        "asks_right": sum(right(a, "asks") for a in answers),
        "seat_right": sum(right(a, "seat") for a in answers),
        "wrong_skips": sum(a["got"].get("for") != "ais" for a in for_ais),
        "of_for_ais": len(for_ais),
        "needless_rounds": sum(a["got"].get("for") == "ais" for a in not_ais),
        "of_not_ais": len(not_ais),
        "forced_replies": sum(a["got"].get("asks") == "yes" for a in not_owed),
        "of_not_owed": len(not_owed),
        "missed_replies": sum(a["got"].get("asks") != "yes" for a in owed),
        "of_owed": len(owed),
        "errors": sum(bool(a["error"]) for a in answers),
        "p50_ms": round(statistics.median(lat) * 1000) if lat else None,
        "p95_ms": round(lat[min(len(lat) - 1, int(len(lat) * 0.95))] * 1000) if lat else None,
        "cost_usd": round(sum(a["cost_usd"] for a in answers), 5),
        "by_category": {k: f"{v[0]}/{v[1]}" for k, v in sorted(by_cat.items())},
        "wrong": [{"id": a["id"], "expected": a["expected"], "got": a["got"] or a["error"]}
                  for a in answers
                  if not all(right(a, k) for k in ("for", "asks", "seat"))],
    }


def render(report):
    lines = ["# Is a turn for the AIs, and which of them?", "",
             "| judge | all right | who for | reply owed | seat | wrong skips "
             "| needless rounds | forced replies | missed replies | p50 ms | cost |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, s in report.items():
        lines.append(
            f"| {name} | {s['all_right']}/{s['cases']} | {s['for_right']} | {s['asks_right']} "
            f"| {s['seat_right']} | {s['wrong_skips']}/{s['of_for_ais']} "
            f"| {s['needless_rounds']}/{s['of_not_ais']} | {s['forced_replies']}/{s['of_not_owed']} "
            f"| {s['missed_replies']}/{s['of_owed']} | {s['p50_ms']} | ${s['cost_usd']:.4f} |")
    for name, s in report.items():
        lines += ["", f"## {name}", ""]
        if s["errors"]:
            lines.append(f"Errors: {s['errors']} of {s['cases']}.")
        lines.append("By kind: " + ", ".join(f"{k} {v}" for k, v in s["by_category"].items()) + ".")
        for w in s["wrong"]:
            lines.append(f"- {w['id']}: wanted {w['expected']}, got {w['got']}")
    return "\n".join(lines) + "\n"


async def run(args):
    cases = load_cases(args.cases)
    judges = ["mock"] if args.mock else (args.judges or ["rules"])
    report = {}
    for j in judges:
        if j == "rules":
            answers = judge_rules(cases)
        elif j == "jev":
            answers = await judge_jev(cases, args.timeout_s)
        elif j == "claude":
            answers = await judge_claude(cases, args.claude_model, args.timeout_s)
        else:
            answers = judge_mock(cases)
        report[j] = summarise(answers)
    return report


def build_arg_parser():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--judge", action="append", dest="judges", choices=JUDGES)
    p.add_argument("--cases", default=str(CASES))
    p.add_argument("--claude-model", default="claude-haiku-4-5")
    p.add_argument("--timeout-s", type=float, default=30.0)
    p.add_argument("--env", help="a .env file to take the keys from, for this run alone")
    p.add_argument("--mock", action="store_true",
                   help="keyless stand-in; for checking the harness, never for numbers")
    p.add_argument("--format", choices=("markdown", "json"), default="markdown")
    p.add_argument("--out")
    p.add_argument("--json-out")
    return p


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    if args.env:
        from dotenv import load_dotenv
        path = os.path.expanduser(args.env)
        if not os.path.isfile(path):
            raise SystemExit(f"--env file not found: {path}")
        load_dotenv(path, override=True)
    report = asyncio.run(run(args))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=2))
    out = json.dumps(report, indent=2) if args.format == "json" else render(report)
    if args.out:
        Path(args.out).write_text(out)
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(out)
    return 0
