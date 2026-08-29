"""Documentation style budgets, enforced (#119).

The docs drifted into a house style nobody chose: 73-word average sentences,
caveats stapled to every claim, bug history in reference prose. None of it
was catchable by review because none of it was measurable, so it accumulated
a paragraph at a time.

These are the hard limits only. The full rules are in CONTRIBUTING.md under
"Writing documentation"; taste is not automatable and is not attempted here.
What this stops is the shape that made the docs unreadable: a sentence that
never ends, a table cell that swallowed a section, a file with no way in.

Prose measurement excludes fenced code blocks, tables and headings, so a
long SQL line or a wide table never trips a prose budget.

The repo's completeness guards live here too: the docs index and config
reference stay whole, every test suite documents itself, and the frontend
rebuild gate watches its whole manifest. They moved here from
test_supervisor_plist.py, a file named for launchd plists, where nobody
would look for them.
"""
from pathlib import Path
import re

REPO = Path(__file__).resolve().parents[1]

# Vendored, generated, or not ours.
SKIP_DIRS = {".venv", "venv", "node_modules", ".git", ".pytest_cache",
             "__pycache__", "data", "dist", "build", "site-packages"}

MAX_SENTENCE_WORDS = 55
MAX_CELL_WORDS = 45
HEADING_EVERY = 50
HEADING_EXEMPT_UNDER = 150


