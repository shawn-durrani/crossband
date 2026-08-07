"""GitHub issue tools: read + file issues from chat, gated on
the chat's code toggle, repos from github_repos config, auth from GITHUB_TOKEN
or the machine's gh CLI. The GitHub API boundary is mocked - keyless, no
network."""

import asyncio
import base64
import json

import httpx
import pytest

from backend import tools as tools_mod

CFG = {"github_repos": {"sideband": "alex/sideband", "membro": "alex/membro"},
       "fetch_timeout": 5, "max_tool_output": 8000}


@pytest.fixture(autouse=True)
def fresh_token_cache(monkeypatch):
    monkeypatch.setattr(tools_mod, "_gh_token_cache",
                        {"value": None, "checked": False})


# ---------- availability + gating ----------

def test_available_needs_repos_and_token(monkeypatch):
    monkeypatch.setattr(tools_mod, "github_token", lambda: "tok")
    assert tools_mod.github_available(CFG)
    assert not tools_mod.github_available({"github_repos": {}})
    monkeypatch.setattr(tools_mod, "github_token", lambda: None)
    assert not tools_mod.github_available(CFG)


def test_token_prefers_env_then_gh_cli(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "env-tok")
    assert tools_mod.github_token() == "env-tok"
    monkeypatch.delenv("GITHUB_TOKEN")

    class FakeDone:
        stdout = "cli-tok\n"

    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeDone())
    assert tools_mod.github_token() == "cli-tok"


def test_definitions_carry_repo_enum():
    defs = tools_mod.github_tool_definitions(CFG)
    names = [d["name"] for d in defs]
    assert names == ["read_github_issues", "read_github_file",
                     "read_github_pr", "reopen_github_issue",
                     "file_github_issue", "comment_github_issue",
                     "edit_github_issue"]
    for d in defs:
        assert d["input_schema"]["properties"]["repo"]["enum"] == ["membro", "sideband"]


# ---------- read ----------

def test_list_issues_formats_and_skips_prs(monkeypatch):
    def fake(method, path, cfg, **kw):
        assert method == "GET" and path == "/repos/alex/sideband/issues"
        return [
            {"number": 9, "title": "Voice drops mid-round", "state": "open",
             "labels": [{"name": "bug"}], "created_at": "2026-07-10T00:00:00Z",
             "comments": 2},
            {"number": 8, "title": "A PR, not an issue", "pull_request": {},
             "labels": [], "created_at": "2026-07-09T00:00:00Z", "comments": 0},
        ]
    monkeypatch.setattr(tools_mod, "_gh_request", fake)
    out = asyncio.run(tools_mod.run_tool("read_github_issues",
                                         {"repo": "sideband"}, CFG))
    assert "#9 Voice drops mid-round [bug]" in out and "2 comments" in out
    assert "A PR" not in out  # PRs filtered out


def test_read_one_issue_includes_comments(monkeypatch):
    def fake(method, path, cfg, **kw):
        if path.endswith("/comments"):
            return [{"user": {"login": "alex"}, "created_at": "2026-07-11T00:00:00Z",
                     "body": "Repro: hold mute for 5s."}]
        return {"number": 9, "title": "Voice drops mid-round", "state": "open",
                "labels": [], "created_at": "2026-07-10T00:00:00Z",
                "user": {"login": "alex"}, "html_url": "https://x/9",
                "body": "The orb goes dark."}
    monkeypatch.setattr(tools_mod, "_gh_request", fake)
    out = asyncio.run(tools_mod.run_tool(
        "read_github_issues", {"repo": "sideband", "number": 9}, CFG))
    assert "alex/sideband#9" in out and "The orb goes dark." in out
    assert "comment by alex" in out and "hold mute" in out


def test_unknown_repo_reports_available_ones():
    out = asyncio.run(tools_mod.run_tool("read_github_issues",
                                         {"repo": "nope"}, CFG))
    assert "unknown repo" in out and "membro" in out


# ---------- read files/trees (added after live feedback: Claude claimed a
# repo-read tool that didn't exist; now it does) ----------

def test_read_file_decodes_content(monkeypatch):
    def fake(method, path, cfg, **kw):
        assert path == "/repos/alex/sideband/contents/backend/db.py"
        return {"type": "file", "size": 20, "encoding": "base64",
                "content": base64.b64encode(b"SCHEMA_VERSION = 5\n").decode()}
    monkeypatch.setattr(tools_mod, "_gh_request", fake)
    out = asyncio.run(tools_mod.run_tool(
        "read_github_file", {"repo": "sideband", "path": "backend/db.py"}, CFG))
    assert "SCHEMA_VERSION = 5" in out and "alex/sideband/backend/db.py" in out


