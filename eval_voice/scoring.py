"""Score what a system said about each turn against the truth. Pure.

Each turn gets one verdict:

  right      the voice heard most on its own is named, and every name on
             the turn belongs to someone who spoke in it
  unnamed    no wrong name, and the main voice isn't named. The report
             says whether the system gave a reason
  wrong      a name on the turn belongs to someone who didn't speak in it
  no label   the system said nothing about the turn

A turn the TV spoke is right when it carries no name at all, whatever
the reason, since a TV is nobody to name. Any name on it is wrong.

A crosstalk turn is also judged on its split: whether it was marked as
two voices, whether both were named right, and what share of the words
the system gave a person went to the person who said them.

When the system names voices once more after a conversation goes quiet,
each turn is judged a second time on what it says after that pass, and
the two readings are set side by side (end_pass).

Names are matched to the synthetic roster by spelling, loosely, because a
transcriber can spell a name it has never been told ("Matteo"). A name
that isn't close to anyone on the roster counts as wrong.
"""

import collections
import difflib
import re

from eval_voice import cast as cast_mod
from eval_voice.truth import SHORT_S

VERDICTS = ("right", "unnamed", "wrong", "no label")
NAME_MATCH = 0.75
SPLIT_RIGHT_SHARE = 0.8
WORD = re.compile(r"[a-z0-9']+")


def canonical(name: str, roster=None) -> str:
    """The roster name a label means, or the label itself when it's close to
    nobody."""
    roster = tuple(roster or cast_mod.CAST)
    clean = (name or "").strip()
    for r in roster:
        if clean.casefold() == r.casefold():
            return r
    best, score = "", 0.0
    for r in roster:
        s = difflib.SequenceMatcher(None, clean.casefold(), r.casefold()).ratio()
        if s > score:
            best, score = r, s
    return best if score >= NAME_MATCH else clean


def words(text: str) -> list:
    return WORD.findall((text or "").casefold())


def split_share(truth, heard, roster=None):
    """(right, wrong, unplaced) word counts for a crosstalk turn: words the
    system put under a name, matched against what each person said."""
    left = {v.name: collections.Counter(words(v.words)) for v in truth.voices}
    right = wrong = unplaced = 0
    for seg in heard.segments:
        name = canonical(seg.get("name") or "", roster) if seg.get("name") \
            else ""
        for w in words(seg.get("text") or ""):
            if not name:
                unplaced += 1
            elif left.get(name, {}).get(w):
                left[name][w] -= 1
                right += 1
            elif any(c.get(w) for n, c in left.items() if n != name):
                for n, c in left.items():
                    if n != name and c.get(w):
                        c[w] -= 1
                        break
                wrong += 1
            else:
                unplaced += 1
    return right, wrong, unplaced


def judge(truth, heard, roster=None) -> dict:
    """One turn's verdict and the facts behind it."""
    spoke = set(truth.names)
    named = [canonical(n, roster) for n in heard.names]
    wrong_names = [n for n in named if n not in spoke]
    if not heard.labelled:
        verdict = "no label"
    elif wrong_names:
        verdict = "wrong"
    elif truth.main in named or (truth.media and not named):
        verdict = "right"
    else:
        verdict = "unnamed"
    out = {"script": truth.script, "index": truth.index, "truth": truth.main,
           "voices": truth.names, "tags": truth.tags(), "named": named,
           "wrong_names": wrong_names, "verdict": verdict,
           "reason": (heard.reason if verdict == "unnamed"
                      or (truth.media and not named) else ""),
           "has_reason": bool(heard.reason or heard.placeholders),
           "learning": heard.learning, "in_time": heard.in_time,
           "note": heard.note, "seconds_alone": truth.alone_s(truth.main),
           "enrolled": truth.enrolled, "media": truth.media,
           "crosstalk": None}
    if truth.crosstalk or heard.crosstalk:
        r, w, u = split_share(truth, heard, roster)
        share = round(r / (r + w), 3) if (r + w) else None
        both = all(n in named for n in spoke)
        out["crosstalk"] = {
            "expected": truth.crosstalk, "marked": heard.crosstalk,
            "both_named": both and not wrong_names,
            "words_right": r, "words_wrong": w, "words_unplaced": u,
            "share_right": share,
            "split_right": bool(truth.crosstalk and heard.crosstalk
                                and both and not wrong_names
                                and share is not None
                                and share >= SPLIT_RIGHT_SHARE)}
    return out