def tracked_docs():
    """Every markdown file that is ours, longest first for readable failures."""
    out = []
    for path in REPO.rglob("*.md"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        out.append(path)
    return sorted(out)


LIST_ITEM = re.compile(r"^\s*(?:[-*+]\s|\d+[.)]\s)")


def prose_blocks(text):
    """Body prose, split into the units a reader actually reads: one paragraph
    or one list item each. No fenced code, tables, headings, or indented
    blocks.

    Blocking matters. A bullet list rarely ends its items with a full stop, so
    joining the whole document into one string turns a fifteen-item dependency
    list into a single 98-word 'sentence' and reports a file that is fine.
    Lines inside one block are joined, so a sentence wrapped across source
    lines is still measured whole."""
    blocks, current, in_fence = [], [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        stripped = line.strip()
        skip = (not stripped or stripped.startswith(("|", "#", ">"))
                or line.startswith(("    ", "\t")))
        if skip or LIST_ITEM.match(line):
            if current:
                blocks.append(" ".join(current))
                current = []
            if skip:
                continue
            stripped = LIST_ITEM.sub("", line).strip()
        current.append(stripped)
    if current:
        blocks.append(" ".join(current))
    return blocks


def sentences_of(block):
    """Split one block on sentence-final punctuation. Emphasis markers are
    stripped first: `**...ends here.** Next one` puts the asterisks between
    the full stop and the space, which otherwise hides two sentences inside
    one measurement. Abbreviations and decimals are protected for the same
    reason, in the other direction."""
    guarded = re.sub(r"[*_]{1,3}", "", block)
    for abbr in ("e.g.", "i.e.", "etc.", "cf.", "vs.", "Dr.", "Mr.", "Ms.",
                 "St.", "approx.", "Fig.", "no."):
        guarded = guarded.replace(abbr, abbr.replace(".", "\x00"))
    guarded = re.sub(r"(\d)\.(\d)", lambda m: m.group(1) + "\x00" + m.group(2),
                     guarded)
    parts = re.split(r"(?<=[.!?])\s+", guarded)
    return [p.replace("\x00", ".") for p in parts]


def word_count(text):
    """Words a reader actually parses. Inline code spans and link targets are
    one token each: a bare URL is not ten words of prose."""
    text = re.sub(r"`[^`]*`", " CODE ", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", " URL ", text)
    return len(text.split())


def test_no_em_dashes():
    """House style, and the one rule that is purely mechanical. A stray
    em-dash in a sample payload is worse than one in prose: it gets copied
    into other people's code."""
    offenders = []
    for path in tracked_docs():
        for n, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
            if "—" in line:
                offenders.append(f"{path.relative_to(REPO)}:{n}")
    assert not offenders, (
        "em-dash is not house style; use a comma, a colon, or two sentences:\n  "
        + "\n  ".join(offenders))


def test_no_runaway_sentences():
    """A sentence past this length has stopped being one claim. The record
    before this test existed was 1,004 words."""
    offenders = []
    for path in tracked_docs():
        for block in prose_blocks(path.read_text(errors="ignore")):
            for sentence in sentences_of(block):
                n = word_count(sentence)
                if n > MAX_SENTENCE_WORDS:
                    offenders.append(
                        f"{path.relative_to(REPO)}: {n} words - {sentence[:90]}...")
    assert not offenders, (
        f"sentences over {MAX_SENTENCE_WORDS} words ({len(offenders)}); "
        "split them:\n  " + "\n  ".join(offenders[:20]))


def test_no_essays_in_table_cells():
    """A reference table is scanned, not read. Past this length the cell has
    become a section and belongs in prose with a link."""
    offenders = []
    for path in tracked_docs():
        in_fence = False
        for n, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
            if line.lstrip().startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence or not line.strip().startswith("|"):
                continue
            for cell in line.split("|"):
                words = word_count(cell)
                if words > MAX_CELL_WORDS:
                    offenders.append(
                        f"{path.relative_to(REPO)}:{n}: {words} words in one cell")
    assert not offenders, (
        f"table cells over {MAX_CELL_WORDS} words; move the detail into prose "
        "and link it:\n  " + "\n  ".join(offenders))


def test_long_docs_stay_navigable():
    """An unbroken wall has no way in. The worst case before this test was a
    335-line section under a single heading.

    CHANGELOG.md is exempt, and the exemption is about genre rather than
    convenience: a changelog's headings are its releases, so the gap between
    two of them is however much shipped in between. Forcing one every 50 lines
    would mean inventing headings that describe nothing. Its entries are still
    held to the sentence budget, which is where changelog prose actually goes
    wrong."""
    offenders = []
    for path in tracked_docs():
        if path.name == "CHANGELOG.md":
            continue
        lines = path.read_text(errors="ignore").splitlines()
        if len(lines) < HEADING_EXEMPT_UNDER:
            continue

        # Only PROSE lines count toward a gap. A fenced block, a table and a
        # list are all navigable already: a numbered procedure is indexed by
        # its own numbers, and a table by its rows. Counting them would push
        # a heading into the middle of a 1..6 list, which renumbers it from 1
        # and makes the document worse to satisfy the guard.
        marks, in_fence, prose = [(0, 0)], False, 0
        for n, line in enumerate(lines, 1):
            if line.lstrip().startswith("```"):
                in_fence = not in_fence
                continue
            if not in_fence and re.match(r"#{2,6}\s", line):
                marks.append((n, prose))
                continue
            stripped = line.strip()
            skip = (in_fence or not stripped or stripped.startswith(("|", ">"))
                    or LIST_ITEM.match(line) or line.startswith(("    ", "\t")))
            if not skip:
                prose += 1
        marks.append((len(lines), prose))
        widest, where = 0, 0
        for (_, pa), (nb, pb) in zip(marks, marks[1:]):
            if pb - pa > widest:
                widest, where = pb - pa, nb
        if widest > HEADING_EVERY:
            offenders.append(
                f"{path.relative_to(REPO)}: {widest} lines of unbroken prose "
                f"ending near line {where}")
    assert not offenders, (
        f"add a heading at least every {HEADING_EVERY} lines:\n  "
        + "\n  ".join(offenders))


def test_every_test_suite_documents_itself():
    """Every suite must say what it covers, in the file itself.

    This used to be a per-file index in docs/TESTING.md, and that index drifted
    three times in one week - each time a PR added a file and the doc wasn't
    touched. The guard stopped the drift and left a worse problem: 340 lines of
    the doc restated descriptions the test files already carried, so the same
    fact lived in two places and the doc that was meant to say what the suites
    GUARANTEE had become a file listing.

    So the requirement moved to where a developer reading the code already
    looks. Same promise, no second copy: nothing is undocumented, and the doc
    is free to talk about guarantees.

    Covers BOTH suites. The index checked only Python at first, and the
    unwatched half drifted exactly as predicted: 13 of 16 frontend suites had
    no entry, including load-bearing invariants (the cross-chat write-guard,
    the atomic-flip race, the barge-in leak). A guard that covers one half of
    the thing it guards teaches you to trust the other half for no reason."""
    import ast
    import glob

    bare = []
    for f in sorted(glob.glob(str(REPO / "tests" / "test_*.py"))):
        doc = ast.get_docstring(ast.parse(Path(f).read_text()))
        if not doc or not doc.strip():
            bare.append(Path(f).name)
    assert not bare, (
        "these suites have no module docstring saying what they cover: "
        f"{bare}")

    # The frontend suites are plain JS, so the convention is a comment block
    # at the top rather than a docstring. Same requirement, different syntax.
    bare_js = []
    for f in sorted(glob.glob(str(REPO / "frontend" / "src" / "*.test.js"))):
        head = "\n".join(Path(f).read_text().splitlines()[:3])
        if not re.search(r"^\s*(//|/\*)", head, re.M):
            bare_js.append(Path(f).name)
    assert not bare_js, (
        "these frontend suites have no header comment saying what they cover: "
        f"{bare_js}")

    doc = (REPO / "docs" / "TESTING.md").read_text()
    assert not re.search(r"\d+ tests across \d+ files", doc), (
        "docs/TESTING.md should not hardcode a test count - it goes stale")


def test_frontend_rebuild_gate_watches_the_manifest_not_just_src():
    """start.sh only rebuilds the SPA when something newer than `dist` exists,
    and that freshness check has to include the MANIFEST and BUILD CONFIG -
    not just the source tree.

    It watched `frontend/src` and `frontend/index.html` only, and deploying the
    React 19 upgrade found out what that costs: a pull changing just
    package.json touches neither, so the gate declared dist current, skipped
    the whole block - and with it the `npm install` - leaving the service
    running the previous bundle against the previous node_modules. Nothing
    errored. Stale code served silently is worse than a failed build, because
    every symptom points at the application rather than the deploy.

    Pinned here because the failure is invisible: no test, no log line and no
    exit code changes if an entry quietly drops off this list again."""
    src = (REPO / "start.sh").read_text()
    # The gate wraps across lines, so read the whole `find ... -print -quit`.
    start = src.index("find frontend/src")
    # Only the paths BEFORE `-newer` are inputs; what follows it is the
    # comparison target (frontend/dist), which is a build artifact and is
    # gitignored - sweeping it into the checks below fails in CI, where it has
    # never been built.
    watched = src[start:src.index("-newer", start)]
    for required in ("frontend/src", "frontend/index.html",
                     "frontend/package.json", "frontend/package-lock.json",
                     "frontend/vite.config.js"):
        assert required in watched, (
            f"start.sh's rebuild gate must watch {required} - otherwise a change "
            f"to it leaves the service serving a stale bundle")
    # Every watched input must exist, or `find` errors into /dev/null and the
    # gate silently degrades to "never rebuild" - the same failure by a
    # different route.
    for path in watched.split():
        if path.startswith("frontend/"):
            assert (REPO / path).exists(), f"{path} is watched but does not exist"
    assert "npm install" in src and "npm run build" in src, (
        "the gate exists to run both; installing without building (or vice "
        "versa) still leaves dist and node_modules out of step")


def test_docs_index_and_config_reference_stay_complete():
    """The docs entry point makes two standing promises, both enforced
    here so they cannot silently rot the way the un-guarded halves of this
    file's sibling checks did:

    1. docs/README.md indexes EVERY document - an index with holes is worse
       than none, because it teaches readers the unlisted files don't exist.
       That covers docs/*.md and the eval harness READMEs: the first draft
       globbed only docs/, and eval_silence/README.md (148 lines) sat
       unlinked from anywhere for a month (#245).
    2. docs/CONFIG.md mentions EVERY Settings field - it advertises itself
       as code-derived, and a settings reference that silently lags the code
       is the config.local.json documentation gap this exists to close.
    Plus the entry-point wiring: README.md and CLAUDE.md must link the index,
    or it is invisible to exactly the audiences it was written for."""
    import glob
    docs_index = (REPO / "docs" / "README.md").read_text()
    for f in glob.glob(str(REPO / "docs" / "*.md")):
        name = Path(f).name
        if name == "README.md":
            continue
        # A real link target, not a mere mention: links inside docs/README.md
        # are sibling-relative, so the href is exactly "(NAME.md" (a first
        # draft of this accepted the name anywhere and passed on link TEXT
        # while the href was broken - caught by negative-testing the guard).
        assert f"({name}" in docs_index, (
            f"docs/{name} is not linked from docs/README.md; index it")
    for f in glob.glob(str(REPO / "eval_*" / "README.md")):
        rel = Path(f).relative_to(REPO).as_posix()
        # Links that leave docs/ are ../-relative, so the href is checked
        # whole for the same reason as above.
        assert f"(../{rel}" in docs_index, (
            f"{rel} is not linked from docs/README.md; index it")

    import sys
    sys.path.insert(0, str(REPO))
    from backend.config import Settings
    config_doc = (REPO / "docs" / "CONFIG.md").read_text()
    undocumented = [f for f in Settings.model_fields if f"`{f}`" not in config_doc]
    assert not undocumented, (
        f"Settings fields missing from docs/CONFIG.md: {undocumented}")

    assert "docs/README.md" in (REPO / "README.md").read_text(), (
        "README.md must link the docs index")
    assert "docs/README.md" in (REPO / "CLAUDE.md").read_text(), (
        "CLAUDE.md must link the docs index, or no AI session ever sees it")
    # The example template ships and stays synthetic (secret-scan also covers it).
    assert (REPO / "config.local.json.example").exists()