def test_read_directory_lists_entries_dirs_first(monkeypatch):
    def fake(method, path, cfg, **kw):
        return [
            {"type": "file", "name": "app.py", "size": 900},
            {"type": "dir", "name": "routers"},
        ]
    monkeypatch.setattr(tools_mod, "_gh_request", fake)
    out = asyncio.run(tools_mod.run_tool(
        "read_github_file", {"repo": "sideband", "path": "backend"}, CFG))
    lines = out.splitlines()
    assert "2 entries" in lines[0]
    assert lines[1].strip().startswith("dir  routers")
    assert "file app.py (900 B)" in lines[2]


def test_read_file_size_cap_and_404(monkeypatch):
    def fake(method, path, cfg, **kw):
        if "huge" in path:
            return {"type": "file", "size": 10_000_000, "content": ""}
        req = httpx.Request("GET", "https://api.github.com" + path)
        raise httpx.HTTPStatusError("404", request=req,
                                    response=httpx.Response(404, request=req))
    monkeypatch.setattr(tools_mod, "_gh_request", fake)
    out = asyncio.run(tools_mod.run_tool(
        "read_github_file", {"repo": "sideband", "path": "huge.bin"}, CFG))
    assert "too large" in out and "Claude Code" in out
    out = asyncio.run(tools_mod.run_tool(
        "read_github_file", {"repo": "sideband", "path": "nope.py"}, CFG))
    assert "not found" in out


def test_read_file_passes_ref(monkeypatch):
    seen = {}

    def fake(method, path, cfg, **kw):
        seen["params"] = kw.get("params")
        return {"type": "file", "size": 2, "encoding": "base64",
                "content": base64.b64encode(b"ok").decode()}
    monkeypatch.setattr(tools_mod, "_gh_request", fake)
    out = asyncio.run(tools_mod.run_tool(
        "read_github_file",
        {"repo": "sideband", "path": "x.py", "ref": "feat/thing"}, CFG))
    assert seen["params"] == {"ref": "feat/thing"} and "@ feat/thing" in out


# ---------- file ----------

def test_file_issue_posts_with_provenance_footer(monkeypatch):
    seen = {}

    def fake(method, path, cfg, **kw):
        seen["method"], seen["path"], seen["json"] = method, path, kw.get("json")
        return {"number": 30, "html_url": "https://github.com/alex/sideband/issues/30"}
    monkeypatch.setattr(tools_mod, "_gh_request", fake)
    out = asyncio.run(tools_mod.run_tool(
        "file_github_issue",
        {"repo": "sideband", "title": "Fix the orb tint lag",
         "body": "The orb keeps the previous speaker's colour for ~2s.",
         "labels": ["bug"]},
        CFG, origin_agent="claude"))
    assert "Filed alex/sideband#30" in out
    assert seen["method"] == "POST" and seen["path"] == "/repos/alex/sideband/issues"
    assert seen["json"]["labels"] == ["bug"]
    assert "_Filed from Crossband by claude._" in seen["json"]["body"]


def test_file_issue_retries_without_unknown_labels(monkeypatch):
    calls = []

    def fake(method, path, cfg, **kw):
        calls.append(kw.get("json"))
        if "labels" in (kw.get("json") or {}):
            req = httpx.Request("POST", "https://api.github.com" + path)
            raise httpx.HTTPStatusError(
                "422", request=req,
                response=httpx.Response(422, request=req, json={}))
        return {"number": 31, "html_url": "https://x/31"}
    monkeypatch.setattr(tools_mod, "_gh_request", fake)
    out = asyncio.run(tools_mod.run_tool(
        "file_github_issue",
        {"repo": "sideband", "title": "A perfectly good title",
         "body": "A body long enough to be self-contained for a reader.",
         "labels": ["not-a-label"]},
        CFG))
    assert "Filed" in out and "labels dropped" in out
    assert len(calls) == 2 and "labels" not in calls[1]


def test_file_issue_rejects_lazy_input():
    out = asyncio.run(tools_mod.run_tool(
        "file_github_issue", {"repo": "sideband", "title": "x", "body": "y"}, CFG))
    assert out.startswith("Error:")


# ---------- comment ----------

