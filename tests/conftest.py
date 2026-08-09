import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config import Settings  # noqa: E402


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
