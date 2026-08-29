<!-- Thanks! Small, complete changes land fastest. -->

## What & why

<!-- One or two sentences. Link the issue if there is one. -->

## Checklist

- [ ] Tests green **keyless**: `env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY .venv/bin/python -m pytest tests/ -q`
- [ ] No real personal data anywhere in the diff; every name in it is invented (see the roster below)
- [ ] Frontend changed? `npm run build` in `frontend/` (the served dist must match)
- [ ] `changelog.d/` fragment in this same PR if this is user-visible; docs updated if they now lie
- [ ] New UI surfaces have a plain-English "what is this & why it matters" explainer

## Synthetic roster

The rule is invented names only, everywhere: code, tests, fixtures, and docs.
Prefer the roster below, so a reviewer can tell invented data from real data at
a glance. If you need a name it doesn't cover, invent an obviously fictional
one and add it here in the same PR.

Two casts are in use and they are kept apart deliberately. General examples
read better with plain first names. The eval corpora label people by their
role instead, because a memory fixture is easier to reason about when the name
itself states the relationship being tested.

| Kind | General examples | Eval corpora (`eval_critic/`, `eval_silence/`) |
| --- | --- | --- |
| People | Alex, Sam, Dave, Mateo | User, contact R, contact P |
| Organisations | AcmeCo, Initech, Globex | AcmeCo, BetaWorks |
| Places | Fairhaven | Meridian Falls, Cedar Hollow |

This is the fleet roster: membro and spendglass use the same cast, so a
reviewer moving between repos can tell invented data from real at a glance.

Infrastructure placeholders are fixed rather than free choice, because
`scripts/secret-scan.sh` allowlists exactly these and rejects anything else
of the same shape: `/Users/you/...`, `my-mac.my-tailnet.ts.net`,
`you@example.com`.
