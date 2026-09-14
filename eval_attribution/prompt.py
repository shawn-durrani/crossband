"""Assembles one probe call: the real seat system prompt, the projected
transcript, and the probe as a final owner turn."""

import time

from backend.providers import split_system_prompt
from eval_attribution.projection import project


def seat_cfg(fixture, base_cfg: dict) -> dict:
    cfg = dict(base_cfg)
    cfg["user_name"] = fixture.user_name
    cfg["shared_instructions"] = ""
    return cfg


def system_prompt(fixture, seat_slug: str, cfg: dict) -> str:
    participant = {"name": fixture.names[seat_slug], "slug": seat_slug,
                   "system_prompt": ""}
    roster = [{"name": m["name"], "slug": m["slug"]} for m in fixture.roster]
    stable, volatile = split_system_prompt(participant, roster, cfg, None, "", False)
    return stable + ("\n" + volatile if volatile else "")


def answer_instruction(fixture, probe) -> str:
    if probe.kind == "yesno":
        return "Answer with one word, yes or no."
    options = ", ".join([fixture.user_name] + [m["name"] for m in fixture.roster])
    return (f"Answer with exactly one name from this list: {options}. "
            "If it was you, answer with your own name.")


def timed_transcript(fixture, start: float | None = None) -> list[dict]:
    """Fixtures carry no timestamps; give every turn one, a minute apart, so
    the labels the app renders carry a real time like the live ones."""
    t0 = start if start is not None else time.time() - 60 * (len(fixture.transcript) + 2)
    out = []
    for i, m in enumerate(fixture.transcript):
        out.append({"id": i + 1, "speaker": m["speaker"], "content": m["content"],
                    "created_at": t0 + 60 * i, "tool_events": [], "attachments": []})
    return out


def build_call(fixture, probe, variant: str, family: str, cfg: dict,
               start: float | None = None):
    transcript = timed_transcript(fixture, start)
    question = f"{probe.question} {answer_instruction(fixture, probe)}"
    transcript.append({"id": len(transcript) + 1, "speaker": "user",
                       "content": question,
                       "created_at": transcript[-1]["created_at"] + 60,
                       "tool_events": [], "attachments": []})
    messages = project(variant, family, probe.seat, transcript, fixture.names, cfg)
    return system_prompt(fixture, probe.seat, cfg), messages
