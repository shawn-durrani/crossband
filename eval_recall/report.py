"""Renders scoring.aggregate output as markdown."""


def _pct(x):
    return "n/a" if x is None else f"{x * 100:.0f}%"


def _num(x, digits=2):
    return "n/a" if x is None else f"{x:.{digits}f}"


def render_markdown(report: dict, mock: bool) -> str:
    lines = ["# Recall replay report (crossband#252 harness)", ""]
    if mock:
        lines.append("MOCK RUN: a made-up ledger and a stand-in memory. Nothing "
                     "here is about your install.")
        lines.append("")
    lines.append(f"turns: {report['n_turns']} across {report['n_chats']} chats; "
                 f"turns with any fact: {report['turns_with_hits']}; "
                 f"facts returned: {report['hits_total']}; "
                 f"live count: {report['live_k']}; asked for: {report['top_k']}")
    s1, sa = report["score_top1"], report["score_all"]
    lines.append(f"score of the top fact: p10 {_num(s1['p10'])}, p50 {_num(s1['p50'])}, "
                 f"p90 {_num(s1['p90'])}; every fact: p10 {_num(sa['p10'])}, "
                 f"p50 {_num(sa['p50'])}, p90 {_num(sa['p90'])}")
    ms = report["recall_ms"]
    lines.append(f"recall latency: p50 {_num(ms['p50'], 0)} ms, p95 {_num(ms['p95'], 0)} ms")
    lines += ["", "## Floor sweep", "",
              "| floor | turns injecting | facts per turn | new facts per turn | share new |",
              "|---|---|---|---|---|"]
    for row in report["floors"]:
        lines.append(f"| {row['floor']:.2f} | {_pct(row['turns_injecting'])} | "
                     f"{_num(row['facts_per_turn'])} | {_num(row['new_facts_per_turn'])} | "
                     f"{_pct(row['share_new'])} |")
    lines += ["", "## By rank", "",
              "| rank | turns with a fact here | mean score | share new |",
              "|---|---|---|---|"]
    for row in report["ranks"]:
        lines.append(f"| {row['rank']} | {_pct(row['turns_with_hit'])} | "
                     f"{_num(row['mean_score'])} | {_pct(row['share_new'])} |")
    lines += ["", "## By chat", "", "| chat | turns | turns with a new fact |",
              "|---|---|---|"]
    for row in report["by_chat"]:
        lines.append(f"| {row['chat_id']} | {row['turns']} | {row['turns_with_new']} |")
    return "\n".join(lines) + "\n"