def test_comment_issue_posts_with_provenance_footer(monkeypatch):
    seen = {}

    def fake(method, path, cfg, **kw):
        seen["method"], seen["path"], seen["json"] = method, path, kw.get("json")
        return {"html_url": "https://github.com/alex/sideband/issues/30#issuecomment-1"}
    monkeypatch.setattr(tools_mod, "_gh_request", fake)
    out = asyncio.run(tools_mod.run_tool(
        "comment_github_issue",
        {"repo": "sideband", "number": 30,
         "body": "The long pro.tail…ts.net hostname forces min-width overflow too."},
        CFG, origin_agent="gpt"))
    assert "Commented on alex/sideband#30" in out
    assert seen["method"] == "POST"
    assert seen["path"] == "/repos/alex/sideband/issues/30/comments"
    assert "_Commented from Crossband by gpt._" in seen["json"]["body"]


def test_comment_issue_rejects_lazy_body():
    out = asyncio.run(tools_mod.run_tool(
        "comment_github_issue",
        {"repo": "sideband", "number": 30, "body": "yep"}, CFG))
    assert out.startswith("Error:")


def test_comment_issue_unknown_repo():
    out = asyncio.run(tools_mod.run_tool(
        "comment_github_issue",
        {"repo": "nope", "number": 1,
         "body": "A body long enough to pass the lazy-input rejection check."},
        CFG))
    assert "unknown repo" in out and "membro" in out


# ---------- reopen (the feedback loop) ----------

def test_reopen_comments_reason_then_reopens(monkeypatch):
    calls = []

    def fake(method, path, cfg, **kw):
        calls.append((method, path, kw.get("json")))
        if method == "GET":
            return {"state": "closed", "html_url": "https://x/9"}
        return {}
    monkeypatch.setattr(tools_mod, "_gh_request", fake)
    out = asyncio.run(tools_mod.run_tool(
        "reopen_github_issue",
        {"repo": "sideband", "number": 9,
         "reason": "The overflow regressed on iOS Safari after the merge."},
        CFG, origin_agent="claude"))
    assert "Reopened alex/sideband#9" in out
    # order matters: reason comment first, then the state change
    assert calls[1][0] == "POST" and calls[1][1].endswith("/issues/9/comments")
    assert "_Reopened from Crossband by claude._" in calls[1][2]["body"]
    assert calls[2] == ("PATCH", "/repos/alex/sideband/issues/9",
                        {"state": "open"})


def test_reopen_guards(monkeypatch):
    def fake(method, path, cfg, **kw):
        if "31" in path:
            return {"state": "open", "html_url": "https://x/31"}
        return {"state": "closed", "pull_request": {}, "html_url": "https://x/5"}
    monkeypatch.setattr(tools_mod, "_gh_request", fake)
    # lazy reason rejected before any API call
    out = asyncio.run(tools_mod.run_tool(
        "reopen_github_issue", {"repo": "sideband", "number": 9, "reason": "bug"}, CFG))
    assert out.startswith("Error:")
    # already open -> pointed at comment tool instead
    out = asyncio.run(tools_mod.run_tool(
        "reopen_github_issue",
        {"repo": "sideband", "number": 31,
         "reason": "A perfectly reasonable and self-contained reason."}, CFG))
    assert "already open" in out
    # PRs are not issues
    out = asyncio.run(tools_mod.run_tool(
        "reopen_github_issue",
        {"repo": "sideband", "number": 5,
         "reason": "A perfectly reasonable and self-contained reason."}, CFG))
    assert "pull request" in out


# ---------- edit (correct a misframing in place, auditably) ----------

def _edit_fake(current, sink):
    """A _gh_request stand-in: GET returns `current`, PATCH echoes the applied
    payload, POST (audit comment) is recorded. All calls appended to `sink`."""
    def fake(method, path, cfg, **kw):
        sink.append((method, path, kw.get("json")))
        if method == "GET":
            return current
        if method == "PATCH":
            merged = dict(current)
            merged.update(kw.get("json") or {})
            merged["html_url"] = "https://x/9"
            return merged
        return {"html_url": "https://x/9#issuecomment-1"}
    return fake


