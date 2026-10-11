"""CLI for the judge trial (#640).

    .venv/bin/python -m eval_bargein --mock
    .venv/bin/python -m eval_bargein --judge rules --judge claude --env ~/dev/crossband/.env
    .venv/bin/python -m eval_bargein --judge local --local-url http://127.0.0.1:8131/v1 \\
        --local-model mlx-community/Qwen3.5-4B-OptiQ-4bit
    .venv/bin/python -m eval_bargein --judge jev --env ~/dev/crossband/.env

A trial only. It never touches a live chat and decides nothing: every judge
answers the same made-up moments, and the report says how often each was
right, how often it would cut a reply wrongly or miss a real interruption,
and how long it took."""

import argparse
import asyncio
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

from eval_bargein.judge import (CASES, OWNER, ROSTER, SEATS, jev_body,
                                load_cases, parse_jev, parse_label, prompt)

JUDGES = ("rules", "claude", "local", "jev", "mock")
JEV_URL = "https://api.typesafe.ai/v1/systemone"
RULES = Path(__file__).with_name("rules.mjs")


def _answer(case, label, latency_s, cost=0.0, confidence=None, error=""):
    return {"id": case.id, "category": case.category, "expected": case.expected,
            "label": label, "latency_s": latency_s, "cost_usd": cost,
            "confidence": confidence, "error": error}


def judge_rules(cases):
    """Today's word rules. They read the words alone: a stop word, a seat's
    or a person's name, a question just asked, how many words."""
    node = shutil.which("node")
    if not node:
        return [_answer(c, "", 0.0, error="node not found") for c in cases]
    payload = {"seats": list(SEATS), "people": [p for p in ROSTER if p != OWNER],
               "cases": [{"id": c.id, "words": c.words, "ai_said": c.ai_said} for c in cases]}
    out = subprocess.run([node, str(RULES)], input=json.dumps(payload), text=True,
                         capture_output=True, check=True, cwd=RULES.parent.parent)
    verdicts = {v["id"]: v for v in json.loads(out.stdout)}
    as_label = {"cut": "interrupt", "listen": "listening"}
    out = []
    for c in cases:
        v = verdicts[c.id]
        a = _answer(c, as_label.get(v["verdict"], "aside"), v["ms"] / 1000)
        # A cut on word count alone is the unclear middle (#640).
        a["unclear"] = v["verdict"] == "cut" and v["why"] == "words"
        out.append(a)
    return out


def with_rules(rules, judged):
    """The rules first, and the judge only where they cut on word count
    alone: how a judge would sit in the app. The judge's time and cost
    count only for the moments it was asked about."""
    by_id = {a["id"]: a for a in judged}
    out = []
    for r in rules:
        if r.get("unclear"):
            out.append(dict(by_id[r["id"]]))
        else:
            out.append({**r, "latency_s": 0.0})
    return out


async def judge_claude(cases, model, timeout):
    from backend.config import Settings
    from backend.llm_util import price_utility_call, utility_complete_with_usage
    cfg = Settings().as_cfg()
    out = []
    for c in cases:
        done = await utility_complete_with_usage(prompt(c), cfg, max_tokens=8,
                                                 model=model, timeout=timeout)
        cost = price_utility_call(model, done, cfg)[0] if done.text is not None else 0.0
        out.append(_answer(c, parse_label(done.text), done.latency_s, cost,
                           error="" if done.text is not None else "no answer"))
    return out


async def judge_local(cases, url, model, timeout):
    """Any OpenAI-compatible server on this Mac, like mlx_lm.server."""
    import httpx
    out = []
    async with httpx.AsyncClient(timeout=timeout) as client:
        for c in cases:
            t0 = time.monotonic()
            try:
                r = await client.post(url.rstrip("/") + "/chat/completions", json={
                    "model": model, "temperature": 0, "max_tokens": 64,
                    "messages": [{"role": "user", "content": prompt(c)}],
                    "chat_template_kwargs": {"enable_thinking": False}})
                r.raise_for_status()
                text = r.json()["choices"][0]["message"]["content"]
                out.append(_answer(c, parse_label(text), time.monotonic() - t0))
            except Exception as e:  # a trial reports a failure, it never stops on one
                out.append(_answer(c, "", time.monotonic() - t0, error=type(e).__name__))
    return out


async def judge_jev(cases, timeout):
    import httpx
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not key:
        return [_answer(c, "", 0.0, error="no TYPESAFE_API_KEY") for c in cases]
    out = []
    async with httpx.AsyncClient(timeout=timeout) as client:
        for c in cases:
            t0 = time.monotonic()
            try:
                r = await client.post(JEV_URL, json=jev_body(c),
                                      headers={"Authorization": f"Bearer {key}"})
                r.raise_for_status()
                label, conf = parse_jev(r.json())
                out.append(_answer(c, label, time.monotonic() - t0, confidence=conf))
            except Exception as e:
                out.append(_answer(c, "", time.monotonic() - t0, error=type(e).__name__))
    return out


