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

Budgets, not taste. `tests/test_doc_style.py` enforces the hard limits;
the rest is review. The house reference is this repo's README: a
15.5-word average sentence with 4% of sentences over 35 words.

- One claim per sentence. Average under 18 words, and keep sentences
  over 35 words under 10% of a document.
- No em-dashes. Australian English. Plain English over jargon.
- Caveats earn their own sentence. Appending a limitation to every claim
  is how the important ones stop reading as important.
- Antithesis ("X, not Y", "rather than", "instead of") is a tool, not a
  cadence. If deleting the "not Y" half loses no information, delete it.
- Never announce your own honesty. "Stated plainly", "the honest reason":
  delete the phrase, keep the fact.
- Issue numbers and bug history go in the CHANGELOG and the issue.
  Reference prose says what is true now. A test file is the exception:
  recording which bug a case guards is exactly what it is for.
- Do not narrate a document's own structure or edit history. Nobody read
  the previous version.
- A table cell holds a value and a sentence, not a section.
- Headings every 30 to 50 lines of prose, so a section can be navigated.
- Say a thing once. Two copies of a rule is one copy that will go stale.

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