def test_edit_applies_change_then_posts_audit_comment(monkeypatch):
    calls = []
    current = {"number": 9, "state": "open", "html_url": "https://x/9",
               "title": "Surface contradictions in high-salience memory facts",
               "body": "The old, weaker framing of the bug.", "labels": [{"name": "idea"}]}
    monkeypatch.setattr(tools_mod, "_gh_request", _edit_fake(current, calls))
    out = asyncio.run(tools_mod.run_tool(
        "edit_github_issue",
        {"repo": "sideband", "number": 9,
         "title": "Require provenance for high-salience personal claims",
         "reason": "The original title captured the wrong root cause; see the correcting comment."},
        CFG, origin_agent="claude"))
    assert "Edited alex/sideband#9" in out
    # order: GET, PATCH (the edit), then the audit comment
    assert calls[0][0] == "GET"
    assert calls[1][0] == "PATCH" and calls[1][1] == "/repos/alex/sideband/issues/9"
    assert calls[1][2] == {"title": "Require provenance for high-salience personal claims"}
    assert calls[2][0] == "POST" and calls[2][1].endswith("/issues/9/comments")
    audit = calls[2][2]["body"]
    assert "Issue edited from Crossband" in audit
    assert "Surface contradictions" in audit and "Require provenance" in audit
    assert "_Edited from Crossband by claude._" in audit


def test_edit_never_touches_state_or_other_fields(monkeypatch):
    calls = []
    current = {"number": 9, "state": "open", "html_url": "https://x/9",
               "title": "Old", "body": "b" * 40, "labels": []}
    monkeypatch.setattr(tools_mod, "_gh_request", _edit_fake(current, calls))
    asyncio.run(tools_mod.run_tool(
        "edit_github_issue",
        {"repo": "sideband", "number": 9, "body": "A refreshed, self-contained body text.",
         "reason": "Tightening the body so it stands alone for a triager."}, CFG))
    patch = next(c[2] for c in calls if c[0] == "PATCH")
    assert set(patch) == {"body"}  # only the field we changed; never 'state'


def test_edit_noop_when_values_already_match(monkeypatch):
    calls = []
    current = {"number": 9, "state": "open", "title": "Same title here",
               "body": "An identical body, unchanged.", "labels": [{"name": "bug"}]}
    monkeypatch.setattr(tools_mod, "_gh_request", _edit_fake(current, calls))
    out = asyncio.run(tools_mod.run_tool(
        "edit_github_issue",
        {"repo": "sideband", "number": 9, "title": "Same title here",
         "labels": ["bug"],
         "reason": "Attempting an edit that matches the current values exactly."}, CFG))
    assert "No changes to apply" in out
    assert not any(c[0] in ("PATCH", "POST") for c in calls)  # nothing written


def test_edit_requires_reason_and_rejects_prs(monkeypatch):
    # lazy reason rejected before any API call
    out = asyncio.run(tools_mod.run_tool(
        "edit_github_issue",
        {"repo": "sideband", "number": 9, "title": "A fine new title", "reason": "typo"}, CFG))
    assert out.startswith("Error:")
    # a PR is not an editable issue
    monkeypatch.setattr(tools_mod, "_gh_request",
                        lambda *a, **k: {"pull_request": {}, "title": "x"})
    out = asyncio.run(tools_mod.run_tool(
        "edit_github_issue",
        {"repo": "sideband", "number": 5, "title": "A fine new title",
         "reason": "A perfectly reasonable and self-contained reason for editing."}, CFG))
    assert "pull request" in out


def test_edit_drops_unknown_labels_but_applies_rest(monkeypatch):
    calls = []
    current = {"number": 9, "state": "open", "html_url": "https://x/9",
               "title": "Old title", "body": "b" * 40, "labels": []}

    def fake(method, path, cfg, **kw):
        calls.append((method, path, kw.get("json")))
        if method == "GET":
            return current
        if method == "PATCH" and "labels" in (kw.get("json") or {}):
            req = httpx.Request("PATCH", "https://api.github.com" + path)
            raise httpx.HTTPStatusError(
                "422", request=req, response=httpx.Response(422, request=req, json={}))
        if method == "PATCH":
            return {**current, **kw["json"], "html_url": "https://x/9"}
        return {"html_url": "https://x/9#c"}
    monkeypatch.setattr(tools_mod, "_gh_request", fake)
    out = asyncio.run(tools_mod.run_tool(
        "edit_github_issue",
        {"repo": "sideband", "number": 9, "title": "A brand new clearer title",
         "labels": ["not-a-label"],
         "reason": "Retitling and attempting a label that does not exist on the repo."}, CFG))
    assert "Edited alex/sideband#9" in out and "labels left unchanged" in out
    # retry PATCH carried the title but not the labels
    patches = [c[2] for c in calls if c[0] == "PATCH"]
    assert "labels" in patches[0] and "labels" not in patches[1]
    audit = next(c[2]["body"] for c in calls if c[0] == "POST")
    assert "Labels:" not in audit  # audit must not claim a label change that didn't apply


