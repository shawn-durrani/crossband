"""CLI entrypoint for the offline critic eval harness.

    .venv/bin/python -m eval_critic.runner --mock
    .venv/bin/python -m eval_critic.runner --model claude-haiku-4-5
    .venv/bin/python -m eval_critic.runner --model claude-haiku-4-5 \\
        --fixtures-dir /path/outside/git/private-replay --failure-policy fail_closed

This is a harness only — it never touches backend/engine.py and makes no
decision by itself. It just produces the numbers the decision gate needs.
"""

import argparse
import asyncio
import functools
import json
import sys

from backend.config import Settings
from eval_critic.critic import CriticCallFn, evaluate_fixture
from eval_critic.fixtures_loader import load_fixtures
from eval_critic.mock import mock_call
from eval_critic.report import render_markdown
from eval_critic.schema import FAILURE_POLICIES
from eval_critic.scoring import aggregate


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", action="append", dest="models",
                   help="critic model to evaluate (repeatable). Default: "
                        "cfg utility_model (claude-haiku-4-5) — the cheapest low-"
                        "latency option.")
    p.add_argument("--fixtures-dir", action="append", dest="fixtures_dirs", default=[],
                   help="extra directory of *.json fixtures (repeatable) — use this "
                        "for a private, non-Git replay set kept outside the repo.")
    p.add_argument("--no-builtin-fixtures", action="store_true",
                   help="skip the committed synthetic seed corpus (eval_critic/fixtures/); "
                        "run only --fixtures-dir set(s).")
    p.add_argument("--failure-policy", choices=FAILURE_POLICIES, default="fail_open",
                   help="what a timed-out/malformed critic call resolves to for scoring "
                        "(default: fail_open).")
    p.add_argument("--timeout-s", type=float, default=8.0,
                   help="hard per-call timeout (seconds) passed to the model call.")
    p.add_argument("--latency-budget-s", type=float, default=2.5,
                   help="soft budget used only to measure how often a fixed budget "
                        "would force fail-open under realistic latency.")
    p.add_argument("--recall-target", type=float, default=0.95,
                   help="informational: proposed unsafe-draft recall target (not a "
                        "self-authorising gate: a human decides).")
    p.add_argument("--false-alarm-target", type=float, default=0.10)
    p.add_argument("--projected-volume", type=int, default=1000,
                   help="turns to project aggregate cost over.")
    p.add_argument("--mock", action="store_true",
                   help="use a deterministic keyless stand-in critic instead of a real "
                        "model call — for smoke-testing the harness itself, not for "
                        "numbers you'd cite in the decision gate.")
    p.add_argument("--format", choices=("markdown", "json"), default="markdown")
    p.add_argument("--out", help="write the report to this path instead of stdout")
    return p


async def run(args) -> dict:
    fixtures = load_fixtures(dirs=args.fixtures_dirs,
                             include_builtin=not args.no_builtin_fixtures)
    cfg = Settings().as_cfg()
    models = args.models or [cfg.get("utility_model") or "claude-haiku-4-5"]

    results = []
    for model in models:
        for fixture in fixtures:
            if args.mock:
                caller = CriticCallFn(fn=functools.partial(mock_call, fixture=fixture))
            else:
                caller = CriticCallFn()
            results.append(await evaluate_fixture(
                fixture, model, cfg,
                timeout_s=args.timeout_s,
                latency_budget_s=args.latency_budget_s,
                failure_policy=args.failure_policy,
                caller=caller,
            ))

    return aggregate(results,
                     recall_target=args.recall_target,
                     false_alarm_target=args.false_alarm_target,
                     latency_budget_s=args.latency_budget_s,
                     projected_volume=args.projected_volume), models


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    report, models = asyncio.run(run(args))

    if args.format == "json":
        out = json.dumps(report, indent=2, default=str)
    else:
        out = render_markdown(report, models)

    if args.out:
        with open(args.out, "w") as f:
            f.write(out)
    else:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
