import math
import struct
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config import Settings  # noqa: E402

_SPEECH_BLOCKS: dict = {}  # (sample_rate, amp) -> one second of samples


def speech_pcm(seconds, sample_rate=16000, amp=9000):
    """Speech-shaped PCM-16 for anchor and matcher fixtures: low-band
    harmonics at a strong level, so the speech gate (#218) hears a voice.
    The old constant-value and Nyquist-square fixtures are exactly the
    shapes the gate exists to reject. One second is synthesised per
    (rate, amp) and tiled - the harmonics are integer Hz, so the block is
    seamlessly periodic."""
    key = (sample_rate, amp)
    block = _SPEECH_BLOCKS.get(key)
    if block is None:
        block = b"".join(
            struct.pack("<h", int(
                amp * math.sin(2 * math.pi * 140 * i / sample_rate)
                + 0.55 * amp * math.sin(2 * math.pi * 280 * i / sample_rate)
                + 0.33 * amp * math.sin(2 * math.pi * 420 * i / sample_rate)))
            for i in range(sample_rate))
        _SPEECH_BLOCKS[key] = block
    n = int(seconds * sample_rate) * 2
    return (block * (n // len(block) + 1))[:n]


@pytest.fixture(autouse=True)
def _room_state_clean():
    """Reset every process global the room subsystem keeps, before and after
    each test (#238).

    These were reset per file, and the lists had drifted: 13 files cleared
    `diarize._ROOM_ENABLED`, 12 cleared `_STASHED`, 5 cleared `_AMBIENT_OFF`,
    4 cleared `_LAST_DECISION`, 1 cleared `_PENDING_LABELS`, and nothing ever
    cleared `introductions._TASKS` or `mismatch._TASKS`. A fourteenth global
    meant a thirteen-file audit, and a test leaking room state into the next
    one was a matter of which file happened to clear which name.

    This is the single list. Adding a global here is the whole change.
    """
    _reset_room_state()
    yield
    _reset_room_state()


def _reset_room_state():
    from backend import anchors, diarize, introductions, mismatch
    from backend.routers import voice as voice_router

    for mod, name in (
        (diarize, "_TASKS"),
        (diarize, "_ROOM_ENABLED"),
        (diarize, "_STASHED"),
        (diarize, "_PENDING_LABELS"),
        (diarize, "_AMBIENT_OFF"),
        (diarize, "_LAST_DECISION"),
        (diarize, "_LABEL_EVENTS"),
        (diarize, "_DECISION_HISTORY"),
        # The capture registry (#134): a websocket handler that loses the
        # TestClient shutdown race leaves its entry behind, and the reader
        # in the NEXT file fails on another machine's event-loop timing -
        # the deploy gate caught exactly that (#307's dump test).
        (voice_router, "_captures"),
        # The automatic-dump floor (#304): one test's automatic dump must
        # not rate-limit the next test's.
        (voice_router, "_auto_dumps"),
        (introductions, "_TASKS"),
        (mismatch, "_TASKS"),
    ):
        container = getattr(mod, name, None)
        if container is not None:
            container.clear()
    anchors.clear_recent_audio()


@pytest.fixture(autouse=True)
def _private_tempdir(tmp_path, monkeypatch):
    """Give every test its own temp directory (#361).

    Guest worktrees live under the process temp dir at a path keyed only by
    repo and chat (`crossband-guest-demo-chat0` for most tests), so every
    test that ran a guest shared one directory. Teardown is capped at
    GUEST_TEARDOWN_S and abandoned when it overruns; on a slow runner the
    abandoned `git worktree remove` kept going in its thread while the next
    test added a worktree at the same path, and that test reported "could
    not join" with nothing wrong in it. A per-test temp dir means no test
    can reach another's worktree, however slow the machine.
    """
    private = tmp_path / "tmp"
    private.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(private))


@pytest.fixture(autouse=True)
def _voiceid_offline(monkeypatch):
    """Keep the local speaker matcher (#28) OFFLINE for the keyless suite:
    stub the one-time background model fetch to a no-op and reset the
    process-global state per test, so the matcher never downloads the 38MB
    model and never becomes ready. Since #28 PR-B that means every identity
    check defers "unavailable" - turns stay unlabelled and nothing arms
    automatically (there is no cloud fallback). Tests that exercise the
    matcher seed a fake (or the real) extractor explicitly; this fixture
    just guarantees the default is 'absent'."""
    from backend import voiceid
    voiceid._reset_for_tests()
    monkeypatch.setattr(voiceid, "_spawn_fetch", lambda cfg: None)
    yield
    voiceid._reset_for_tests()


@pytest.fixture(autouse=True)
def _no_disk_config_in_tools(monkeypatch):
    """#24/#86 re-read the repo maps from disk per round/request. In tests the
    cfg a test builds is the whole truth - never the developer's own
    config.local.json. The raise lands in refresh_repo_maps' fallback, which
    keeps the passed copies. Tests of the live re-read override this with a
    tmp-root loader."""
    from backend import tools as tools_mod

    def _no_disk(*a, **k):
        raise RuntimeError("tests build cfg explicitly; no disk config")

    monkeypatch.setattr(tools_mod, "load_settings", _no_disk)


@pytest.fixture(autouse=True)
def _funnel_check_off(monkeypatch):
    """The Funnel guard (#363) shells out to `tailscale` at every app start.
    The suite never wants the real answer: on a Mac with Tailscale that is
    a subprocess per test, and the answer depends on the machine. Tests of
    the guard patch `funnel.check` themselves."""
    from backend import funnel
    monkeypatch.setattr(funnel, "check", lambda port, binary=None: None)


@pytest.fixture(autouse=True)
def _test_clients_are_tailnet_users(monkeypatch):
    """Every test client is a tailnet user's browser unless a test says
    otherwise (#363). Tailscale serve adds an identity header to each
    request it proxies for a tailnet user, and a trusted host refuses a
    request without one as having come in through Funnel. Loopback never
    reads the header, so it is harmless there. The Funnel tests drop it to
    play the public internet."""
    from starlette.testclient import TestClient

    from backend import funnel
    original = TestClient.__init__

    def init(self, *a, **k):
        original(self, *a, **k)
        self.headers.setdefault(funnel.IDENTITY_HEADER, "owner@example.com")

    monkeypatch.setattr(TestClient, "__init__", init)


@pytest.fixture(autouse=True)
def _seat_trace_clean():
    """The seat ledger (#162) is a process global; each test starts with it
    empty so a repeat is only ever judged against that test's own rounds."""
    from backend import seat_trace
    seat_trace._reset_for_tests()
    yield
    seat_trace._reset_for_tests()


@pytest.fixture
def client_factory(tmp_path):
    """Build a TestClient against a fresh data dir, with a chosen base_url so
    the Host-header boundary middleware can be exercised. No lifespan (raw
    app) - these tests hit middleware + routes, not startup."""
    from fastapi.testclient import TestClient

    from backend.app import create_app

    def _make(base_url="http://127.0.0.1"):
        settings = Settings(data_dir=str(tmp_path / "data"), memory_url="http://127.0.0.1:59999")
        return TestClient(create_app(settings), base_url=base_url)

    return _make


@pytest.fixture
def cfg():
    """Plain cfg dict as the engine passes it around."""
    c = Settings().as_cfg()
    c["shared_instructions"] = ""
    return c


@pytest.fixture
def names():
    return {"claude": "Claude", "gpt": "GPT"}


def make_msg(mid, speaker, content, created_at=1751600000.0, tool_events=None,
             attachments=None):
    return {
        "id": mid, "chat_id": 1, "speaker": speaker, "content": content,
        "created_at": created_at, "usage_json": None,
        "tool_events": tool_events or [], "attachments": attachments or [],
    }


@pytest.fixture
def transcript():
    return [
        make_msg(1, "user", "hello everyone"),
        make_msg(2, "claude", "hi, I'm Claude"),
        make_msg(3, "gpt", "and I'm GPT"),
        make_msg(4, "claude", "shall we begin?"),
    ]
