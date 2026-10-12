"""CLI for the spoken intent harness.

    .venv/bin/python -m eval_intent --mock
    .venv/bin/python -m eval_intent --model claude-haiku-4-5
    .venv/bin/python -m eval_intent --strategy merged --fixtures-dir /path/outside/git
    .venv/bin/python -m eval_intent --env ~/dev/crossband/.env   # from a guest worktree

A harness only. It never touches the live scan and decides nothing; it
produces the numbers #258 needs: how many instruction turns today's phrase
lists drop, and whether one merged call hears more than four gated ones."""

import argparse
import asyncio
import json
import os
import sys

from dotenv import load_dotenv

from backend.config import Settings
from backend import jev
from backend.intent import (build_jev_check, build_merged_prompt, parse_jev_check,
                            parse_merged)
from backend.llm_util import price_utility_call, utility_complete_with_usage
from eval_intent.fixtures_loader import load_fixtures
from eval_intent.report import render_markdown
from eval_intent.scoring import DEFAULT_STRATEGIES, STRATEGIES, Result, aggregate
from eval_intent.today import merge_today, silent_misses, today_prompts


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", default="claude-haiku-4-5",
                   help="the utility model to judge with; default claude-haiku-4-5")
    p.add_argument("--strategy", action="append", dest="strategies",
                   choices=STRATEGIES, help="today, merged (both by default), or jev, "
                                            "which needs TYPESAFE_API_KEY")
    p.add_argument("--fixtures-dir", action="append", dest="fixtures_dirs", default=[])
    p.add_argument("--no-builtin-fixtures", action="store_true")
    p.add_argument("--timeout-s", type=float, default=30.0)
    p.add_argument("--env", help="a .env file to take the provider keys from, for a "
                                 "run where the process has none (a guest worktree)")
    p.add_argument("--mock", action="store_true",
                   help="keyless stand-ins; for checking the harness, never for numbers")
    p.add_argument("--format", choices=("markdown", "json"), default="markdown")
    p.add_argument("--out")
    p.add_argument("--json-out", help="also write the whole report as JSON here")
    return p


# Jev's price per million input tokens, from TypeSafe's models page (output
# is free), for the report's cost per turn.
JEV_PER_M_INPUT = 0.042


async def run_jev(fx, caller, jev_caller, cfg, model: str) -> Result:
    """#646: Jev answers every axis it can in one request. A turn it says
    names or renames a person goes on to the merged prompt, whose names are
    used. Time and cost add up both calls."""
    state, questions = build_jev_check(fx.text, fx.user_name, fx.seats,
                                       fx.present, fx.known, asking=fx.asking)
    got = await jev_caller(state, questions)
    heard, needs_names = parse_jev_check(got["answers"], fx.text)
    cost = (got["usage"].get("input_tokens") or 0) * JEV_PER_M_INPUT / 1_000_000
    latency, calls = got["ms"] / 1000, 1
    replies = {"jev": json.dumps(got["answers"])}
    missing = bool(got.get("error")) and not jev.api_key()
    if needs_names:
        prompt = build_merged_prompt(fx.text, fx.user_name, fx.seats,
                                     fx.present, fx.known, asking=fx.asking)
        done = await caller(prompt, "merged")
        names = parse_merged(done.text, fx.text)
        for axis in ("introductions", "departures", "aliases", "corrections"):
            heard[axis] = names[axis]
        if done.text is not None:
            cost += price_utility_call(model, done, cfg)[0] or 0.0
        latency += done.latency_s
        calls += 1
        replies["merged"] = done.text or ""
    return Result(fixture=fx, strategy="jev", heard=heard, replies=replies,
                  cost_usd=cost, latency_s=latency, calls=calls,
                  missing_key=missing, timed_out=got.get("error") == "ReadTimeout")


async def run_one(fx, strategy: str, caller, cfg, model: str, jev_caller=None) -> Result:
    if strategy == "jev":
        return await run_jev(fx, caller, jev_caller, cfg, model)
    if strategy == "merged":
        prompt = build_merged_prompt(fx.text, fx.user_name, fx.seats,
                                     fx.present, fx.known, asking=fx.asking)
        done = await caller(prompt, "merged")
        cost = price_utility_call(model, done, cfg)[0] if done.text is not None else 0.0
        # The turn goes to the parser too, for the live scan's rule on words
        # spelt out letter by letter (#494).
        return Result(fixture=fx, strategy=strategy,
                      heard=parse_merged(done.text, fx.text),
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
    strategies = args.strategies or list(DEFAULT_STRATEGIES)
    jev_caller = None
    if args.mock:
        from eval_intent.mock import MockCaller, MockJev
        caller = caller or MockCaller(fixtures)
        jev_caller = MockJev(fixtures)
    elif caller is None:
        async def caller(prompt, axis):
            return await utility_complete_with_usage(
                prompt, cfg, max_tokens=300, model=args.model, timeout=args.timeout_s)
    if jev_caller is None:
        async def jev_caller(state, questions):
            return await jev.ask(state, questions, timeout_s=args.timeout_s)
    results = []
    for strategy in strategies:
        for fx in fixtures:
            results.append(await run_one(fx, strategy, caller, cfg, args.model,
                                         jev_caller=jev_caller))
    silent = {fx.id: silent_misses(fx) for fx in fixtures}
    return aggregate(results, fixtures, silent)


def load_env(path: str) -> None:
    """Take the provider keys from the app's own .env. A summoned guest starts
    with every ANTHROPIC and CLAUDE variable blanked so its own turns bill
    the Mac's Claude Code login and never the metered key; a blank counts as
    set, so the file has to override. Only this process sees the keys."""
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        raise SystemExit(f"--env file not found: {path}")
    load_dotenv(path, override=True)


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.env:
        load_env(args.env)
    report = asyncio.run(run(args))
    if args.json_out:
        with open(args.json_out, "w") as fh:
            fh.write(json.dumps(report, indent=2, default=str))
    out = (json.dumps(report, indent=2, default=str) if args.format == "json"
           else render_markdown(report, args.mock))
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(out)
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(out)
    return 0
