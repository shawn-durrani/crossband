"""Guardrail for scripts/secret-scan.sh, the ONE scanner that runs both as the
local pre-commit hook and, through this test, in CI. Keyless: it only shells out
to bash + git, no network, no provider keys.

Three leak classes are asserted independently:
  · SECRETS                       real credential shapes
  · PERSONAL / INFRA IDENTIFIERS  real *.ts.net hosts, home paths, emails
  · PERSONAL CONTENT              regexes from a gitignored local deny-list
with documented synthetic placeholders explicitly allowed through.

The `--tree` case is the CI enforcement path: it proves the committed tree is
clean, so a PR that adds a real identifier turns this test red.

Every invocation pins SECRET_SCAN_LOCAL to an explicit path. Without that, the
personal-content class would read a real .secret-scan-local on a maintainer's
machine and no file at all in CI, so the same test would check different things
in the two places.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCANNER = REPO / "scripts" / "secret-scan.sh"
FIXTURES = REPO / "tests" / "fixtures" / "identifiers"

# A path that cannot exist, so the personal-content class is deliberately
# absent unless a test supplies its own deny-list.
NO_DENYLIST = "/nonexistent/.secret-scan-local"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None,
    reason="secret-scan.sh needs bash + git",
)


def _env(local_list=None):
    env = dict(os.environ)
    env["SECRET_SCAN_LOCAL"] = local_list or NO_DENYLIST
    return env


def run(*args, local_list=None):
    p = subprocess.run(["bash", str(SCANNER), *args], cwd=REPO,
                       capture_output=True, text=True, env=_env(local_list))
    return p.returncode, p.stdout + p.stderr


# ── the shape-based matchers, in isolation ───────────────────────────────────

def test_clean_placeholders_pass():
    code, out = run("--files", str(FIXTURES / "clean.txt"))
    assert code == 0, out
    assert "clean" in out


def test_real_identifiers_are_rejected():
    code, out = run("--files", str(FIXTURES / "leaky.txt"))
    assert code == 1
    assert "IDENTIFIER" in out
    # every real-looking token is named back to the author
    for tok in ("acmecorp.ts.net", "laptop-7f3a2b.ts.net",
                "/Users/jsmith", "/home/jsmith",
                "jane.smith@fastmail.dev", "j.smith.1988@gmail.com"):
        assert tok in out, tok


def test_credential_shapes_are_rejected():
    code, out = run("--files", str(FIXTURES / "secret.txt"))
    assert code == 1
    assert "CREDENTIAL" in out


def test_secrets_and_identifiers_are_distinct_classes():
    """The scanner must name the distinction, not collapse it into 'secrets'."""
    _, out = run("--files", str(FIXTURES / "leaky.txt"),
                 str(FIXTURES / "secret.txt"))
    assert "CREDENTIAL" in out and "IDENTIFIER" in out


# ── CI enforcement: the whole committed tree must be clean ───────────────────

def test_tree_scan_is_clean():
    code, out = run("--tree")
    assert code == 0, f"a real identifier/secret is committed:\n{out}"


# ── per-token truth table (real fails, placeholder passes) ───────────────────

REAL = [
    "host is corp-vpn.ts.net today",
    # A placeholder tailnet suffix does not launder a real machine name in
    # front of it: the allowlist matches the WHOLE token, so masking only the
    # part that looked sensitive still leaks the host label.
    "host is pro.tailXXXX.ts.net",
    "log path /Users/alice/Library/Logs",
    "linux path /home/bob/.cache",
    "ping alice.walker@outlook.com for access",
]
PLACEHOLDER = [
    "host is my-mac.my-tailnet.ts.net",
    "host is <mac>.<tailnet>.ts.net",
    "host is my-mac.tailXXXX.ts.net",
    "host is tailXXXX.ts.net",
    "log path /Users/you/Library/Logs",
    "linux path /home/you/.cache",
    "email you@example.com or 9+you@users.noreply.github.com",
    # Decorator-shaped identifiers that don't match real emails after stripping diff +
    # prefix (regression: @pytest.fixture once false-positived as +@pytest.fixture)
    "@pytest.fixture",
    "@dataclass.field",
    "@contextmanager.async_ctx",
]


def test_files_path_prefix_is_not_scanned(tmp_path):
    """The caller's own file PATH must never be scanned as content. --files
    labels each line with the file it came from; if that label is the absolute
    path, a real home dir (/Users/<name>) rides in and trips the identifier
    matcher on the machine running the scan: green in CI (/home/runner is
    allowlisted), red on any developer's Mac. This fakes that hostile path so
    the guard holds on every machine, CI included. Regression for the
    post-merge failure of test_clean_placeholders_pass."""
    home = tmp_path / "Users" / "realperson"
    home.mkdir(parents=True)
    f = home / "clean.txt"
    f.write_text("host is my-mac.my-tailnet.ts.net\n")
    code, out = run("--files", str(f))
    assert code == 0, out


@pytest.mark.parametrize("line", REAL)
def test_each_real_identifier_fails(tmp_path, line):
    f = tmp_path / "s.txt"
    f.write_text(line + "\n")
    code, out = run("--files", str(f))
    assert code == 1, out


@pytest.mark.parametrize("line", PLACEHOLDER)
def test_each_placeholder_passes(tmp_path, line):
    f = tmp_path / "s.txt"
    f.write_text(line + "\n")
    code, out = run("--files", str(f))
    assert code == 0, out


def test_staged_diff_decorator_pattern_passes(tmp_path):
    """Regression: the email regex with required alphanumeric local part prevents
    false positives on decorator-shaped identifiers in git staged diffs. This test
    creates a temp git repo, stages a file with @pytest.fixture, and verifies the
    scanner passes in staged mode (default behaviour without arguments). The old
    regex [A-Za-z0-9._%+-]+ would match +@pytest as +<local>@<domain>, but the
    fixed regex [A-Za-z0-9][A-Za-z0-9._%+-]* requires the first char to be
    alphanumeric, blocking symbol-only prefixes like +."""
    import subprocess
    repo_dir = tmp_path / "test_repo"
    repo_dir.mkdir()

    # Initialise git repo
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir,
                   check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir,
                   check=True, capture_output=True)

    # Create and commit initial file
    (repo_dir / "initial.txt").write_text("initial\n")
    subprocess.run(["git", "add", "initial.txt"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True, capture_output=True)

    # Stage a new file with decorator
    (repo_dir / "code.py").write_text("@pytest.fixture\ndef test_x(): pass\n")
    subprocess.run(["git", "add", "code.py"], cwd=repo_dir, check=True, capture_output=True)

    # Run scanner in staged mode, directly with correct cwd
    p = subprocess.run(["bash", str(SCANNER)], cwd=repo_dir,
                       capture_output=True, text=True, env=_env())
    # Should pass: decorator patterns no longer match the email regex
    assert p.returncode == 0, f"Staged diff with decorator should pass:\n{p.stdout}\n{p.stderr}"


def test_genuine_emails_still_caught():
    """Verify the tightened regex still catches real email-shaped identifiers
    while rejecting decorator-shaped and symbol-only patterns."""
    # Test cases: (line, should_fail)
    cases = [
        ("contact jane.smith@fastmail.dev for help", True),   # real email, should fail
        ("use name+tag@domain.dev for filtering", True),      # plus-addressing with real domain, should fail
        ("no email here", False),                              # no email, should pass
        ("+@pytest.fixture in the docs", False),              # symbol-only local, should pass (no match)
        ("@dataclass.field is a decorator", False),           # no alnum before @, should pass
    ]
    for line, should_fail in cases:
        f = REPO / "test_tmp_email.txt"
        f.write_text(line + "\n")
        code, out = run("--files", str(f))
        f.unlink()
        if should_fail:
            assert code == 1, f"Should catch '{line}' but passed"
        else:
            assert code == 0, f"Should not catch '{line}' but failed: {out}"


# ── the gate can detect, not just pass ───────────────────────────────────────
#
# `test_tree_scan_is_clean` above asserts a clean tree scans clean, which is
# EXACTLY what a --tree mode scanning zero bytes would also produce. Every other
# test here drives --files or staged mode, so nothing proved the tree walk can
# find anything. That is the same silently-green failure this guard was written
# about, moved one level down: the release gate could rot without a test going
# red. These plant a known-bad file in a real tree and prove both halves.

def _planted(text):
    """Assemble leak-shaped strings at RUNTIME, never as literals.

    A test file that CONTAINS a real-shaped key or a real-looking home path is
    itself a leak the scanner would have to be taught to ignore, and an
    exclusion is one more thing that can silently stop matching. Building the
    strings from fragments keeps this file clean by construction, so it needs no
    special case in the scanner and stays honest if the excludes are ever
    changed."""
    return text


FAKE_SECRET = _planted("sk-" + "ant-" + "api03-" + "B" * 32)
FAKE_HOMEPATH = _planted("/Users/" + "notarealperson")


def _tree_repo(tmp_path, contents):
    """A real git repo with `contents` COMMITTED. `git ls-files` only lists
    tracked files, so an untracked plant would prove nothing."""
    repo = tmp_path / "planted"
    repo.mkdir()
    for cmd in (["git", "init", "-q"],
                ["git", "config", "user.email", "you@example.com"],
                ["git", "config", "user.name", "Test"]):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)
    (repo / "config.py").write_text(contents + "\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "plant"], cwd=repo,
                   check=True, capture_output=True)
    return repo


def _scan(repo, *args, local_list=None):
    p = subprocess.run(["bash", str(SCANNER), *args], cwd=repo,
                       capture_output=True, text=True, env=_env(local_list))
    return p.returncode, p.stdout + p.stderr


def test_tree_scan_actually_detects_a_planted_secret(tmp_path):
    code, out = _scan(_tree_repo(tmp_path, f"KEY = '{FAKE_SECRET}'"), "--tree")
    assert code == 1, f"--tree missed a committed credential:\n{out}"
    assert "CREDENTIAL" in out


def test_tree_scan_actually_detects_a_planted_identifier(tmp_path):
    code, out = _scan(_tree_repo(tmp_path, f"LOG = '{FAKE_HOMEPATH}/log'"), "--tree")
    assert code == 1, f"--tree missed a committed identifier:\n{out}"
    assert "IDENTIFIER" in out


def test_the_bare_invocation_trap_is_real_and_pinned(tmp_path):
    """The reason RELEASING.md insists on `--tree`, in executable form.

    On the SAME repo that --tree correctly rejects, the no-argument invocation
    reports clean, because at release time nothing is staged, so it inspects
    zero bytes. If a future change ever makes bare mode scan the tree, this test
    fails and the docs telling people to avoid it become the stale thing."""
    repo = _tree_repo(tmp_path, f"KEY = '{FAKE_SECRET}'")
    assert _scan(repo, "--tree")[0] == 1          # the committed secret IS there
    code, out = _scan(repo)                        # …and bare mode sails past it
    assert code == 0
    assert "clean" in out


# ── class 3: personal content, from the gitignored local deny-list ───────────
#
# Names, places, vendors, project code names, and a bare username with no
# leading slash have no shape the other two classes can match. They are only
# findable once someone names them, which is what .secret-scan-local is for.
# The deny-list is personal data itself, so it never ships and CI cannot run
# this class; these tests supply their own throwaway list instead.

def test_local_denylist_catches_personal_content(tmp_path):
    deny = tmp_path / "deny"
    deny.write_text("# a name, a bare username, and a private project slug\n"
                    "jane citizen\n"
                    "jcitizen\n"
                    "plan-repo-x\n")
    hot = tmp_path / "doc.txt"
    hot.write_text("Ask JANE CITIZEN, notes live in plan-repo-x.\n")
    code, out = run("--files", str(hot), local_list=str(deny))
    assert code == 1, out
    assert "PERSONAL CONTENT" in out


def test_bare_username_is_caught_only_by_the_denylist(tmp_path):
    """The gap this class closes: /Users/jcitizen trips the identifier matcher,
    but the same username standing on its own has no shape to match, so nothing
    catches it until the deny-list names it."""
    deny = tmp_path / "deny"
    deny.write_text("jcitizen\n")
    bare = tmp_path / "bare.txt"
    bare.write_text("built by jcitizen on a Tuesday\n")

    assert run("--files", str(bare))[0] == 0, "no shape-based class can see this"
    code, out = run("--files", str(bare), local_list=str(deny))
    assert code == 1 and "PERSONAL CONTENT" in out


def test_missing_denylist_is_skipped_loudly(tmp_path):
    """A green result must never imply coverage the run did not perform."""
    ok = tmp_path / "ok.txt"
    ok.write_text("nothing personal here\n")
    code, out = run("--files", str(ok))
    assert code == 0, out
    assert "SKIPPED" in out, "a skipped class must say so"
    assert "NOT a publication clearance" in out


def test_denylist_comments_and_blank_lines_are_not_patterns(tmp_path):
    """A comment-only list has zero active patterns, so it checks nothing. The
    success line must report that as SKIPPED rather than as a class that ran."""
    deny = tmp_path / "deny"
    deny.write_text("# jane citizen is only mentioned in this comment\n\n\n")
    hot = tmp_path / "doc.txt"
    hot.write_text("Ask jane citizen about it.\n")
    code, out = run("--files", str(hot), local_list=str(deny))
    assert code == 0, out
    assert "SKIPPED" in out


def test_denylist_pattern_count_is_reported(tmp_path):
    """The success line states what was actually checked, so a shrinking list
    is visible in the output rather than silently reducing coverage."""
    deny = tmp_path / "deny"
    deny.write_text("# two real patterns, one comment\njane citizen\njcitizen\n")
    ok = tmp_path / "ok.txt"
    ok.write_text("nothing personal here\n")
    code, out = run("--files", str(ok), local_list=str(deny))
    assert code == 0, out
    assert "2 local deny-list pattern(s)" in out
    assert "NOT a publication clearance" in out


def test_inline_allow_marker_exempts_one_line_only(tmp_path):
    """A deliberate keep (a wire value, a documented placeholder) can carry an
    inline marker. It must exempt that line and nothing else, or the deny-list
    quietly stops guarding the rest of the tree."""
    deny = tmp_path / "deny"
    deny.write_text("acme-internal\n")
    doc = tmp_path / "sample.txt"
    doc.write_text(
        'KEEP = "acme-internal"  # secret-scan: allow (wire value)\n'
        'LEAK = "acme-internal"\n')
    rc, out = run("--files", str(doc), local_list=str(deny))
    assert rc == 1, "an unmarked hit on the same pattern must still fail"
    assert "LEAK" in out and "KEEP" not in out


def test_exclude_lists_cannot_drift(tmp_path):
    """--tree filtering and the git pathspec are derived from one array. They
    were once maintained separately and disagreed, so --tree scanned files the
    pathspec had already excluded."""
    src = SCANNER.read_text()
    assert src.count("EXCLUDES=(") == 1
    assert 'e="${e#:(exclude)}"' in src, \
        "is_excluded must derive from EXCLUDES rather than repeat it"
