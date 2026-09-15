"""CLI for the recall replay harness.

    .venv/bin/python -m eval_recall --mock
    .venv/bin/python -m eval_recall
    .venv/bin/python -m eval_recall --chat 42 --max-turns 0 \\
        --format json --with-content --out /path/outside/git/recall-42.json

A harness only. It never touches backend/engine.py and decides nothing; it
produces the numbers the #252 tuning needs: a floor sweep, a per-rank table
and the latency, over your own turns and your own membro."""

import argparse
import asyncio
import json
import sys
import time

from backend.config import Settings
from eval_recall.ledger import user_turns
from eval_recall.report import render_markdown
from eval_recall.scoring import (DEFAULT_FLOORS, TurnResult, aggregate,
                                 content_words, hits_from_facts)

LIVE_K = 6      # engine.py asks for this many facts per round
ORIGIN = "eval"  # membro's access log keeps this apart from the live "auto"


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", help="path to chat.db; default the app's own data dir")
    p.add_argument("--memory-url", help="membro base URL; default from settings")
    p.add_argument("--chat", action="append", dest="chats", type=int, default=[],
                   help="replay this chat only (repeatable)")
    p.add_argument("--max-turns", type=int, default=200,
                   help="newest user turns to replay; 0 means every turn")
    p.add_argument("--include-memory-off", action="store_true",
                   help="also replay chats that have memory switched off")
    p.add_argument("--top-k", type=int, default=10,
                   help="facts to ask for per turn, to see past the live count")
    p.add_argument("--live-k", type=int, default=LIVE_K,
                   help="facts a live round injects; the floor sweep counts within it")
    p.add_argument("--floors", default=",".join(str(f) for f in DEFAULT_FLOORS),
                   help="comma-separated score floors to sweep")
    p.add_argument("--with-content", action="store_true",
                   help="put the query text and fact content in the JSON corpus")
    p.add_argument("--mock", action="store_true",
                   help="a made-up ledger and a stand-in memory; for checking the "
                        "harness, never for numbers")
    p.add_argument("--format", choices=("markdown", "json"), default="markdown")
    p.add_argument("--out")
    return p


def parse_floors(text: str) -> list[float]:
    floors = sorted({float(x) for x in text.split(",") if x.strip()})
    if not floors:
        raise SystemExit("--floors needs at least one number")
    return floors


async def replay(turns, memory, top_k: int, with_content: bool = False) -> list[TurnResult]:
    summary_words = content_words(await memory.get_summary())
    results = []
    for t in turns:
        t0 = time.perf_counter()
        facts = await memory.recall(t.query, limit=top_k, origin=ORIGIN,
                                    chat_id=t.chat_id)
        ms = (time.perf_counter() - t0) * 1000.0
        results.append(TurnResult(chat_id=t.chat_id, message_id=t.message_id,
                                  created_at=t.created_at, query_chars=len(t.query),
                                  recall_ms=ms,
                                  hits=hits_from_facts(facts, summary_words),
                                  query=t.query if with_content else ""))
    return results


async def run(args, memory=None, turns=None) -> dict:
    floors = parse_floors(args.floors)
    if args.mock:
        from eval_recall.mock import MockMemory, mock_turns
        memory = memory or MockMemory()
        turns = turns if turns is not None else mock_turns()
    else:
        settings = Settings()
        if turns is None:
            db = args.db or str(settings.resolved_data_dir() / "chat.db")
            turns = user_turns(db, chat_ids=args.chats, max_turns=args.max_turns,
                               include_memory_off=args.include_memory_off)
        if memory is None:
            from backend.memory_client import MemoryClient
            memory = MemoryClient(args.memory_url or settings.memory_url, timeout=30.0)
            if not await memory.probe(force=True):
                await memory.aclose()
                raise SystemExit(f"membro is not answering at {memory.base_url}; "
                                 "start it and run again")
    try:
        results = await replay(turns, memory, args.top_k, args.with_content)
    finally:
        await memory.aclose()
    return aggregate(results, floors, args.live_k, args.top_k, args.with_content)


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
