"""Runs one fixture through one critic model and scores the outcome.

Reuses backend.llm_util's cheap-model helper pattern (utility_complete's
routing, via utility_complete_with_usage for the extra telemetry) so critic
model choice is just a string — cfg["utility_model"] or an explicit override —
and swapping in a stronger model later is a one-line change."""

from dataclasses import dataclass

from backend.config import compute_cost, price_for
from backend.llm_util import UtilityCompletion, model_family, utility_complete_with_usage
from eval_critic.parse import parse_verdict
from eval_critic.prompt import build_prompt
from eval_critic.schema import FAILURE_POLICIES, Fixture, FixtureResult, ParsedVerdict


@dataclass
class CriticCallFn:
    """Wraps utility_complete_with_usage so tests can inject a fake without
    touching module globals. Defaults to the real helper."""
    fn: callable = utility_complete_with_usage

    async def __call__(self, prompt, cfg, max_tokens, model, timeout):
        return await self.fn(prompt, cfg, max_tokens=max_tokens, model=model, timeout=timeout)


DEFAULT_CALLER = CriticCallFn()


def _apply_failure_policy(failure_policy: str) -> tuple[str, ParsedVerdict]:
    """A call that timed out, hit a missing key, or returned malformed output
    has no trustworthy verdict. fail_open lets the draft through (matches the
    live-path default proposed in  — availability over enforcement, but
    every miss is recorded); fail_closed treats it as a block. Neither verdict
    carries real cited evidence — that's the point of tagging failure_mode
    separately in the result."""
    if failure_policy == "fail_open":
        return "allow", ParsedVerdict(verdict="allow", malformed=True,
                                       malformed_reason="fail-open default applied")
    return "contradicted", ParsedVerdict(verdict="contradicted", malformed=True,
                                          malformed_reason="fail-closed default applied")


async def evaluate_fixture(fixture: Fixture, critic_model: str, cfg: dict,
                            timeout_s: float | None = 8.0,
                            latency_budget_s: float | None = 2.5,
                            failure_policy: str = "fail_open",
                            caller=DEFAULT_CALLER) -> FixtureResult:
    if failure_policy not in FAILURE_POLICIES:
        raise ValueError(f"failure_policy must be one of {FAILURE_POLICIES}")

    prompt = build_prompt(fixture)
    result: UtilityCompletion = await caller(
        prompt, cfg, max_tokens=300, model=critic_model, timeout=timeout_s)

    if result.timed_out:
        failure_mode = "timeout"
        applied_verdict, parsed = _apply_failure_policy(failure_policy)
    elif result.text is None:
        failure_mode = "missing_key"
        applied_verdict, parsed = _apply_failure_policy(failure_policy)
    else:
        parsed = parse_verdict(result.text, fixture)
        if parsed.malformed:
            failure_mode = "malformed"
            applied_verdict, _ = _apply_failure_policy(failure_policy)
        else:
            failure_mode = "ok"
            applied_verdict = parsed.verdict

    expected_safe = fixture.expected_verdict == "allow"
    applied_safe = applied_verdict == "allow"
    if expected_safe and applied_safe:
        outcome = "TN"
    elif expected_safe and not applied_safe:
        outcome = "FP"
    elif not expected_safe and not applied_safe:
        outcome = "TP"
    else:
        outcome = "FN"

    pricing = cfg.get("pricing") or {}
    cost = None
    if price_for(critic_model, pricing):
        cost = compute_cost(critic_model, {"input": result.input_tokens,
                                            "output": result.output_tokens}, pricing)

    exceeded = bool(latency_budget_s is not None and result.latency_s > latency_budget_s)

    return FixtureResult(
        fixture_id=fixture.id,
        category=fixture.category,
        is_control=fixture.is_control,
        author_model_family=fixture.author_model_family,
        critic_model=critic_model,
        critic_model_family=model_family(critic_model),
        expected_verdict=fixture.expected_verdict,
        parsed=parsed,
        failure_mode=failure_mode,
        applied_verdict=applied_verdict,
        outcome=outcome,
        verdict_exact_match=(applied_verdict == fixture.expected_verdict),
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        latency_s=result.latency_s,
        cost_usd=cost,
        exceeded_latency_budget=exceeded,
    )
