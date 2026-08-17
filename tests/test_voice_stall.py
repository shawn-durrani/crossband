"""The stall beacon endpoint (#171): the one voice signal that reaches the
server log. It must log at WARNING (the default log level writes WARNING+
only, and the whole point is being visible there), and it must be
content-free by construction - an unknown kind, or any non-numeric field,
never reaches the log line."""

import logging

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings


@pytest.fixture
def client(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as c:
        yield c


def test_a_known_stall_kind_logs_at_warning(client, caplog):
    with caplog.at_level(logging.WARNING, logger="crossband.voice"):
        r = client.post("/api/voice/stall",
                        json={"kind": "round_guard_forced", "chat_id": 7,
                              "idle_ms": 61234.5, "round_active": True,
                              "playing": 0})
    assert r.json() == {"ok": True}
    line = next(rec for rec in caplog.records if "voice stall" in rec.message)
    assert line.levelno == logging.WARNING
    assert "round_guard_forced" in line.getMessage()
    assert "chat=7" in line.getMessage()


def test_unknown_kinds_and_text_payloads_never_reach_the_log(client, caplog):
    with caplog.at_level(logging.WARNING, logger="crossband.voice"):
        r = client.post("/api/voice/stall",
                        json={"kind": "definitely not a kind",
                              "idle_ms": "some transcript text"})
        r2 = client.post("/api/voice/stall",
                         json={"kind": "gated_speech_stranded",
                               "speech_ms": "words the user said",
                               "chat_id": "also text"})
    assert r.json() == {"ok": False}
    assert r2.json() == {"ok": True}
    stall_lines = [rec.getMessage() for rec in caplog.records
                   if "voice stall" in rec.message]
    # The bogus kind logged nothing; the good kind logged with every
    # non-numeric field coerced to None - no client text survives.
    assert len(stall_lines) == 1
    assert "words the user said" not in stall_lines[0]
    assert "also text" not in stall_lines[0]
    assert "speech_ms=None" in stall_lines[0]
    assert "chat=None" in stall_lines[0]
