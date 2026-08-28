# CLAUDE.md

Instructions for AI sessions working in this repository.

## Process

The pipeline is documented once, in [CONTRIBUTING.md](CONTRIBUTING.md).
Session-specific rules, for a session with write access working for the
maintainer:

- Merge your own PRs once CI is green; don't wait for human approval.
  The maintainer comments asynchronously. External contributors: the
  maintainer merges yours.
- Never commit directly to `main`, and never branch off another open PR.
- Both suites run keyless. Never add a hard dependency on an API key.

## Rules that override convenience

- `data/` holds real conversations. Never read, copy or quote its
  contents into code, tests, docs, commits or chat. Debug with a
  disposable data directory.
- No real personal data in a diff, including spend figures, chat titles,
  hostnames, paths, and the names of real people (household members,
  guests). This is paramount: field-test transcripts carry real names, so
  when a phrasing from one becomes a test or a comment, rename it to the
  synthetic roster (Alex, Sam, Dave, Mateo) FIRST. Fixtures are invented,
  not sampled. Personal deny-list patterns live ONLY in the gitignored
  `.secret-scan-local` (note the leading dot); a file named
  `secret-scan-local` without the dot is NOT ignored and must never be
  created.
- Rules live in pure `.js` modules with `node --test` suites. Behaviour
  buried in a component has no automated guard. One render smoke does
  run in CI (`frontend/scripts/render-smoke.mjs`); it mounts the real
  message list so a render crash cannot ship. Do not delete it.
- Every live message insert goes through `db.insert_message`. A raw
  insert elsewhere fails the build, and the guard is deliberate.
- Cost provenance is stamped at write time and never backfilled.
- Anything that spawns a process from request data is loopback-only,
  and stays gated on the request's own host even now that the browser
  gate exists: a session is not a licence to spawn from a remote host.

## Orientation

Read [ARCHITECTURE.md](ARCHITECTURE.md), then browse
[docs/README.md](docs/README.md), which indexes every document by what
you are trying to do. Read [docs/CONFIG.md](docs/CONFIG.md) for
settings, and [docs/GUEST_PERMISSIONS.md](docs/GUEST_PERMISSIONS.md)
before touching anything about summoned guests. Open issues hold the active work.
