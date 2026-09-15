"""CLI for the spoken intent harness.

    .venv/bin/python -m eval_intent --mock
    .venv/bin/python -m eval_intent --model claude-haiku-4-5
    .venv/bin/python -m eval_intent --strategy merged --fixtures-dir /path/outside/git

A harness only. It never touches the live scan and decides nothing; it
produces the numbers #258 needs: how many instruction turns today's phrase
lists drop, and whether one merged call hears more than four gated ones."""

import argparse
import asyncio
import json
import sys

from backend.config import Settings
from backend.llm_util import price_utility_call, utility_complete_with_usage
from eval_intent.fixtures_loader import load_fixtures
from eval_intent.parse import parse_merged
from eval_intent.prompt import build_merged_prompt
from eval_intent.report import render_markdown
from eval_intent.scoring import STRATEGIES, Result, aggregate
from eval_intent.today import merge_today, silent_misses, today_prompts


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", default="claude-haiku-4-5",
                   help="the utility model to judge with; default claude-haiku-4-5")
    p.add_argument("--strategy", action="append", dest="strategies",
                   choices=STRATEGIES, help="today, merged, or both (default)")
    p.add_argument("--fixtures-dir", action="append", dest="fixtures_dirs", default=[])
    p.add_argument("--no-builtin-fixtures", action="store_true")
    p.add_argument("--timeout-s", type=float, default=30.0)
    p.add_argument("--mock", action="store_true",
                   help="keyless stand-ins; for checking the harness, never for numbers")
    p.add_argument("--format", choices=("markdown", "json"), default="markdown")
    p.add_argument("--out")
    return p


async def run_one(fx, strategy: str, caller, cfg, model: str) -> Result:
    if strategy == "merged":
        done = await caller(build_merged_prompt(fx), "merged")
        cost = price_utility_call(model, done, cfg)[0] if done.text is not None else 0.0
        return Result(fixture=fx, strategy=strategy,
                      heard=parse_merged(done.text),
                      replies={"merged": done.text or ""}, cost_usd=cost,
                      latency_s=done.latency_s, calls=1,
                      missing_key=done.text is None and not done.timed_out,
                      timed_out=done.timed_out)
    replies, cost, latency, calls = {}, 0.0, 0.0, 0
    missing = timed_out = False
    for axis, prompt in today_prompts(fx).items():
        done = await caller(prompt, axis)
        calls += 1
        latency += done.latency_s
        if done.text is None:
            missing = missing or not done.timed_out
            timed_out = timed_out or done.timed_out
            continue
        replies[axis] = done.text
        cost += price_utility_call(model, done, cfg)[0]
    return Result(fixture=fx, strategy=strategy, heard=merge_today(replies),
                  replies=replies, cost_usd=cost, latency_s=latency, calls=calls,
                  missing_key=missing, timed_out=timed_out)


async def run(args, caller=None) -> dict:
    fixtures = load_fixtures(dirs=args.fixtures_dirs,
                             include_builtin=not args.no_builtin_fixtures)
    cfg = Settings().as_cfg()
    strategies = args.strategies or list(STRATEGIES)
    if args.mock:
        from eval_intent.mock import MockCaller
        caller = caller or MockCaller(fixtures)
    elif caller is None:
        async def caller(prompt, axis):
            return await utility_complete_with_usage(
                prompt, cfg, max_tokens=300, model=args.model, timeout=args.timeout_s)
    results = []
    for strategy in strategies:
        for fx in fixtures:
            results.append(await run_one(fx, strategy, caller, cfg, args.model))
    silent = {fx.id: silent_misses(fx) for fx in fixtures}
    return aggregate(results, fixtures, silent)


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    report = asyncio.run(run(args))
    out = (json.dumps(report, indent=2, default=str) if args.format == "json"
           else render_markdown(report, args.mock))
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(out)
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(out)
    return 0
