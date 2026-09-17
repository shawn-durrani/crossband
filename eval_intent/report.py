"""Renders scoring.aggregate output as markdown."""


def _pct(x):
    return "n/a" if x is None else f"{x * 100:.0f}%"


def render_markdown(report: dict, mock: bool) -> str:
    lines = ["# Spoken intent report (crossband#258 harness)", ""]
    if mock:
        lines.append("MOCK RUN: keyless stand-ins acting out the graded verdicts. "
                     "Nothing here is evidence about any model.")
        lines.append("")
    s = report["silent_today"]
    lines.append(f"fixtures: {report['n_fixtures']}, with an instruction: "
                 f"{report['n_with_intent']}")
    lines.append(f"turns today's phrase lists never send to a model: {s['turns']} "
                 f"of {report['n_with_intent']} ({_pct(s['share'])}); by axis: "
                 + ", ".join(f"{a} {n}" for a, n in s["by_axis"].items()))
    strategies = report["strategies"]
    if strategies:
        lines += ["", "## Accuracy by strategy", "",
                  "| strategy | every axis right | calls per turn | cost per turn | latency p50 |",
                  "|---|---|---|---|---|"]
        for name, st in strategies.items():
            lat = "n/a" if st["latency_p50_s"] is None else f"{st['latency_p50_s'] * 1000:.0f} ms"
            lines.append(f"| {name} | {_pct(st['all_axes_right'])} | "
                         f"{st['calls_per_turn']:.2f} | ${st['cost_per_turn_usd']:.4f} | {lat} |")
        lines += ["", "## Accuracy by axis", "",
                  "| axis | " + " | ".join(strategies) + " |",
                  "|---|" + "---|" * len(strategies)]
        axes = next(iter(strategies.values()))["per_axis"]
        for a in axes:
            lines.append(f"| {a} | " + " | ".join(_pct(st["per_axis"][a])
                                                 for st in strategies.values()) + " |")
        lines += ["", "## Accuracy by category", "",
                  "| category | " + " | ".join(strategies) + " |",
                  "|---|" + "---|" * len(strategies)]
        cats = sorted({c for st in strategies.values() for c in st["per_category"]})
        for c in cats:
            lines.append(f"| {c} | " + " | ".join(_pct(st["per_category"].get(c))
                                                 for st in strategies.values()) + " |")
        for name, st in strategies.items():
            if st["missing_key"] or st["timeouts"]:
                lines.append(f"\n{name}: missing-key calls {st['missing_key']}, "
                             f"timeouts {st['timeouts']}")
            if st["misses"]:
                lines += ["", f"## Misses, {name}", ""]
                for m in st["misses"]:
                    lines.append(f"- {m['id']} ({m['category']}): wrong on "
                                 f"{', '.join(m['wrong'])}. \"{m['text']}\"")
    if s["ids"]:
        lines += ["", "## Turns today's lists drop", ""]
        lines += [f"- {i}" for i in s["ids"]]
    return "\n".join(lines) + "\n"
