"""The run's report, as markdown for a person or JSON for a program.

The markdown holds names from the synthetic roster, counts and timings,
and no words: neither the script's lines nor what the transcriber heard,
unless you ask for them with show_words. That keeps a report fit to paste
into an issue.
"""

VERDICT_LABELS = {"right": "Named right", "unnamed": "Unnamed",
                  "wrong": "Wrong name", "no label": "No label"}


def _pct(share):
    return "n/a" if share is None else f"{round(100 * share)}%"


def _of(n, total):
    return f"{n} of {total}" if total else "none to measure"


def cost_lines(cost: dict) -> list:
    out = []
    tts = cost.get("tts") or {}
    if tts and tts.get("model") != "mock":
        out.append(f"- Speech rendered: {tts.get('new_chars', 0)} characters "
                   f"new ({tts.get('new_lines', 0)} lines), "
                   f"{tts.get('cached_chars', 0)} from the cache, on "
                   f"`{tts.get('model')}`. About ${tts.get('usd', 0):.3f}.")
    beds = cost.get("beds") or {}
    if beds.get("new_seconds") or beds.get("cached"):
        out.append(f"- Noise beds: {beds.get('new_seconds', 0):g} seconds "
                   f"made new, {beds.get('cached', 0)} from the cache. About "
                   f"${beds.get('usd', 0):.3f}.")
    if cost.get("stt_seconds") is not None:
        out.append(f"- Audio streamed to the app for transcription: "
                   f"{cost['stt_seconds']:.0f} seconds. About "
                   f"${cost.get('stt_usd', 0):.3f}.")
    if cost.get("generate_usd"):
        out.append(f"- Scripts written by the utility model: about "
                   f"${cost['generate_usd']:.3f}.")
    ledger = (cost.get("app") or {}).get("app_ledger") or {}
    totals = ledger.get("totals") or {}
    if totals:
        parts = ", ".join(f"{k} ${v:.3f}" for k, v in sorted(totals.items())
                          if isinstance(v, (int, float)))
        out.append(f"- The app's own ledger for the run: {parts}.")
    return out


def end_pass_lines(ep, notes=None) -> list:
    """The section on the end-of-session pass, or none when it wasn't
    scored."""
    if not ep:
        return []
    notes = notes or {}
    out = ["## After the end-of-session pass", "",
           "The app names every voice once more when a voice session goes "
           "quiet. The rig waits for that pass and reads every turn again.",
           "", "| Verdict | When the conversation ended | After the pass |",
           "|---|---|---|"]
    for v, label in VERDICT_LABELS.items():
        out.append(f"| {label} | {ep['then'][v]} | {ep['after'][v]} |")
    w = ep["wrong_per_100"]
    out += [f"| Wrong names per 100 turns | {w['then']} | {w['after']} |", ""]
    if notes.get("sessions"):
        out.append(f"{notes.get('ended', 0)} of {notes['sessions']} voice "
                   f"sessions ended with the pass, which relabelled "
                   f"{notes.get('relabelled', 0)} turns.")
        out.append("")
    if ep["changed"]:
        out += ["Turns the pass changed:", ""]
        for c in ep["changed"]:
            before = ", ".join(c["named"]) or "no name"
            after = ", ".join(c["named_after"]) or "no name"
            out.append(f"- `{c['script']}` turn {c['index']}, "
                       f"{' + '.join(c['voices'])} spoke: {before} "
                       f"({c['verdict']}) became {after} "
                       f"({c['verdict_after']})")
    else:
        out.append("The pass changed no turn's name.")
    out.append("")
    return out


