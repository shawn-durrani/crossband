"""The transcript shapes under test.

`current` is exactly what the app sends today, built by the real provider
builders: a seat's own turns as bare assistant turns, everyone else's as
labelled user turns. `self_labelled` keeps that layout but gives the seat's
own turns the same "[Name · time]:" head as everyone else's. `envelope` is
the #212 shape: other seats' turns wrapped in a machine envelope inside the
user role, only the owner kept as a plain labelled user turn, plus one
preamble line saying so."""

from backend import providers
from backend.providers import CONTINUE_NUDGE, _labelled_text

VARIANTS = ("current", "self_labelled", "envelope")
ENVELOPE_PREAMBLE = ("(Only {user} speaks as the user in this chat. Other members' "
                     "turns arrive inside <member> envelopes; your own earlier turns "
                     "are your assistant turns.)")


def _user_text(m, names, cfg):
    return _labelled_text(m, names, cfg)


def _envelope_text(m, names, cfg):
    name = names.get(m["speaker"], m["speaker"])
    ts = providers._iso(m.get("created_at"))
    at = f' at="{ts}"' if ts else ""
    return f'<member name="{name}"{at}>{m["content"].strip()}</member>'


def _walk(variant, family, self_slug, transcript, names, cfg):
    text_key = "text" if family == "anthropic" else "input_text"

    def user(text):
        return {"role": "user", "content": [{"type": text_key, "text": text}]}

    msgs = []
    if variant == "envelope":
        msgs.append(user(ENVELOPE_PREAMBLE.format(user=cfg["user_name"])))
    for m in transcript:
        if m["speaker"] == self_slug:
            body = (_labelled_text(m, names, cfg) if variant == "self_labelled"
                    else m["content"])
            msgs.append({"role": "assistant", "content": body})
        elif variant == "envelope" and m["speaker"] != "user":
            msgs.append(user(_envelope_text(m, names, cfg)))
        else:
            msgs.append(user(_user_text(m, names, cfg)))
    if not msgs or msgs[0]["role"] != "user":
        msgs.insert(0, user("(The group chat continues.)"))
    if msgs[-1]["role"] == "assistant":
        msgs.append(user(CONTINUE_NUDGE))
    return msgs


def project(variant, family, self_slug, transcript, names, cfg):
    """Messages (Anthropic) or input items (OpenAI Responses) for one seat."""
    if variant not in VARIANTS:
        raise ValueError(f"variant must be one of {VARIANTS}")
    if variant == "current":
        if family == "anthropic":
            return providers.build_anthropic_messages(self_slug, transcript, names, cfg)
        return providers.build_openai_input(self_slug, transcript, names, cfg)
    return _walk(variant, family, self_slug, transcript, names, cfg)