# ---------- read PRs (never assert deploy state blind) ----------

def test_read_pr_merged_with_deploy_trail(monkeypatch):
    def fake(method, path, cfg, **kw):
        if path.endswith("/pulls/46"):
            return {"merged": True, "state": "closed", "title": "Resume the roster",
                    "merged_at": "2026-07-13T13:54:20Z",
                    "merge_commit_sha": "50b54f6abcdef",
                    "html_url": "https://x/46",
                    "head": {"ref": "guest/resume", "sha": "abc"},
                    "base": {"ref": "main"}}
        if path.endswith("/check-runs"):
            return {"check_runs": [{"name": "test", "conclusion": "success"}]}
        return [{"user": {"login": "alex"}, "created_at": "2026-07-13T13:54:33Z",
                 "body": "🚀 Auto-deployed to the live sideband service."}]
    monkeypatch.setattr(tools_mod, "_gh_request", fake)
    out = asyncio.run(tools_mod.run_tool(
        "read_github_pr", {"repo": "sideband", "number": 46}, CFG))
    assert "[MERGED]" in out and "50b54f6" in out
    assert "test=success" in out and "🚀 Auto-deployed" in out


def test_read_pr_open_without_deploy_comment_says_so(monkeypatch):
    def fake(method, path, cfg, **kw):
        if path.endswith("/pulls/50"):
            return {"merged": False, "state": "open", "title": "WIP",
                    "html_url": "https://x/50", "head": {"ref": "b", "sha": "s"},
                    "base": {"ref": "main"}}
        if path.endswith("/check-runs"):
            return {"check_runs": []}
        return []
    monkeypatch.setattr(tools_mod, "_gh_request", fake)
    out = asyncio.run(tools_mod.run_tool(
        "read_github_pr", {"repo": "sideband", "number": 50}, CFG))
    assert "[OPEN]" in out and "deploy tooling has not reported" in out


def test_read_pr_404_redirects_to_issues(monkeypatch):
    def fake(method, path, cfg, **kw):
        req = httpx.Request("GET", "https://api.github.com" + path)
        raise httpx.HTTPStatusError("404", request=req,
                                    response=httpx.Response(404, request=req))
    monkeypatch.setattr(tools_mod, "_gh_request", fake)
    out = asyncio.run(tools_mod.run_tool(
        "read_github_pr", {"repo": "sideband", "number": 31}, CFG))
    assert "not a pull request" in out and "read_github_issues" in out


def test_read_pr_list_without_number(monkeypatch):
    def fake(method, path, cfg, **kw):
        assert path == "/repos/alex/sideband/pulls"
        assert kw["params"]["state"] == "open"
        return [
            {"number": 48, "title": "Memoize Message", "draft": False,
             "merged_at": None, "head": {"ref": "cc/memoize"},
             "created_at": "2026-07-13T04:04:15Z"},
            {"number": 60, "title": "WIP thing", "draft": True,
             "merged_at": None, "head": {"ref": "cc/wip"},
             "created_at": "2026-07-13T05:00:00Z"},
        ]
    monkeypatch.setattr(tools_mod, "_gh_request", fake)
    out = asyncio.run(tools_mod.run_tool(
        "read_github_pr", {"repo": "sideband"}, CFG))
    assert "#48 Memoize Message · cc/memoize" in out
    assert "#60 WIP thing [draft]" in out
    assert "deploy trail" in out  # nudges the drill-down


def test_read_pr_list_empty(monkeypatch):
    monkeypatch.setattr(tools_mod, "_gh_request", lambda *a, **k: [])
    out = asyncio.run(tools_mod.run_tool(
        "read_github_pr", {"repo": "sideband", "state": "open"}, CFG))
    assert out == "No open pull requests in alex/sideband."


def test_write_tools_carry_the_privacy_rule():
    """Every GitHub WRITE tool warns against real personal data (a live leak:
    repos publish; chat is private, GitHub is not)."""
    defs = {d["name"]: d["description"]
            for d in tools_mod.github_tool_definitions(CFG)}
    for name in ("file_github_issue", "comment_github_issue",
                 "reopen_github_issue", "edit_github_issue"):
        assert "personal data" in defs[name] and "publish" in defs[name], name
    # The new-content tools also warn against real INFRASTRUCTURE identifiers
    # (the tailnet-host class of leak, not just names/companies).
    for name in ("file_github_issue", "comment_github_issue"):
        assert "ts.net" in defs[name] and "infrastructure identifier" in defs[name], name