def judge_mock(cases):
    """Keyless and always right: for checking the harness, never for numbers."""
    return [_answer(c, c.expected, 0.0) for c in cases]


def summarise(answers) -> dict:
    """How one judge did. A cut is an 'interrupt'; anything else carries on."""
    n = len(answers)
    right = sum(a["label"] == a["expected"] for a in answers)
    should_cut = [a for a in answers if a["expected"] == "interrupt"]
    should_not = [a for a in answers if a["expected"] != "interrupt"]
    false_cuts = sum(a["label"] == "interrupt" for a in should_not)
    missed = sum(a["label"] != "interrupt" for a in should_cut)
    lat = sorted(a["latency_s"] for a in answers if not a["error"])
    by_cat = {}
    for a in answers:
        got = by_cat.setdefault(a["category"], [0, 0])
        got[0] += a["label"] == a["expected"]
        got[1] += 1
    return {
        "cases": n, "right": right,
        "cut_right": n - false_cuts - missed,
        "false_cuts": false_cuts, "of_not_cut": len(should_not),
        "missed_cuts": missed, "of_cut": len(should_cut),
        "errors": sum(bool(a["error"]) for a in answers),
        "p50_ms": round(statistics.median(lat) * 1000) if lat else None,
        "p95_ms": round(lat[min(len(lat) - 1, int(len(lat) * 0.95))] * 1000) if lat else None,
        "cost_usd": round(sum(a["cost_usd"] for a in answers), 5),
        "by_category": {k: f"{v[0]}/{v[1]}" for k, v in sorted(by_cat.items())},
        "wrong": [{"id": a["id"], "expected": a["expected"], "got": a["label"] or a["error"]}
                  for a in answers if a["label"] != a["expected"]],
    }


def render(report: dict) -> str:
    lines = ["# Judge trial for unclear interruptions", "",
             "\"rules, then X\" asks judge X only where the word rules cut on "
             "word count alone, as it would sit in the app.", "",
             "| judge | right | cut or carry on right | wrong cuts | missed cuts "
             "| p50 ms | p95 ms | cost |",
             "|---|---|---|---|---|---|---|---|"]
    for name, s in report.items():
        lines.append(f"| {name} | {s['right']}/{s['cases']} | {s['cut_right']}/{s['cases']} "
                     f"| {s['false_cuts']}/{s['of_not_cut']} | {s['missed_cuts']}/{s['of_cut']} "
                     f"| {s['p50_ms']} | {s['p95_ms']} | ${s['cost_usd']:.4f} |")
    for name, s in report.items():
        lines += ["", f"## {name}", ""]
        if s["errors"]:
            lines.append(f"Errors: {s['errors']} of {s['cases']}.")
        lines.append("By kind of moment: " + ", ".join(
            f"{k} {v}" for k, v in s["by_category"].items()) + ".")
        for w in s["wrong"]:
            lines.append(f"- {w['id']}: wanted {w['expected']}, got {w['got']}")
    return "\n".join(lines) + "\n"


async def run(args) -> dict:
    cases = load_cases(args.cases)
    judges = ["mock"] if args.mock else (args.judges or ["rules"])
    report = {}
    rules = judge_rules(cases)
    for j in judges:
        if j == "rules":
            answers = rules
        elif j == "claude":
            answers = await judge_claude(cases, args.claude_model, args.timeout_s)
        elif j == "local":
            answers = await judge_local(cases, args.local_url, args.local_model, args.timeout_s)
        elif j == "jev":
            answers = await judge_jev(cases, args.timeout_s)
        else:
            answers = judge_mock(cases)
        name = j if j != "local" else f"local ({args.local_model})"
        report[name] = summarise(answers)
        if j not in ("rules", "mock") and rules[0]["error"] == "":
            report[f"rules, then {name}"] = summarise(with_rules(rules, answers))
            report[f"rules, then {name}"]["asked"] = sum(bool(r.get("unclear")) for r in rules)
    return report


def build_arg_parser():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--judge", action="append", dest="judges", choices=JUDGES)
    p.add_argument("--cases", default=str(CASES))
    p.add_argument("--claude-model", default="claude-haiku-4-5")
    p.add_argument("--local-url", default="http://127.0.0.1:8131/v1")
    p.add_argument("--local-model", default="mlx-community/Qwen3.5-4B-OptiQ-4bit")
    p.add_argument("--timeout-s", type=float, default=30.0)
    p.add_argument("--env", help="a .env file to take the keys from, for this run alone")
    p.add_argument("--mock", action="store_true",
                   help="keyless stand-in; for checking the harness, never for numbers")
    p.add_argument("--format", choices=("markdown", "json"), default="markdown")
    p.add_argument("--out")
    p.add_argument("--json-out", help="also write the whole report as JSON here")
    return p


def main(argv=None) -> int:
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
