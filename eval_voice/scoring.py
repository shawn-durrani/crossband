"""Score what a system said about each turn against the truth. Pure.

Each turn gets one verdict:

  right      the voice heard most on its own is named, and every name on
             the turn belongs to someone who spoke in it
  unnamed    no wrong name, and the main voice isn't named. The report
             says whether the system gave a reason
  wrong      a name on the turn belongs to someone who didn't speak in it
  no label   the system said nothing about the turn

A crosstalk turn is also judged on its split: whether it was marked as
two voices, whether both were named right, and what share of the words
the system gave a person went to the person who said them.

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
    elif truth.main in named:
        verdict = "right"
    else:
        verdict = "unnamed"
    out = {"script": truth.script, "index": truth.index, "truth": truth.main,
           "voices": truth.names, "tags": truth.tags(), "named": named,
           "wrong_names": wrong_names, "verdict": verdict,
           "reason": heard.reason if verdict == "unnamed" else "",
           "has_reason": bool(heard.reason or heard.placeholders),
           "learning": heard.learning, "in_time": heard.in_time,
           "note": heard.note, "seconds_alone": truth.alone_s(truth.main),
           "enrolled": truth.enrolled, "crosstalk": None}
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
        ev[f"{e.kind} {e.value}" if e.kind == "room" else e.kind][e.result] += 1
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
        "rows": rows,
        "cost": cost or {},
    }