def end_pass(rows) -> dict | None:
    """The names after the end-of-session pass beside the names when the
    conversation ended: each verdict's turns both ways, wrong names per 100
    turns both ways, and every turn whose name the pass changed. None when
    no turn was read again after a pass. A row carries the second reading
    as `after_end`, judge's verdict, names and reason."""
    scored = [r for r in rows if r.get("after_end")]
    if not scored:
        return None
    then = collections.Counter(r["verdict"] for r in scored)
    after = collections.Counter(r["after_end"]["verdict"] for r in scored)
    changed = [{"script": r["script"], "index": r["index"],
                "truth": r["truth"], "voices": r["voices"],
                "tags": r["tags"], "named": r["named"],
                "verdict": r["verdict"],
                "named_after": r["after_end"]["named"],
                "verdict_after": r["after_end"]["verdict"]}
               for r in scored
               if r["after_end"]["named"] != r["named"]
               or r["after_end"]["verdict"] != r["verdict"]]
    n = len(scored)
    return {"turns": n,
            "then": {v: then.get(v, 0) for v in VERDICTS},
            "after": {v: after.get(v, 0) for v in VERDICTS},
            "wrong_per_100": {"then": round(100 * then["wrong"] / n, 1),
                              "after": round(100 * after["wrong"] / n, 1)},
            "changed": changed}


MEDIA_REASON = "media"


def _event_kind(e) -> str:
    if e.kind == "room":
        return f"room {e.value}"
    if e.kind == "answer":
        return "answer the TV" if e.value in cast_mod.MEDIA else "answer a name"
    return e.kind


def asks(rows, events) -> list:
    """Each spoken answer to the system's "who's this?" about a new voice,
    judged on the names as they finally stand (after the end-of-session
    pass when there was one). For a name: did the turn the ask pointed at
    take it, did the voice's turns before and after the answer carry it,
    and was a clip of the voice saved. For the TV: were the same turns
    marked as the TV, with no person made for it. Both: was the ask still
    open, or open again, when the conversation ended."""
    by_script = collections.defaultdict(dict)
    for r in rows:
        by_script[r["script"]][r["index"]] = r
    out = []
    for e in events:
        if e.kind != "answer":
            continue
        turns = by_script.get(e.script) or {}
        who = e.value
        d = e.detail or {}

        def final(r):
            return r.get("after_end") or r

        def carries(r):
            f = final(r)
            if r.get("media"):
                return not f["named"] and f.get("reason") == MEDIA_REASON
            return who in f["named"]

        ask_turn = d.get("ask_turn")
        asked = turns.get(ask_turn) if ask_turn is not None else None
        before = [r for i, r in sorted(turns.items())
                  if i < e.index and r["truth"] == who]
        after = [r for i, r in sorted(turns.items())
                 if i > e.index and r["truth"] == who]
        media = bool(asked and asked.get("media")) or who not in cast_mod.CAST
        out.append({
            "script": e.script, "index": e.index, "answer": who,
            "media": media, "result": e.result, "ask_turn": ask_turn,
            "asked_right": bool(asked and asked["truth"] == who),
            "named": bool(asked and carries(asked)),
            "before": [sum(carries(r) for r in before), len(before)],
            "after": [sum(carries(r) for r in after), len(after)],
            "clips": d.get("clips"), "people_new": d.get("people_new") or [],
            "open_asks": d.get("open_asks")})
    return out


