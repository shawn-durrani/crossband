#!/usr/bin/env bash
# Leak scan: ONE implementation, run BOTH locally (pre-commit) and in CI (via
# tests/test_secret_scan.py) so the two rule sets can never drift.
#
# It guards THREE distinct classes of leak. The distinction is deliberate.
#
#   1. SECRETS: real API-key / credential SHAPES (a prefix followed by enough
#      body to be an actual key). These are live capabilities: they belong in
#      .env (gitignored) and nowhere else. This matches the shape rather than a
#      bare prefix, so docs that merely mention "sk-ant" as a pattern don't
#      trip it. A secret is a key you rotate: catch it, revoke it, move on.
#
#   2. PERSONAL / INFRASTRUCTURE IDENTIFIERS: not secrets, but fingerprints of
#      a real machine, network, or person that must never enter a repo that
#      will be shared with collaborators or published:
#        · real Tailscale *.ts.net tailnet hostnames
#        · machine-specific absolute home paths (/Users/<name>, /home/<name>)
#        · personal, email-shaped values
#      Documented SYNTHETIC placeholders are allowed through, so the docs can
#      still show a worked example without leaking a real one. Every allowlist
#      entry is ANCHORED to the position it covers, so masking one part of a
#      token cannot launder the rest of it: `pro.tailXXXX.ts.net` is a leak of
#      a machine name even though `tailXXXX` on its own is a documented
#      placeholder. Emails anchor on whichever half marks the address
#      synthetic: a reserved domain (`@example.com`, `@users.noreply.github.com`)
#      or a non-personal local part (`noreply@`, `git@github.com`). Keep the
#      other half a placeholder too, because a real first name in front of
#      `@example.com` still passes.
#      An identifier is a fact about a person that you cannot un-publish; the
#      only real fix is to keep it out in the first place.
#
#   3. PERSONAL CONTENT: names, places, vendors, project code names, account
#      nicknames, and a bare username with no leading slash (class 2 only sees
#      the /Users/<name> shape, so a username on its own sails past it). These
#      have no general shape, so they are read as extended regexes, one per
#      line with # comments allowed, from a gitignored .secret-scan-local file.
#      The deny-list is itself personal data, so it never ships. Where the file
#      is absent (CI, a fresh clone) this class is SKIPPED and the success line
#      says so, because a green result must never imply coverage it did not
#      perform. See secret-scan-local.example.
#
# HONESTY CLAUSE: a green result covers these three classes only. It is not a
# publication clearance. Dated measurements, real conversation fragments, and
# story-shaped facts evade every regex here, so publishing still requires
# reading each file that ships.
#
# Modes (all funnel through the SAME matchers below):
#   (no args)      scan STAGED added lines            → .githooks/pre-commit
#   --tree         scan every tracked file's content  → CI, via pytest
#   --files F...   scan the given files' content      → tests / ad-hoc
#
# Run manually:  bash scripts/secret-scan.sh --tree
# Enable the local hook once:  git config core.hooksPath .githooks
set -uo pipefail

# ── 1. SECRET shapes: prefix + enough body to be a real credential ───────────
SECRET_PATTERNS='sk-ant-[A-Za-z0-9_-]{24,}|sk-proj-[A-Za-z0-9_-]{24,}|sk-[A-Za-z0-9]{40,}|tvly-(dev-)?[A-Za-z0-9_-]{16,}|BSA[A-Za-z0-9_-]{20,}|sk_[a-f0-9]{32,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{36}|xox[baprs]-[A-Za-z0-9-]{12,}'

# ── 2. IDENTIFIER shapes: things that look like a real person/machine ────────
# Candidate tokens are extracted first, then filtered against a per-class
# placeholder allowlist. The email pattern requires at least one alphanumeric
# in the local part (before @) so symbols-only prefixes cannot match; that is
# what stops a diff's leading `+` and decorator shapes like `@pytest.fixture`
# from reading as addresses.
TS_TOKEN='[A-Za-z0-9<>_.-]+\.ts\.net'
HOME_TOKEN='/(Users|home)/[A-Za-z0-9._<>-]+'
EMAIL_TOKEN='[A-Za-z0-9][A-Za-z0-9._%+<>-]*@[A-Za-z0-9.<>-]+\.[A-Za-z]{2,}'