def ask_lines(asks) -> list:
    """The section on spoken answers to "who's this?", or none when no
    script answered one."""
    if not asks:
        return []
    out = ["", "## Who's this", "",
           "A voice nobody knows talks until the app asks who it is, and "
           "someone answers out loud. These are judged on the names as they "
           "finally stand.", "",
           "| Script | Answer | The ask pointed at | That turn took it "
           "| Earlier turns relabelled | Later turns | Saved "
           "| Still asking at the end |",
           "|---|---|---|---|---|---|---|---|"]
    for a in asks:
        answer = "the TV" if a["media"] else a["answer"]
        if a["ask_turn"] is None:
            asked = "nothing asked" if a["result"] == "no ask" else "unknown"
        else:
            whose = "the new voice" if a["asked_right"] else "another voice"
            asked = f"turn {a['ask_turn']}, {whose}"
        if a["media"]:
            saved = ("no person made" if not a["people_new"]
                     else f"made {', '.join(a['people_new'])}")
        else:
            n = a["clips"] or 0
            saved = f"{n} clip{'s' if n != 1 else ''}"
        again = {0: "no", None: ""}.get(a["open_asks"], "yes")
        out.append(f"| {a['script']} | {answer} | {asked} | "
                   f"{'yes' if a['named'] else 'no'} | "
                   f"{_of(*a['before'])} | {_of(*a['after'])} | {saved} | "
                   f"{again} |")
    out += ["", "A turn took the answer when it carries the name. For the "
            "TV, it carries no name and the TV as its reason."]
    return out


