"""What the page did with each moment, read off its own record.

Pure: it takes the page's events (page.py) and a moment's timing, all on
the page's clock in milliseconds, and says what happened. Nothing here
runs the app, so the tests feed it made-up records.
"""

import statistics

SEND = "/send"
ABORT = "/round/abort"
# The detector can't hear a word before it's said. A decision this far
# ahead of the words still counts, for the few milliseconds between the
# rig's clock and the page's audio.
EARLY_MS = 150.0


def _sends(events, start, end):
    return [f for f in events.get("fetch", [])
            if f.get("path", "").endswith(SEND) and start <= f["t"] <= end]


def _first(log, tag, after, before=float("inf")):
    for e in log:
        if e.get("tag") == tag and after <= e["t"] <= before:
            return e
    return None


def pause(moment, clip, started, events, until) -> dict:
    """A pause moment. One turn, sent after the last word, is right. A turn
    sent before the last word means the app took the pause as the end."""
    end = started + clip.end_s * 1000
    sends = _sends(events, started, until)
    early = [s for s in sends if s["t"] < end]
    after = [s for s in sends if s["t"] >= end]
    out = {"id": moment.id, "kind": "pause", "pause_s": moment.pause_s,
           "noise": moment.noise or "", "turns": len(sends),
           "jumped_in": bool(early), "missed": not sends}
    if early:
        out["cut_in_ms"] = round(early[0]["t"] - (started + clip.pause[0] * 1000))
    if after and not early:
        out["send_ms"] = round(after[0]["t"] - end)
    fin = _first(events.get("log", []), "vad:finalize", end - EARLY_MS, until)
    if fin and not early:
        out["finalize_ms"] = round(fin["t"] - end)
    return out


def over(moment, clip, opening, opened, over_at, events, until) -> dict:
    """A moment someone talked over a reply. `opened` is when the opening
    question started, `over_at` when this moment's clip started."""
    log = events.get("log", [])
    said_end = opened + opening.end_s * 1000
    onset = over_at + clip.onset_s * 1000
    media = events.get("media", [])
    playing = next((m["t"] for m in media if m["ev"] == "playing" and m["t"] > said_end), None)
    sends = _sends(events, said_end - EARLY_MS, onset)
    out = {"id": moment.id, "kind": "over", "group": moment.group,
           "should_cut": moment.should_cut}
    if sends:
        out["send_ms"] = round(sends[0]["t"] - said_end)
    if playing is not None:
        out["reply_ms"] = round(playing - said_end)
    stopped = next((m for m in media if m["ev"] in ("pause", "ended")
                    and playing is not None and playing < m["t"] < onset - EARLY_MS), None)
    if playing is None or playing > onset or stopped is not None:
        out["no_reply"] = True    # nothing was playing to talk over
        return out
    hold = _first(log, "vad:hold", onset - EARLY_MS, until)
    cut = _first(log, "vad:bargeIn", onset - EARLY_MS, until)
    let_go = _first(log, "vad:letGo", (hold or {}).get("t", onset), until) if hold else None
    words = [e["data"].get("verdict") for e in log
             if e.get("tag") == "vad:words" and onset - EARLY_MS <= e["t"] <= until
             and isinstance(e.get("data"), dict)]
    judge = _first(log, "judge:verdict", onset - EARLY_MS, until)
    no_judge = _first(log, "judge:none", onset - EARLY_MS, until)
    # A cut is the barge-in line, or the round stopped by the words that
    # came after a reply was let go (a late cut).
    abort = next((f for f in events.get("fetch", []) if f.get("path", "").endswith(ABORT)
                  and onset - EARLY_MS <= f["t"] <= until), None)
    cut_at = min([e["t"] for e in (cut, abort) if e is not None], default=None)
    out.update({
        "held": hold is not None, "cut": cut_at is not None,
        "resumed": let_go is not None,
        "late_cut": cut_at is not None and let_go is not None and let_go["t"] < cut_at,
        "words": words[-1] if words else "",
        "judge": ((judge or {}).get("data") or {}).get("verdict", "")
                 or ("none: " + str(((no_judge or {}).get("data") or {}).get("why", ""))
                     if no_judge else ""),
    })
    if hold:
        out["hold_ms"] = round(hold["t"] - onset)
    if cut_at is not None:
        out["cut_ms"] = round(cut_at - onset)
    if let_go:
        out["resume_ms"] = round(let_go["t"] - onset)
    out["right"] = out["cut"] == moment.should_cut
    return out


def _median(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs)) if xs else None


def summarise(results, pause_setting_s=1.5) -> dict:
    """The run in numbers. A pause shorter than the app's pause setting
    should never end a turn. A longer one ends it by design, which is the
    trade the setting makes, so the two are counted apart."""
    pauses = [r for r in results if r["kind"] == "pause" and not r["missed"]]
    short = [r for r in pauses if r["pause_s"] < pause_setting_s]
    long_ = [r for r in pauses if r["pause_s"] >= pause_setting_s]
    overs = [r for r in results if r["kind"] == "over" and not r.get("no_reply")]
    groups = {}
    for r in overs:
        g = groups.setdefault(r["group"], {"moments": 0, "right": 0, "cut": 0,
                                           "held": 0, "cut_ms": [], "hold_ms": []})
        g["moments"] += 1
        g["right"] += r["right"]
        g["cut"] += r["cut"]
        g["held"] += r["held"]
        g["cut_ms"].append(r.get("cut_ms"))
        g["hold_ms"].append(r.get("hold_ms"))
    for g in groups.values():
        g["cut_ms"] = _median(g["cut_ms"])
        g["hold_ms"] = _median(g["hold_ms"])
    opened = [r for r in results if r["kind"] == "over"]
    return {
        "pauses": len(pauses),
        "short": len(short), "jumped_in_short": sum(r["jumped_in"] for r in short),
        "long": len(long_), "jumped_in_long": sum(r["jumped_in"] for r in long_),
        "missed_turns": sum(r["missed"] for r in results if r["kind"] == "pause"),
        "send_ms": _median([r.get("send_ms") for r in pauses]
                           + [r.get("send_ms") for r in opened]),
        "reply_ms": _median([r.get("reply_ms") for r in opened]),
        "over": len(overs),
        "over_right": sum(r["right"] for r in overs),
        "no_reply": sum(bool(r.get("no_reply")) for r in opened),
        "groups": groups,
    }
