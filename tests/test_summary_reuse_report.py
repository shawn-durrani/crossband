"""scripts/summary_reuse_report.py, the week of numbers #565 asked for.

The memory summary rides every Claude seat call's uncached tail. The report
reads the summary fingerprints the calls recorded and says how often a
cached block of its own would have been read back, and what that would
have saved at each model's rate card. Everything here is synthetic records:
fingerprints are made-up strings and no call carries any text.
"""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from backend import db
from backend.config import DEFAULT_PRICING, Settings

REPO = Path(__file__).resolve().parents[1]
T0 = 1_700_000_000.0  # a fixed moment, so gaps are exact
TOKENS = 3000         # 12,000 characters at four a token
OPUS = "claude-opus-4-8"      # $5/M input, reads 0.1x, writes 1.25x
OPUS_5_5 = "claude-opus-5-5"  # $4/M input, reads 0.05x


def _script():
    spec = importlib.util.spec_from_file_location(
        "summary_reuse_report", REPO / "scripts" / "summary_reuse_report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rep = _script()


def call(at, summary="s1", *, model=OPUS, chat=1, seat="claude",
         stable="st1", tools="tl1", requests=1, cache_read=0, cost=0.10,
         chars=4 * TOKENS):
    return rep.Call(ts=T0 + at, chat_id=chat, speaker=seat, model=model,
                    tools=tools, stable=stable, summary=summary,
                    chars=0 if summary == "none" else chars,
                    requests=requests, cache_read=cache_read, cost=cost)


def tally(*calls, model=OPUS):
    return rep.analyse(list(calls), DEFAULT_PRICING)[model]


def per_token(model):
    return DEFAULT_PRICING[model]["input"] / 1_000_000


# ---------- what counts as a call caching would have served ----------

def test_the_same_summary_within_five_minutes_is_a_repeat():
    t = tally(call(0), call(60))
    assert (t.repeats, t.served, t.written) == (1, 1, 1)
    assert t.saved == pytest.approx(TOKENS * per_token(OPUS) * 0.9)
    assert t.premium == pytest.approx(TOKENS * per_token(OPUS) * 0.25)


def test_five_minutes_apart_the_block_has_expired():
    t = tally(call(0), call(300))
    assert (t.repeats, t.served, t.written) == (0, 0, 2)
    assert t.saved == 0


def test_each_repeat_keeps_the_block_alive():
    """A read refreshes the block, so three calls four minutes apart are
    two repeats, though the first and last are eight minutes apart."""
    t = tally(call(0), call(240), call(480))
    assert t.repeats == 2


def test_a_different_model_or_prompt_ahead_is_not_a_repeat():
    """The cache serves a block only when everything before it matches: a
    second seat has its own stable block, and a chat with other tools has
    its own tool list."""
    calls = [call(0), call(30, seat="sonnet", stable="st2"),
             call(60, tools="tl2"), call(90, model="claude-sonnet-5")]
    assert tally(*calls).repeats == 0
    assert tally(*calls, model="claude-sonnet-5").repeats == 0


def test_a_new_summary_is_not_a_repeat():
    t = tally(call(0, "s1"), call(60, "s2"))
    assert t.repeats == 0
    assert len(t.summaries) == 2


def test_tool_rounds_after_the_first_are_always_served():
    """Each request sends the summary again, seconds after the one before."""
    t = tally(call(0, requests=3))
    assert (t.requests, t.repeats, t.served, t.written) == (3, 0, 2, 1)


def test_a_call_that_sent_no_summary_is_counted_apart():
    t = tally(call(0), call(30, "none"), call(60))
    assert t.no_summary == 1
    assert t.summaries == {"s1"}
    assert t.repeats == 1  # the call with none didn't break the block


# ---------- the cost of a new summary ----------

def test_a_new_summary_on_a_warm_conversation_rewrites_it():
    """Behind the summary block, the conversation the call read from cache
    would be written again. Counted per request, as an upper bound."""
    t = tally(call(0, "s1"), call(60, "s2", requests=2, cache_read=40_000))
    assert (t.changes, t.rewrites) == (1, 1)
    assert t.rewrite == pytest.approx(
        20_000 * per_token(OPUS) * (1.25 - 0.1))


def test_a_new_summary_on_a_cold_conversation_costs_nothing_extra():
    t = tally(call(0, "s1"), call(400, "s2", cache_read=40_000))
    assert (t.changes, t.rewrites, t.rewrite) == (1, 0, 0)


def test_another_chat_or_seat_leaves_the_conversation_alone():
    t = tally(call(0, "s1"), call(60, "s2", chat=2, cache_read=40_000),
              call(90, "s2", seat="sonnet", stable="st2", cache_read=40_000))
    assert (t.changes, t.rewrites) == (0, 0)


# ---------- the rate card ----------

def test_each_model_is_priced_at_its_own_card():
    """Opus 5.5 reads its cache at 0.05x and Opus 4.8 at 0.1x."""
    t = tally(call(0, model=OPUS_5_5), call(60, model=OPUS_5_5),
              model=OPUS_5_5)
    assert t.saved == pytest.approx(TOKENS * per_token(OPUS_5_5) * 0.95)
    assert t.net == pytest.approx(t.saved - t.premium - t.rewrite)


def test_a_model_with_no_rate_card_is_not_priced():
    t = tally(call(0, model="claude-unknown"), call(60, model="claude-unknown"),
              model="claude-unknown")
    assert t.priced is False
    assert t.repeats == 1


def test_the_report_says_what_share_of_spend_the_net_is():
    calls = [call(0, cost=1.0), call(60, cost=1.0)]
    tallies = rep.analyse(calls, DEFAULT_PRICING)
    lines = rep.report(calls, 0, tallies, T0, T0 + 600)
    net = tallies[OPUS].net
    assert f"  net is {net / 2.0:.1%} of these calls' $2.00 recorded spend" \
        in lines


# ---------- end to end, on a throwaway database ----------

def _usage(summary, *, model=OPUS, requests=1, cost=0.05):
    pref = {"model": model, "tools": "0" * 16, "stable": "1" * 16,
            "volatile": "2" * 16, "transcript": "3" * 16, "thinking": "none",
            "effort": "default", "changed": []}
    if summary is not None:
        pref.update(summary=summary, summary_chars=12_000, requests=requests)
    return json.dumps({"input": 5000, "cache_read": 20_000,
                       "cache_creation": 0, "output": 40, "model": model,
                       "cost": cost, "cache_prefix": pref})


@pytest.fixture
def seeded(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"), port=1)
    db.configure(settings.resolved_data_dir())
    db.init(settings)
    con = db.connect()
    con.execute("INSERT INTO chats(id, title, created_at, updated_at) "
                "VALUES(3, 'T', 0, 0)")
    rows = [(0, None), (100, "a" * 16), (160, "a" * 16), (220, "b" * 16)]
    for at, summary in rows:
        msg = db.insert_message(con, 3, "claude", "Reply.", notify=False,
                                usage_json=_usage(summary))
        con.execute("UPDATE messages SET created_at=? WHERE id=?",
                    (T0 + at, msg["id"]))
    # a pass: no message, same usage block, counted like a reply
    db.log_seat_usage(con, 3, "claude", "pass", _usage("b" * 16, requests=2))
    con.execute("UPDATE seat_usage SET created_at=?", (T0 + 250,))
    # a GPT reply has no cache_prefix, and an hour later is out of range
    db.insert_message(con, 3, "gpt", "Reply.", notify=False,
                      usage_json=json.dumps({"input": 10, "cost": 0.01}))
    late = db.insert_message(con, 3, "claude", "Reply.", notify=False,
                             usage_json=_usage("c" * 16))
    con.execute("UPDATE messages SET created_at=? WHERE id=?",
                (T0 + 3600, late["id"]))
    con.commit()
    con.close()
    return settings


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_the_report_reads_both_tables_and_writes_nothing(seeded):
    path = seeded.resolved_data_dir() / "chat.db"
    before = _digest(path)
    lines = []
    assert rep.main(["--days", "1"], settings=seeded, now=T0 + 1800,
                    out=lines.append) == 0
    text = "\n".join(lines)
    assert "1 Claude seat call(s) in the period were recorded before" in text
    assert "Claude seat calls: 4, making 5 requests" in text
    assert "Distinct summaries: 2, median length 12,000 characters " \
           "(about 3,000 tokens)" in text
    assert "  repeats within five minutes: 2 of 4 calls" in text
    assert "  sends a cached summary would have served: 3 of 5" in text
    assert "  new summary since the seat's last call in that chat: 1" in text
    assert "c" * 16 not in text and "a" * 16 not in text
    assert _digest(path) == before


def test_a_missing_database_is_never_created(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "nowhere"), port=1)
    with pytest.raises(SystemExit):
        rep.main([], settings=settings, out=lambda _: None)
    assert not (tmp_path / "nowhere").exists()