def render_markdown(report: dict, mock: bool = False,
                    show_words: bool = False) -> str:
    s = report["summary"]
    total = report["turns"]
    setup = report.get("setup") or {}
    out = ["# Voice rig report", ""]
    if mock:
        out += ["Mock run: keyless stand-ins answered from the truth, wrong on "
                "a fixed few turns. These numbers say nothing about any "
                "system.", ""]
    n = len(report["scripts"])
    out.append(f"{n} conversation{'s' if n != 1 else ''}, {total} spoken "
               f"turns, through the {setup.get('adapter', 'unknown')} adapter.")
    if setup.get("described"):
        out.append(setup["described"])
    for sid, why in (report.get("skipped") or {}).items():
        out += ["", f"Left out `{sid}`, because {why}"]
    out += ["", "## Naming", "", "| Verdict | Turns | Share |", "|---|---|---|"]
    for v, label in VERDICT_LABELS.items():
        row = s[v]
        extra = (f" ({row['with_reason']} with a reason)"
                 if v == "unnamed" and row["turns"] else "")
        out.append(f"| {label} | {row['turns']}{extra} | {_pct(row['share'])} |")
    out.append("")
    if report["wrong"]:
        out.append("Wrong names, one by one:")
        out.append("")
        for w in report["wrong"]:
            others = [v for v in w["voices"] if v != w["truth"]]
            with_ = f" with {', '.join(others)}" if others else ""
            out.append(f"- `{w['script']}` turn {w['index']}: {w['truth']} "
                       f"spoke{with_}, and the app wrote "
                       f"{', '.join(w['named'])} ({', '.join(w['tags'])})")
    else:
        out.append("No wrong names.")
    t = report["targets"]
    out += ["", "## Against the redesign's targets", "",
            "| Measure | This run | Target |", "|---|---|---|",
            f"| Wrong names per 100 turns | {t['wrong_per_100']} | 1 at most |",
            f"| Unnamed per 100 turns, after each person's first two | "
            f"{t['unnamed_per_100_after_two']} "
            f"(of {t['turns_after_two']} turns) | 5 at most |",
            f"| Known person named on their first turn of 1.5 seconds or "
            f"more | {_of(t['first_named'], t['first_turns'])} | 9 in 10 |",
            f"| Name on the message when it was saved | "
            f"{_of(t['in_time'], t['in_time_of'])} | every turn |", ""]
    out += end_pass_lines(report.get("end_pass"),
                          (report.get("diagnostics") or {}).get("end_pass"))
    out += ["## By condition", "",
            "| Condition | Turns | Right | Unnamed | Wrong | No label |",
            "|---|---|---|---|---|---|"]
    for tag, c in report["by_tag"].items():
        out.append(f"| {tag} | {c.get('turns', 0)} | {c.get('right', 0)} | "
                   f"{c.get('unnamed', 0)} | {c.get('wrong', 0)} | "
                   f"{c.get('no label', 0)} |")
    x = report["crosstalk"]
    out += ["", "## Crosstalk", "",
            "| Turns with two voices | Marked as two voices | Both named right "
            "| Split right | Words with the right person |",
            "|---|---|---|---|---|",
            f"| {x['turns']} | {x['marked']} | {x['both_named']} | "
            f"{x['split_right']} | {_pct(x['words_share_right'])} |", "",
            f"Single-voice turns marked as two voices: {x['false_marked']}.", ""]
    out += ["## Introductions and instructions", ""]
    if report["events"]:
        out += ["| Kind | Heard | Missed | Already so | Nothing asked |",
                "|---|---|---|---|---|"]
        for kind, c in report["events"].items():
            out.append(f"| {kind} | {c.get('heard', 0)} | {c.get('missed', 0)} "
                       f"| {c.get('already', 0)} | {c.get('no ask', 0)} |")
    else:
        out.append("None checked.")
    out += ask_lines(report.get("asks"))
    out += ["", "## Every turn", ""]
    head = ("| Script | Turn | Spoke | Conditions | App wrote | Reason | "
            "Verdict | On time | Naming | After the pass |")
    if show_words:
        head += " Heard as |"
    out += [head, "|" + "---|" * (head.count("|") - 1)]
    for r in report["rows"]:
        wrote = ", ".join(r["named"]) or "no name"
        if r.get("learning"):
            wrote += " (learning)"
        if r["crosstalk"] and r["crosstalk"]["marked"]:
            wrote += ", two voices"
        on_time = {True: "yes", False: "no", None: ""}[r["in_time"]]
        later = r.get("after_end") or {}
        changed = later and (later["named"] != r["named"]
                             or later["verdict"] != r["verdict"])
        after = (f"{', '.join(later['named']) or 'no name'} "
                 f"({later['verdict']})" if changed
                 else "same" if later else "")
        line = (f"| {r['script']} | {r['index']} | {' + '.join(r['voices'])} "
                f"| {', '.join(r['tags'])} | {wrote} | "
                f"{r['reason'] or r['note']} | {r['verdict']} | {on_time} | "
                f"{r.get('method', '')} | {after} |")
        if show_words:
            line += f" {r.get('transcript') or ''} |"
        out.append(line)
    out += ["", "## Setup and cost", ""]
    enrol = setup.get("enrol") or {}
    for name, p in (enrol.get("people") or {}).items():
        ready = (enrol.get("readiness") or {}).get(name) or {}
        why = f", readiness {ready.get('reason')}" if ready.get("reason") else ""
        out.append(f"- {name}: {p.get('clips')} clips, {p.get('seconds')} "
                   f"seconds recorded before the run{why}.")
    cal = enrol.get("calibration") or {}
    if cal.get("state"):
        out.append(f"- Calibrated scorer: {cal.get('state')}, calibrated "
                   f"{cal.get('calibrated')}, {cal.get('people')} people, "
                   f"{cal.get('clips')} clips.")
    diag = report.get("diagnostics") or {}
    if diag.get("methods"):
        out.append("- How turns were named: " + ", ".join(
            f"{k} {v}" for k, v in sorted(diag["methods"].items())) + ".")
    if diag.get("errors"):
        out.append("- Diariser errors: " + ", ".join(
            f"{k} {v}" for k, v in sorted(diag["errors"].items())) + ".")
    if setup.get("diariser"):
        out.append(f"- Tracking sessions: {diag['sessions']} opened, "
                   f"{diag.get('sessions_ended', 0)} ended by the app, "
                   f"{diag.get('sessions_closed', 0)} closed by the rig.")
    if diag.get("people_met"):
        out.append(f"- People the app met in a conversation, forgotten when "
                   f"it ended: {diag['people_met']}.")
    if diag.get("relay_errors"):
        out.append(f"- Conversations the voice relay stopped in: "
                   f"{diag['relay_errors']}.")
    if report.get("no_transcript"):
        out.append(f"- Turns the transcriber returned nothing for: "
                   f"{report['no_transcript']}.")
    out += cost_lines(report.get("cost") or {})
    out.append("")
    return "\n".join(out)