# Documented synthetic placeholders. A candidate token is dropped only when the
# placeholder covers the identifying part, which is why every entry below is
# anchored: an unanchored "contains tailXXXX" test would let any real hostname
# through as long as the tailnet part was masked. Keep these in sync with the
# placeholder vocabulary in CONTRIBUTING.md, the roster in the PR template, and
# tests/fixtures/identifiers/clean.txt.
#   hosts:  <mac>.<tailnet>.ts.net, my-mac.my-tailnet.ts.net,
#           my-mac.tailXXXX.ts.net, tailXXXX.ts.net
#   homes:  /Users/you, /home/you (and user/username/name/me/example/runner)
#   emails: you@example.com, ...@users.noreply.github.com, git@github.com,
#           noreply@..., <user>@...
# The angle-bracket email form is anchored to the LOCAL part on purpose.
# Unanchored, it excused any token that mentioned a placeholder ANYWHERE, so
# `alex@<domain>.com` passed with the name still in it. The leading `<` is
# optional in ALLOW_EMAIL because EMAIL_TOKEN must start alphanumeric (that is
# what keeps `+` and `@decorator` shapes from reading as addresses), so the
# extractor hands this matcher `user>@<domain>.com` with the `<` already gone.
ALLOW_TS='^((<[a-z-]+>|my-[a-z0-9-]+)\.)*(<[a-z-]+>|my-[a-z0-9-]+|tail[x]{3,}|example([.-][a-z0-9-]+)*)\.ts\.net$'
ALLOW_HOME='^/(Users|home)/(you|user|username|name|me|example|runner|<[a-z-]+>)([/._-]|$)'
ALLOW_EMAIL='@example\.(com|org|net)$|@users\.noreply\.github\.com$|^git@github\.com$|^noreply@|^<?[a-z-]+>@'

# ── 3. PERSONAL CONTENT deny-list: patterns only the person can name ─────────
# Resolved from the repo root, overridable so the scanner's own tests can point
# at an explicit file and behave the same on a machine that has a real
# deny-list and in CI, which does not.
LOCAL_LIST="${SECRET_SCAN_LOCAL:-$(git rev-parse --show-toplevel 2>/dev/null || echo .)/.secret-scan-local}"

# Files whose PURPOSE is to contain these patterns (this scanner, its hook, and
# the test fixtures) plus dependency lockfiles (hashes and vendored author
# emails are noise, not leaks from this project).
EXCLUDES=(
  ':(exclude)scripts/secret-scan.sh'
  ':(exclude).githooks/pre-commit'
  ':(exclude)tests/test_secret_scan.py'
  ':(exclude)tests/fixtures/identifiers/*'
  ':(exclude)frontend/package-lock.json'
  # The never-ship set: private working documents that this snapshot does NOT
  # carry, so the scan enforces cleanliness of the SHIP SET rather than of
  # private history. An entry only belongs here if the file is absent from the
  # published tree; check `git ls-files` before adding one.
  #
  # CHANGELOG.md and CLAUDE.md were listed here under that same rationale and
  # it was wrong: both are tracked and both ship, so the exclusion quietly
  # exempted two published files from every matcher above. They are scanned
  # now, and they pass. Removing any remaining entry here without also removing
  # the file from the publication drop-list would be a mistake.
  ':(exclude)DECISIONS.md'
  ':(exclude)BACKLOG.md'
  ':(exclude)CLAUDE.local.md'
  ':(exclude)RELEASING.md'
  ':(exclude)docs/COLLABORATOR_SAFETY.md'
  ':(exclude)package-lock.json'
)
# ONE list drives both mechanisms. The pathspec array above is what git
# understands; this derives the bare-path matcher from the same entries, so
# the two can never disagree. They did once: --tree kept scanning files the
# pathspec had already excluded, and the scan reported hits the staged path
# would have ignored.
is_excluded() {
  local f="$1" e
  for e in "${EXCLUDES[@]}"; do
    e="${e#:(exclude)}"
    case "$f" in
      $e) return 0 ;;
    esac
  done
  return 1
}

# ── gather the lines to scan, per mode ───────────────────────────────────────
mode="${1:-staged}"
lines=""
case "$mode" in
  --tree)
    while IFS= read -r f; do
      is_excluded "$f" && continue
      [ -f "$f" ] || continue
      # Skip binary files.
      grep -Iq . "$f" 2>/dev/null || continue
      lines+=$(printf '%s\n' "$(sed "s#^#$f: #" "$f")")$'\n'
    done < <(git ls-files)
    ;;
  --files)
    shift
    for f in "$@"; do
      [ -f "$f" ] || continue
      # Label with the BASENAME, not the caller's full path: an absolute path
      # (e.g. /Users/<real-name>/…) would otherwise be scanned as file CONTENT
      # and trip the identifier matcher on the machine running the scan.
      lines+=$(printf '%s\n' "$(sed "s#^#${f##*/}: #" "$f")")$'\n'
    done
    ;;
  staged)
    lines=$(git diff --cached -U0 -- . "${EXCLUDES[@]}" \
              | grep -E '^\+' | grep -vE '^\+\+\+' | sed 's/^+//' || true)
    ;;
  *)
    echo "usage: secret-scan.sh [--tree | --files F...]" >&2
    exit 2
    ;;
