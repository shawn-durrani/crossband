"""Renders scoring.aggregate output as markdown."""


def _pct(x):
    return "n/a" if x is None else f"{x * 100:.0f}%"


def render_markdown(report: dict, models: list[str], variants: list[str],
                    mock: bool) -> str:
    lines = ["# Attribution replay report (crossband#212 harness)", ""]
    if mock:
        lines.append("MOCK RUN: the answers come from a keyless stand-in acting out "
                     "the hypothesis. Nothing here is evidence about any model.")
        lines.append("")
    lines.append(f"models: {', '.join(models)}; variants: {', '.join(variants)}; "
                 f"probes: {report['n_probes']}; results: {report['n_results']}")
    if report["missing_key"]:
        lines.append(f"missing-key calls: {report['missing_key']} (set the API key "
                     "for that family and rerun)")
    if report["timeouts"] or report["unparsed"]:
        lines.append(f"timeouts: {report['timeouts']}; unparsed replies: "
                     f"{report['unparsed']}")
    lines += ["", "## Accuracy by projection shape", "",
              "| shape | model | all probes | probes about the seat itself | cost |",
              "|---|---|---|---|---|"]
    for v in variants:
        for m in models:
            key = f"{v}|{m}"
            lines.append(f"| {v} | {m} | {_pct(report['accuracy'].get(key))} | "
                         f"{_pct(report['self_accuracy'].get(key))} | "
                         f"${report['cost_usd'].get(key, 0):.4f} |")
    lines += ["", "## By probe kind", ""]
    for key, acc in sorted(report["by_kind"].items()):
        lines.append(f"- {key}: {_pct(acc)}")
    lines += ["", "## By category", ""]
    for key, acc in sorted(report["by_category"].items()):
        lines.append(f"- {key}: {_pct(acc)}")
    if report["misses"]:
        lines += ["", "## Misses", ""]
        for m in report["misses"]:
            lines.append(f"- {m['variant']} / {m['model']} / {m['fixture']}:{m['probe']} "
                         f"(seat {m['seat']}): expected {m['expected']}, "
                         f"answered {m['answered']}: \"{m['text']}\"")
    return "\n".join(lines)