def _share(n, total):
    return round(n / total, 3) if total else None


def targets(rows) -> dict:
    """The redesign's targets (docs/VOICE_ID_REDESIGN.md, Measuring it),
    as far as a made-up run can measure them."""
    total = len(rows)
    wrong = sum(r["verdict"] == "wrong" for r in rows)
    seen = collections.Counter()
    later_known, later_unnamed = 0, 0
    first = {}
    for r in rows:
        key = (r["script"], r["truth"])
        seen[key] += 1
        if r["enrolled"] and seen[key] > 2:
            later_known += 1
            later_unnamed += r["verdict"] in ("unnamed", "no label")
        crosstalk = bool(r["crosstalk"] and r["crosstalk"]["expected"])
        if r["enrolled"] and key not in first and not crosstalk \
                and r["seconds_alone"] >= SHORT_S:
            first[key] = r["verdict"] == "right"
    labelled = [r for r in rows if r["verdict"] != "no label"
                and r["in_time"] is not None]
    return {
        "wrong_per_100": round(100 * wrong / total, 1) if total else None,
        "unnamed_per_100_after_two": (round(100 * later_unnamed / later_known, 1)
                                      if later_known else None),
        "turns_after_two": later_known,
        "first_named": sum(first.values()), "first_turns": len(first),
        "in_time": sum(bool(r["in_time"]) for r in labelled),
        "in_time_of": len(labelled),
    }


def aggregate(rows, events=(), cost=None) -> dict:
    """The run's numbers from the per-turn rows (judge's output) and the
    event checks (adapter.EventCheck)."""
    total = len(rows)
    counts = collections.Counter(r["verdict"] for r in rows)
    summary = {v: {"turns": counts.get(v, 0),
                   "share": _share(counts.get(v, 0), total)} for v in VERDICTS}
    unnamed = [r for r in rows if r["verdict"] == "unnamed"]
    summary["unnamed"]["with_reason"] = sum(r["has_reason"] for r in unnamed)
    by_tag = collections.defaultdict(collections.Counter)
    for r in rows:
        for tag in r["tags"]:
            by_tag[tag][r["verdict"]] += 1
            by_tag[tag]["turns"] += 1
    xt = [r for r in rows if r["crosstalk"] and r["crosstalk"]["expected"]]
    false_xt = [r for r in rows if r["crosstalk"]
                and not r["crosstalk"]["expected"] and r["crosstalk"]["marked"]]
    words_r = sum(r["crosstalk"]["words_right"] for r in xt)
    words_w = sum(r["crosstalk"]["words_wrong"] for r in xt)
    crosstalk = {
        "turns": len(xt),
        "marked": sum(r["crosstalk"]["marked"] for r in xt),
        "both_named": sum(r["crosstalk"]["both_named"] for r in xt),
        "split_right": sum(r["crosstalk"]["split_right"] for r in xt),
        "words_share_right": _share(words_r, words_r + words_w),
        "false_marked": len(false_xt),
    }
    ev = collections.defaultdict(collections.Counter)
    for e in events:
        ev[_event_kind(e)][e.result] += 1
    return {
        "turns": total,
        "scripts": sorted({r["script"] for r in rows}),
        "summary": summary,
        "wrong": [{"script": r["script"], "index": r["index"],
                   "truth": r["truth"], "voices": r["voices"],
                   "named": r["named"], "tags": r["tags"]}
                  for r in rows if r["verdict"] == "wrong"],
        "targets": targets(rows),
        "by_tag": {t: dict(c) for t, c in sorted(by_tag.items())},
        "crosstalk": crosstalk,
        "events": {k: dict(c) for k, c in sorted(ev.items())},
        "no_transcript": sum(r["note"] == "no transcript" for r in rows),
        "end_pass": end_pass(rows),
        "asks": asks(rows, events),
        "rows": rows,
        "cost": cost or {},
    }