esac

status=0

# ── matcher 1: secrets ───────────────────────────────────────────────────────
secret_hits=$(printf '%s\n' "$lines" | grep -nE "$SECRET_PATTERNS" || true)
if [ -n "$secret_hits" ]; then
  echo "✖ secret-scan: a possible real CREDENTIAL is present:"
  printf '%s\n' "$secret_hits"
  echo
  echo "  Keys are live capabilities: put them in .env (gitignored), never in git."
  echo "  Rotate it if it was ever committed."
  status=1
fi

# ── matcher 2: personal / infrastructure identifiers ─────────────────────────
ts_hits=$(printf '%s\n' "$lines"    | grep -oE "$TS_TOKEN"    2>/dev/null | grep -viE "$ALLOW_TS"    || true)
home_hits=$(printf '%s\n' "$lines"  | grep -oE "$HOME_TOKEN"  2>/dev/null | grep -viE "$ALLOW_HOME"  || true)
email_hits=$(printf '%s\n' "$lines" | grep -oE "$EMAIL_TOKEN" 2>/dev/null | grep -viE "$ALLOW_EMAIL" || true)
id_bad=$(printf '%s\n%s\n%s\n' "$ts_hits" "$home_hits" "$email_hits" \
           | grep -vE '^[[:space:]]*$' | sort -u || true)
if [ -n "$id_bad" ]; then
  echo "✖ secret-scan: a real-looking PERSONAL / INFRASTRUCTURE IDENTIFIER is present:"
  printf '    %s\n' "$id_bad"
  echo
  echo "  These are not secrets but they fingerprint a real machine or person and"
  echo "  cannot be un-published once shared. Use a documented placeholder, and"
  echo "  replace the WHOLE token, not just the part that looked sensitive:"
  echo "    tailnet host → my-mac.my-tailnet.ts.net  (or <mac>.<tailnet>.ts.net)"
  echo "    home path    → /Users/you  or  /home/you"
  echo "    email        → you@example.com  (or a ...@users.noreply.github.com)"
  status=1
fi

# ── matcher 3: personal content, from the gitignored local deny-list ─────────
personal_hits=""
personal_count=0
if [ -f "$LOCAL_LIST" ]; then
  while IFS= read -r pat || [ -n "$pat" ]; do
    case "$pat" in ''|'#'*) continue ;; esac
    personal_count=$((personal_count + 1))
    # A deliberate keep (a wire value, a documented placeholder) carries an
    # inline `secret-scan: allow` marker naming why. The marker exempts that
    # ONE line, so the pattern keeps guarding every other line in the tree.
    hit=$(printf '%s\n' "$lines" | grep -inE "$pat" \
          | grep -v 'secret-scan: allow' | head -5 || true)
    [ -n "$hit" ] && personal_hits="${personal_hits}${personal_hits:+
}  pattern '$pat':
$(printf '%s\n' "$hit" | sed 's/^/    /')"
  done < "$LOCAL_LIST"
fi
if [ -n "$personal_hits" ]; then
  echo "✖ secret-scan: PERSONAL CONTENT matched the local deny-list:"
  printf '%s\n' "$personal_hits"
  echo
  echo "  These patterns name personal facts (.secret-scan-local, gitignored)."
  echo "  Replace the content with a synthetic equivalent. Do not weaken the list."
  status=1
fi

if [ "$status" -ne 0 ]; then
  echo
  echo "  If a hit is genuinely NOT a leak, add the placeholder to the allowlist in"
  echo "  scripts/secret-scan.sh, or (local commit only) use: git commit --no-verify"
  exit 1
fi

if [ "$personal_count" -gt 0 ]; then
  echo "✓ secret-scan clean: key shapes + infra identifiers + $personal_count local deny-list pattern(s) checked. NOT a publication clearance."
else
  echo "✓ secret-scan clean: key shapes + infra identifiers checked. No deny-list patterns to check (.secret-scan-local absent or empty), so the personal-content class was SKIPPED. NOT a publication clearance."
fi
