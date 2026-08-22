"""The browse worker's OS sandbox (#148 point 1), tested keyless on any OS.

What CI can hold without a Mac: the profile's shape (every deny present,
every hole scoped to what was passed in, hostile paths refused), the argv
wrap, the refusal detector's precision (a page error inside a healthy
sandbox is NEVER read as a sandbox failure), and the render fallback - a
refused profile costs exactly one retry, unwrapped, and stops further
wrapping. Whether the profile actually compiles and holds on macOS is
scripts/sandbox_probe.py, run on the deploy box."""

import stat

import pytest

from backend import browse, egress, sandbox
from backend.config import Settings


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    sandbox._reset_for_tests()
    monkeypatch.setattr(browse, "_available", True)
    egress.set_proxy_url(None)
    egress.set_view_proxy_url(None)
    yield
    sandbox._reset_for_tests()
    egress.set_proxy_url(None)
    egress.set_view_proxy_url(None)


def _cfg(**over):
    c = Settings().as_cfg()
    c.update(over)
    return c


def test_profile_scopes_exactly_what_it_is_given():
    p = sandbox.profile(profile_dir="/tmp/x1", port=8931,
                        data_dir="/home/o/data", env_file="/home/o/.env",
                        ssh_dir="/home/o/.ssh")
    assert '(allow network-outbound (remote tcp "localhost:8931"))' in p
    assert "(deny network-outbound (remote ip))" in p
    assert "(deny file-write*)" in p
    assert '(subpath "/tmp/x1")' in p
    assert '(subpath "/home/o/data")' in p
    assert '(literal "/home/o/.env")' in p
    assert '(subpath "/home/o/.ssh")' in p
    # last-match-wins: the port allow must come AFTER the ip deny
    assert p.index("(deny network-outbound") < p.index("localhost:8931")


@pytest.mark.parametrize("bad", [
    {"profile_dir": 'x"y'}, {"data_dir": "a\\b"}, {"env_file": "a\nb"},
    {"ssh_dir": ""}, {"port": 0}, {"port": 70000},
])
def test_hostile_or_empty_fields_are_refused_never_quoted(bad):
    kw = dict(profile_dir="/t", port=1, data_dir="/d", env_file="/e",
              ssh_dir="/s")
    kw.update(bad)
    with pytest.raises(ValueError):
        sandbox.profile(**kw)


def test_wrap_prepends_and_preserves():
    assert sandbox.wrap(["py", "w"], "(version 1)") == \
        ["sandbox-exec", "-p", "(version 1)", "py", "w"]


def test_refusal_detector_never_eats_a_real_page_error():
    # worker produced output: whatever stderr says, it ran - not a refusal
    assert not sandbox.refused(b'{"error": "nav timeout"}',
                               b"sandbox-exec noise in a log line")
    # no output + sandbox-exec on stderr: the OS refused the profile
    assert sandbox.refused(b"", b"sandbox-exec: sandbox_apply: Operation not permitted")
    assert sandbox.refused(b"  ", b"sandbox-exec: profile syntax error near line 3")
    # no output, unrelated stderr (a crashed worker): not a refusal either
    assert not sandbox.refused(b"", b"Traceback (most recent call last): ...")


def test_available_is_darwin_plus_binary_minus_strikes(monkeypatch):
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    monkeypatch.setattr(sandbox.shutil, "which", lambda n: "/usr/bin/sandbox-exec")
    assert sandbox.available()
    sandbox.mark_broken("probe")
    assert not sandbox.available()          # one strike ends wrapping
    sandbox._reset_for_tests()
    monkeypatch.setattr(sandbox.shutil, "which", lambda n: None)
    assert not sandbox.available()
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    assert not sandbox.available()


# ---------- the render fallback, with a fake worker and a fake wrap ----------

def _fake_worker(tmp_path, monkeypatch):
    w = tmp_path / "fake_worker.py"
    w.write_text(
        "import json, sys\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'final_url': 'x', 'title': '', 'text': 'ok',"
        " 'links': []}))\n")
    w.chmod(w.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(browse, "_WORKER", w)
    egress.set_proxy_url("http://127.0.0.1:8931")


def test_a_refused_profile_costs_one_retry_then_stays_off(tmp_path, monkeypatch):
    _fake_worker(tmp_path, monkeypatch)
    monkeypatch.setattr(sandbox, "available", lambda: not sandbox._broken)
    # a wrap that dies exactly like sandbox-exec refusing a profile
    monkeypatch.setattr(sandbox, "wrap", lambda argv, prof: [
        "/bin/sh", "-c",
        "echo 'sandbox-exec: profile syntax error' >&2; exit 65"])
    wraps = []
    real_profile = sandbox.profile
    monkeypatch.setattr(sandbox, "profile",
                        lambda **kw: wraps.append(kw) or real_profile(**kw))
    out = browse.render("https://example.com/", _cfg())
    assert out["text"] == "ok"              # the render still succeeded
    assert len(wraps) == 1 and sandbox._broken
    out = browse.render("https://example.com/", _cfg())
    assert out["text"] == "ok"
    assert len(wraps) == 1                  # no second wrap attempt, ever


def test_a_healthy_wrap_is_used_and_scoped_to_the_render(tmp_path, monkeypatch):
    _fake_worker(tmp_path, monkeypatch)
    monkeypatch.setattr(sandbox, "available", lambda: True)
    seen = {}

    def spy_wrap(argv, prof):
        seen["profile"] = prof
        return argv                          # run the worker unwrapped on linux

    monkeypatch.setattr(sandbox, "wrap", spy_wrap)
    out = browse.render("https://example.com/", _cfg())
    assert out["text"] == "ok"
    assert 'localhost:8931' in seen["profile"]           # the proxy port
    assert 'crossband-browse-' in seen["profile"]        # this render's dir
    assert '/.ssh' in seen["profile"]


def test_the_knob_turns_the_wrap_off(tmp_path, monkeypatch):
    _fake_worker(tmp_path, monkeypatch)
    monkeypatch.setattr(sandbox, "available", lambda: True)
    called = []
    monkeypatch.setattr(sandbox, "wrap", lambda a, p: called.append(1) or a)
    browse.render("https://example.com/", _cfg(browse_sandbox=False))
    assert not called