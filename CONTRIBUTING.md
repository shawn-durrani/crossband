# Contributing

Crossband is solo-maintained and built primarily for the maintainer's
own use. Issues and PRs are welcome; response times vary.

## Setup

```sh
git clone https://github.com/shawn-durrani/crossband.git
cd crossband
./start.sh                                   # venv, deps, build, serve on 8902
env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY \
  .venv/bin/python -m pytest -q              # backend, keyless
node --test frontend/src/*.test.js           # frontend rules
```

Both suites must pass with no API keys set; CI runs them keyless. A
change that only works with a key needs a keyless fallback. The frontend
suites are a separate CI step, so a green pytest run is not the whole
gate.

## How work lands

Every change is a PR linked to its issue, CI green, landed by
squash-merge with `Fixes #N`. Branch from `main`, never off another open
PR: squash-merging the first would orphan the second.

## Ground rules

- Tests accompany behaviour changes. Rules belong in a pure `.js` module
  with a `node --test` suite, not inside a component.
- User-visible changes get a line in `CHANGELOG.md` under Unreleased.
- No real personal data in any diff: not in code, tests, fixtures, docs,
  screenshots or a demo database. That includes generated chat titles,
  which summarise whatever the chat actually discussed. Enable the leak
  scanner once per clone:

```sh
git config core.hooksPath .githooks
```

  Optionally copy `secret-scan-local.example` to `.secret-scan-local`
  (gitignored) with patterns for your own names and places. A deliberate
  keep can carry an inline `secret-scan: allow` marker naming why; it
  exempts that one line. A green scan covers key shapes, infrastructure
  identifiers and your deny-list. It is not a clearance: content must be
  synthetic by construction.
- Scope boundaries in [ARCHITECTURE.md](ARCHITECTURE.md) are deliberate.

## Releasing

Ordinary semantic versions in the 0.x range: no stability promise yet.

Before a tag, every box:

- [ ] Both suites green keyless
- [ ] `pip-audit -r requirements.txt --strict` clean, and
      `npm --prefix frontend audit --omit=dev --audit-level=high` clean.
      The lockfile lives in `frontend/`, so a bare `npm audit` from the
      root has nothing to audit and errors out.
- [ ] `bash scripts/secret-scan.sh --tree` green. The bare command scans
      staged lines only, so at release time it scans nothing and still
      reports clean; `--tree` is the one that looks.
- [ ] CHANGELOG entry dated, fresh `## Unreleased` left above it
