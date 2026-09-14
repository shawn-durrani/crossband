"""Turns a reply into an answer and a run into a report."""

import re
from collections import defaultdict

from backend.config import DEFAULT_PRICING, compute_cost, price_for
from eval_attribution.schema import ProbeResult

_SELF_WORDS = ("me", "i did", "myself", "that was me", "i was")


def parse_answer(text: str | None, fixture, probe) -> str | None:
    """The first roster name (or the owner's name) in the reply, mapped to a
    slug; "me"-shaped replies map to the probed seat; yes/no for yesno
    probes. None when nothing recognisable came back."""
    if not text:
        return None
    low = text.lower()
    if probe.kind == "yesno":
        m = re.search(r"\b(yes|no)\b", low)
        return m.group(1) if m else None
    candidates = {fixture.user_name.lower(): "user"}
    candidates.update({m["name"].lower(): m["slug"] for m in fixture.roster})
    first, first_pos = None, None
    for name, slug in candidates.items():
        m = re.search(rf"\b{re.escape(name)}\b", low)
        if m and (first_pos is None or m.start() < first_pos):
            first, first_pos = slug, m.start()
    if first is None and any(w in low for w in _SELF_WORDS):
        return probe.seat
    return first


def score(fixture, probe, variant, model, completion, cfg) -> ProbeResult:
    expected = probe.resolve_expected()
    if completion.timed_out:
        failure, answered = "timeout", None
    elif completion.text is None:
        failure, answered = "missing_key", None
    else:
        answered = parse_answer(completion.text, fixture, probe)
        failure = "ok" if answered is not None else "unparsed"
    cost = None
    if completion.text is not None:
        pricing = cfg.get("pricing") or DEFAULT_PRICING
        if price_for(model, pricing):
            cost = compute_cost(model, {"input": completion.input_tokens,
                                        "output": completion.output_tokens}, pricing)
    return ProbeResult(
        fixture_id=fixture.id, category=fixture.category, probe_tag=probe.tag,
        seat=probe.seat, kind=probe.kind, variant=variant, model=model,
        expected=expected, answered=answered, correct=(answered == expected),
        failure_mode=failure, latency_s=completion.latency_s,
        input_tokens=completion.input_tokens, output_tokens=completion.output_tokens,
        cost_usd=cost, raw_text=(completion.text or "")[:200])


def _acc(rows):
    scored = [r for r in rows if r.failure_mode in ("ok", "unparsed")]
    if not scored:
        return None
    return sum(1 for r in scored if r.correct) / len(scored)


def aggregate(results: list[ProbeResult]) -> dict:
    by_variant_model = defaultdict(list)
    by_kind = defaultdict(list)
    by_category = defaultdict(list)
    self_probes = defaultdict(list)
    for r in results:
        by_variant_model[(r.variant, r.model)].append(r)
        by_kind[(r.variant, r.model, r.kind)].append(r)
        by_category[(r.variant, r.model, r.category)].append(r)
        if r.expected == r.seat:
            self_probes[(r.variant, r.model)].append(r)
    report = {
        "n_probes": len({(r.fixture_id, r.probe_tag) for r in results}),
        "n_results": len(results),
        "missing_key": sum(1 for r in results if r.failure_mode == "missing_key"),
        "timeouts": sum(1 for r in results if r.failure_mode == "timeout"),
        "unparsed": sum(1 for r in results if r.failure_mode == "unparsed"),
        "accuracy": {f"{v}|{m}": _acc(rows) for (v, m), rows in by_variant_model.items()},
        "self_accuracy": {f"{v}|{m}": _acc(rows) for (v, m), rows in self_probes.items()},
        "by_kind": {f"{v}|{m}|{k}": _acc(rows) for (v, m, k), rows in by_kind.items()},
        "by_category": {f"{v}|{m}|{c}": _acc(rows)
                        for (v, m, c), rows in by_category.items()},
        "cost_usd": {f"{v}|{m}": sum(r.cost_usd or 0 for r in rows)
                     for (v, m), rows in by_variant_model.items()},
        "latency_mean_s": {f"{v}|{m}": (sum(r.latency_s for r in rows) / len(rows))
                           for (v, m), rows in by_variant_model.items()},
        "misses": [{"fixture": r.fixture_id, "probe": r.probe_tag, "seat": r.seat,
                    "variant": r.variant, "model": r.model, "expected": r.expected,
                    "answered": r.answered, "text": r.raw_text}
                   for r in results if r.failure_mode == "ok" and not r.correct],
    }
    return report
