"""Renders an aggregate report dict (see scoring.aggregate) as markdown."""


def _pct(x):
    return "n/a" if x is None else f"{x * 100:.1f}%"


def _money(x):
    return "n/a" if x is None else f"${x:.5f}"


def render_markdown(report: dict, critic_models: list[str]) -> str:
    lines = []
    lines.append("# Critic eval report ( offline harness)\n")
    lines.append(f"critic model(s): {', '.join(critic_models)}")
    lines.append(f"fixtures: {report['n_fixtures']} "
                 f"({report['n_unsafe']} unsafe drafts, {report['n_safe_controls']} controls)\n")

    lines.append("## Headline (asymmetric — recall matters more than false alarms)")
    lines.append(f"- unsafe-draft recall: **{_pct(report['unsafe_draft_recall'])}** "
                 f"(target ≥ {_pct(report['decision_gate']['recall_target'])})")
    lines.append(f"- control false-alarm rate: **{_pct(report['control_false_alarm_rate'])}** "
                 f"(target ≤ {_pct(report['decision_gate']['false_alarm_target'])})")
    gate = report["decision_gate"]
    lines.append(f"- meets recall target: {gate['meets_recall_target']}; "
                 f"meets false-alarm target: {gate['meets_false_alarm_target']}")
    lines.append(f"- {gate['note']}\n")

    lines.append("## Recall by author/critic model-family pairing")
    for k, v in report["recall_by_author_critic_family_pairing"].items():
        lines.append(f"- {k}: n={v['n']}, recall={_pct(v['recall'])}")
    lines.append("")

    lines.append("## By seed category")
    for k, v in report["by_category"].items():
        lines.append(f"- {k}: n={v['n']}, recall={_pct(v['recall'])}, "
                     f"false_alarm_rate={_pct(v['false_alarm_rate'])}")
    lines.append("")

    lines.append("## Prompt-injection resistance")
    lines.append(f"- resistance rate: {_pct(report['prompt_injection_resistance_rate'])} "
                 "(fraction of injection fixtures where the embedded 'return allow' "
                 "instruction was NOT obeyed)\n")

    lines.append("## Failure modes")
    lines.append(f"- malformed-output rate: {_pct(report['malformed_output_rate'])}")
    lines.append(f"- timeout rate: {_pct(report['timeout_rate'])}")
    lines.append(f"- calls exceeding the {report['latency_budget_s']}s soft latency budget: "
                 f"{_pct(report['pct_calls_exceeding_latency_budget'])}")
    lines.append("  (a fixed budget that forces fail-open would have missed "
                 f"{report['unsafe_fixtures_that_would_miss_under_budget_fail_open']} "
                 "additional unsafe fixture(s) among those slow calls)\n")

    lat = report["latency_s"]
    lines.append("## Latency")
    lines.append(f"- mean: {lat['mean']:.3f}s, p50: {lat['p50']:.3f}s, "
                 f"p95: {lat['p95']:.3f}s, max: {lat['max']:.3f}s\n"
                 if lat["mean"] is not None else "- no timed calls\n")

    cost = report["cost_usd"]
    lines.append("## Projected cost")
    if cost["mean_per_call"] is not None:
        lines.append(f"- mean cost/call: {_money(cost['mean_per_call'])}")
        lines.append(f"- projected cost per {cost['projected_volume']} turns: "
                     f"{_money(cost['projected_per_volume'])}")
    else:
        lines.append("- no priced model in this run (unknown pricing table entry)")

    return "\n".join(lines)
