# Contributing

Crossband is maintained by one person and built first for their own
use. Issues and pull requests are welcome, and replies can take a
while.

## Setup

```sh
git clone https://github.com/shawn-durrani/crossband.git
cd crossband
./start.sh                                   # venv, deps, build, serve on 8902
env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY \
  .venv/bin/python -m pytest -q              # backend, keyless
npm --prefix frontend test                    # lint, rules, render smoke
```

Both suites have to pass with no API keys set, because CI runs them
keyless. If a change only works with a key, give it a keyless fallback.
The frontend command runs `eslint`, the `node --test` rule suites and
the render smoke, and CI runs each of those as its own step. A green
pytest run is part of the gate and never all of it.

## How work lands

Every change is a pull request linked to its issue. CI is green before
it lands, and it lands by squash merge with `Fixes #N` in the message.
Branch from `main`, never from another open pull request. Squash
merging the first one leaves the second with no parent.

## Ground rules

- Tests come with behaviour changes. A rule lives in a pure `.js`
  module with a `node --test` suite, never inside a component.
- A change a person can see gets one new file under `changelog.d/`,
  and `CHANGELOG.md` stays untouched. Name the file `<issue>-<slug>.md`
  and write the finished entry. That is one `- ` paragraph in the
  changelog's voice, with continuation lines indented two spaces. The entries fold
  into the changelog at release, so two open pull requests never touch
  the same line.
- No real personal data in any diff. That covers code, tests,
  fixtures, docs, screenshots and any demo database, and it covers
  generated chat titles too, because a title summarises whatever the
  chat discussed. Turn on the leak scanner once per clone:

```sh
git config core.hooksPath .githooks
```

  You can copy `secret-scan-local.example` to `.secret-scan-local`,
  which is gitignored, and fill it with patterns for your own names
  and places. A line you mean to keep can carry an inline
  `secret-scan: allow` marker that says why, and the marker exempts
  that one line. A green scan covers key shapes, infrastructure
  identifiers and your own deny list. It isn't a clearance, so write
  content that is made up from the start.
  `scripts/secret-scan.sh` is the fleet's one scanner. Membro and
  spendglass carry byte for byte copies, each guarded by a test that
  fails when the copy drifts, so a pattern fix lands here first and is
  then copied across. Exclusions only this repo needs, such as the
  frontend lockfile and files that never ship, go in
  `.secret-scan-exclude`, one path per line, so the script itself stays
  the same everywhere.
- The scope boundaries in [ARCHITECTURE.md](ARCHITECTURE.md) are
  chosen, and that page says why. Read it before you widen one.

## Retiring code

A removal that stops at the first green build leaves residue, so
retiring a feature, a helper or a convention has its own checklist.

- [ ] Delete the code, its exports, and any constant that existed only
      to serve it.
- [ ] Delete or rewrite the tests that pinned it. A green test for a
      dead rule reads as coverage of a live one.
- [ ] Sweep the comments and docstrings that name it, on both sides of
      the frontend and backend boundary.
- [ ] Check the UI for branches that still render the retired
      convention.
- [ ] Land the removal as its own pull request. A removal that has to
      justify itself inside a feature pull request stops at the first
      green build.

## Writing documentation

Write a page the way you'd explain the app to a smart friend who's
never seen it, and if you wouldn't say a sentence like that, rewrite it
until you would. Contractions are fine, the reader is "you", and short
words beat long ones. A paragraph is one thought, and it opens with its
point. When you bring in something from outside the app, say what it is
in a sentence and link its own documentation. Don't announce a count
before a list, and don't end a paragraph on a line that sounds good.
README.md is the page to measure against.

`tests/test_doc_style.py` checks the mechanical part. Every markdown
file in the repo is held to the same ceilings: no em-dash, no sentence
over 55 words, no table cell over 45 words, and a heading at least
every 50 lines of prose. A doc rewritten in the voice is listed in
`CONVERTED` near the top of that file, and those docs also keep to
these rules:

- no dashes and no semicolons
- one colon per sentence, and only to introduce a list, a command or a
  quoted value
- bracketed asides under eight words, and no sentence starting with one
- capitals only for acronyms
- none of the filler words the test names
- no sentence that announces a count before the list
- contrasts, such as "X, not Y", kept rare
- no history and no issue numbers
- no pointers to the page itself
- no sentence opening with "So" or "Because"

When you rewrite a doc, add its path to `CONVERTED` in the same pull
request, and the suite tells you what's left. Test files are
different, because a test names the issue it guards.

## Releasing

Versions are ordinary semantic versions in the 0.x range, with no
stability promise yet.

Before you tag, tick every box.

- [ ] Both suites green keyless.
- [ ] `pip-audit -r requirements.txt --strict` clean, and
      `npm --prefix frontend audit --omit=dev --audit-level=high` clean.
      The lockfile lives in `frontend/`, so a bare `npm audit` from the
      root finds nothing to audit and errors out.
- [ ] `bash scripts/secret-scan.sh --tree` green. The bare command scans
      staged lines only, so at release time it scans nothing and still
      reports clean, and `--tree` is the one that looks.
- [ ] `python scripts/fold_changelog.py vX.Y.Z` run, so `changelog.d/`
      is empty, the new section is dated, and the Unreleased heading
      over it stays empty.
