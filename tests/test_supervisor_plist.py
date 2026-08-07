"""The launchd supervisor is a placeholder template rendered
by ops/install-supervisor.sh at install time. These tests pin the two things
that make it safe and correct without needing macOS or launchd:

  1. The committed template carries NO real path - only placeholders - so no
     personal home directory can leak into the repo (the same discipline the
     leak scanner enforces).
  2. After substitution the result is a valid plist that actually configures a
     self-restarting, run-at-load agent pointing at start.sh.

Keyless and cross-platform: plistlib is stdlib, so this runs in CI on Linux
where `plutil` does not exist.
"""
import plistlib
import re
import stat
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TEMPLATE = REPO / "ops" / "dev.sideband.server.plist.template"
INSTALLER = REPO / "ops" / "install-supervisor.sh"

PLACEHOLDERS = ("{{REPO_DIR}}", "{{HOME}}", "{{PATH}}")


def _render(repo_dir="/opt/example/sideband", home="/home/example",
            path="/usr/bin:/bin"):
    text = TEMPLATE.read_text()
    return (text.replace("{{REPO_DIR}}", repo_dir)
                .replace("{{HOME}}", home)
                .replace("{{PATH}}", path))


def test_template_and_installer_present():
    assert TEMPLATE.exists(), "the plist template ships in ops/"
    assert INSTALLER.exists(), "the installer ships in ops/"


def test_template_has_no_real_path_only_placeholders():
    """No committed personal path: every machine-specific value is a
    placeholder the installer fills in. This is what keeps the leak scanner
    green and the template reusable."""
    text = TEMPLATE.read_text()
    assert "/Users/" not in text, "a real macOS home path leaked into the template"
    for ph in PLACEHOLDERS:
        assert ph in text, f"template lost its {ph} placeholder"


def test_installer_is_executable():
    mode = INSTALLER.stat().st_mode
    assert mode & stat.S_IXUSR, "install-supervisor.sh must be executable"


def test_rendered_plist_is_valid_and_self_restarting():
    rendered = _render()
    assert "{{" not in rendered, "a placeholder survived substitution"
    doc = plistlib.loads(rendered.encode())

    assert doc["Label"] == "dev.sideband.server"
    # the two keys that make it a supervisor: come up on load, stay up
    assert doc["RunAtLoad"] is True
    assert doc["KeepAlive"] is True
    # it launches the project's real entrypoint in the repo it was rendered for
    assert doc["ProgramArguments"][-1].endswith("/start.sh")
    assert doc["WorkingDirectory"] == "/opt/example/sideband"
    # a throttle so a hard boot failure can't spin
    assert int(doc["ThrottleInterval"]) >= 1


def test_installer_takes_over_cleanly_and_validates():
    """The installer must not blindly load: it boots out any prior agent,
    frees the port, and refuses to load a plist with a leftover placeholder."""
    src = INSTALLER.read_text()
    assert "launchctl bootout" in src, "must remove a prior agent before loading"
    assert "launchctl bootstrap" in src, "must load the new agent"
    assert re.search(r"grep -q '\{\{'", src), \
        "must fail if a placeholder was left unsubstituted"
    assert "plutil -lint" in src, "must validate the generated plist"


def test_every_test_suite_has_a_doc_entry():
    """docs/TESTING.md promises an entry per suite, and that promise drifted
    three times in one week - each time a PR added a file and the doc wasn't
    touched. Enforce the correspondence in both directions instead of trusting
    anyone to remember. (Exact test COUNTS are deliberately absent from the doc
    for the same reason: a hand-maintained number is a number that goes stale.)

    Covers BOTH suites. This checked only Python at first, and the unwatched
    half drifted exactly as predicted: 13 of 16 frontend suites had no entry,
    including load-bearing invariants (the cross-chat write-guard, the
    atomic-flip race, the barge-in leak). A guard that covers one half of the
    thing it guards teaches you to trust the other half for no reason."""
    import glob
    import re as _re
    doc = (REPO / "docs" / "TESTING.md").read_text()

    py = {Path(f).name for f in glob.glob(str(REPO / "tests" / "test_*.py"))}
    py_doc = set(_re.findall(r"`(test_[a-z_0-9]+\.py)`", doc))
    assert not py - py_doc, f"undocumented suites: {sorted(py - py_doc)}"
    assert not py_doc - py, f"documented but missing: {sorted(py_doc - py)}"

    js = {Path(f).name
          for f in glob.glob(str(REPO / "frontend" / "src" / "*.test.js"))}
    js_doc = set(_re.findall(r"`([A-Za-z0-9_]+\.test\.js)`", doc))
    assert not js - js_doc, f"undocumented frontend suites: {sorted(js - js_doc)}"
    assert not js_doc - js, f"documented but missing: {sorted(js_doc - js)}"

    assert not _re.search(r"\d+ tests across \d+ files", doc), (
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

    1. docs/README.md indexes EVERY document in docs/ - an index with holes
       is worse than none, because it teaches readers the unlisted files
       don't exist.
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
