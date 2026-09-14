"""CLI for the attribution replay harness.

    .venv/bin/python -m eval_attribution --mock
    .venv/bin/python -m eval_attribution --model claude-haiku-4-5 --model gpt-5
    .venv/bin/python -m eval_attribution --model claude-haiku-4-5 \\
        --fixtures-dir /path/outside/git/private-replay --no-builtin-fixtures

A harness only. It never touches backend/engine.py and decides nothing; it
produces the numbers the #212 decision needs."""

import argparse
import asyncio
import json
import sys

from backend.config import Settings
from backend.llm_util import model_family
from eval_attribution.caller import mock_call, real_call
from eval_attribution.fixtures_loader import load_fixtures
from eval_attribution.projection import VARIANTS
from eval_attribution.prompt import build_call, seat_cfg
from eval_attribution.report import render_markdown
from eval_attribution.scoring import aggregate, score


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", action="append", dest="models",
                   help="model to play every probed seat (repeatable); default "
                        "claude-haiku-4-5")
    p.add_argument("--variant", action="append", dest="variants", choices=VARIANTS,
                   help="projection shape (repeatable); default all three")
    p.add_argument("--fixtures-dir", action="append", dest="fixtures_dirs", default=[])
    p.add_argument("--no-builtin-fixtures", action="store_true")
    p.add_argument("--timeout-s", type=float, default=30.0)
    p.add_argument("--max-tokens", type=int, default=40)
    p.add_argument("--mock", action="store_true",
                   help="keyless stand-in that acts out the hypothesis; for checking "
                        "the harness, never for numbers")
    p.add_argument("--format", choices=("markdown", "json"), default="markdown")
    p.add_argument("--out")
    return p


async def run(args, caller=None):
    fixtures = load_fixtures(dirs=args.fixtures_dirs,
                             include_builtin=not args.no_builtin_fixtures)
    base_cfg = Settings().as_cfg()
    models = args.models or ["claude-haiku-4-5"]
    variants = args.variants or list(VARIANTS)
    results = []
    for model in models:
        family = model_family(model)
        for variant in variants:
            for fixture in fixtures:
                cfg = seat_cfg(fixture, base_cfg)
                for probe in fixture.probes:
                    system, messages = build_call(fixture, probe, variant, family, cfg)
                    if args.mock:
                        completion = await mock_call(fixture, probe, variant, model,
                                                     system, messages)
                    elif caller is not None:
                        completion = await caller(model, system, messages,
                                                  max_tokens=args.max_tokens,
                                                  timeout=args.timeout_s)
                    else:
                        completion = await real_call(model, system, messages,
                                                     max_tokens=args.max_tokens,
                                                     timeout=args.timeout_s)
                    results.append(score(fixture, probe, variant, model,
                                         completion, cfg))
    return aggregate(results), models, variants


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    report, models, variants = asyncio.run(run(args))
    out = (json.dumps(report, indent=2, default=str) if args.format == "json"
           else render_markdown(report, models, variants, args.mock))
    if args.out:
        with open(args.out, "w") as f:
            f.write(out)
    else:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
