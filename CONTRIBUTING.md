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
npm --prefix frontend test                    # lint, rules, render smoke
```

Both suites must pass with no API keys set; CI runs them keyless. A
change that only works with a key needs a keyless fallback. The frontend
command runs three gates: `eslint`, the `node --test` rule suites, and
the render smoke. CI runs the same three as separate steps, so a green
pytest run is not the whole gate.

## How work lands

Every change is a PR linked to its issue, CI green, landed by
squash-merge with `Fixes #N`. Branch from `main`, never off another open
PR: squash-merging the first would orphan the second.

## Ground rules

- Tests accompany behaviour changes. Rules belong in a pure `.js` module
  with a `node --test` suite, not inside a component.
- User-visible changes get one new file under `changelog.d/`, not an
  edit to `CHANGELOG.md`. Name it `<issue>-<slug>.md` and write the
  finished entry: one `- ` paragraph in the changelog's voice, with
  continuation lines indented two spaces. Entries fold into the
  changelog at release, so two open PRs never touch the same line.
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
  `scripts/secret-scan.sh` is the fleet's canonical scanner: membro and
  spendglass carry byte-identical copies, each guarded by a test that
  fails when the copy differs, so a pattern fix lands here first and is
  then copied across. Exclusions only this repo needs (the frontend
  lockfile, files that never ship) live in `.secret-scan-exclude`, one
  path per line, so the script itself stays identical everywhere.
- Scope boundaries in [ARCHITECTURE.md](ARCHITECTURE.md) are deliberate.

## Retiring code

Three retirements each stopped at the first green build and left
residue, so removal has its own checklist. When a feature, helper or
convention is retired:

- [ ] Delete the code, its exports, and any constant that existed only
      to serve it.
- [ ] Delete or rewrite the tests that pinned it. A green test for a
      dead rule reads as coverage of a live one.
- [ ] Sweep the comments and docstrings that name it, both sides of the
      frontend/backend boundary.
- [ ] Check the UI for branches that render the retired convention.
- [ ] Land the removal as its own PR. A removal that has to justify
      itself inside a feature PR stops at the first green build.

## Writing documentation

The docs are written in the fleet's voice, and
[docs/WRITING.md](docs/WRITING.md) says what that sounds like. Read the
seven examples there before you write or change a page. The test is one
line: read each sentence as if you're explaining the app to a smart
friend who's never seen it, and if you wouldn't say it like that,
rewrite it. README.md is the first doc rewritten that way, and the one
to measure the others against.

`tests/test_doc_style.py` checks the mechanical part. Every markdown
file in the repo is held to four ceilings: no em-dash, no sentence over
55 words, no table cell over 45 words, and a heading at least every 50
lines of prose. A doc rewritten in the voice is listed in `CONVERTED` at
the top of that file, and those docs are also held to the rules the
guide marks (CI):

- no dashes and no semicolons
- one colon per sentence, and only to introduce a list, a command or a
  quoted value
- bracketed asides under eight words, and no sentence starting with one
- capitals only for acronyms
- none of the filler words the guide names
- contrasts ("rather than", "X, not Y") kept rare
- no history and no issue numbers
- no pointers to the page itself
- no sentence opening with "So" or "Because"

When you rewrite a doc, add its path to `CONVERTED` in the same PR, and
the suite tells you what's left. Test files are different: a test names
the issue it guards.

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
- [ ] `python scripts/fold_changelog.py vX.Y.Z` run: `changelog.d/`
      empty, the new section dated, Unreleased left empty above it
