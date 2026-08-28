"""Attachments are read once per round, not once per seat (#229).

A round builds one message list per seat, and every seat walks the same
transcript. Before this, three seats over one 20MB PDF re-read and re-encoded
the file three times, then serialised 28MB of base64 through json.dumps three
times, all on the event loop. Measured at about 175ms per seat.

These cases pin the two behaviours that fix relies on: the read cache returns
the same bytes without touching disk again, and the transcript hash still
changes when, and only when, the transcript changes.
"""
import base64
import json
import os

import pytest

from backend import attachments as att_mod
from backend import providers


@pytest.fixture(autouse=True)
def _clean_cache():
    att_mod.clear_cache()
    yield
    att_mod.clear_cache()


@pytest.fixture
def pdf(tmp_path, monkeypatch):
    monkeypatch.setattr(att_mod.db, "ATTACH_DIR", str(tmp_path))
    body = b"%PDF-1.4\n" + b"x" * 4096
    (tmp_path / "stored.pdf").write_bytes(body)
    return {"filename": "report.pdf", "mime": "application/pdf",
            "stored_name": "stored.pdf"}, body


def test_repeat_reads_do_not_touch_the_disk_again(pdf, monkeypatch):
    att, body = pdf
    first = att_mod.read_b64(att)
    assert base64.standard_b64decode(first) == body

    def explode(*a, **kw):
        raise AssertionError("read_b64 re-opened a file it had already read")

    monkeypatch.setattr("builtins.open", explode)
    assert att_mod.read_b64(att) == first
    # The provider builders sit on top of the same cache.
    assert att_mod.anthropic_blocks(att)[0]["source"]["data"] == first
    assert first in att_mod.openai_parts(att)[0]["file_data"]


def test_a_rewritten_file_is_not_served_from_cache(pdf, tmp_path):
    att, _ = pdf
    first = att_mod.read_b64(att)
    path = tmp_path / "stored.pdf"
    path.write_bytes(b"%PDF-1.4\nentirely different content")
    os.utime(path, (0, 0))
    assert att_mod.read_b64(att) != first


def test_the_cache_respects_its_byte_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(att_mod.db, "ATTACH_DIR", str(tmp_path))
    monkeypatch.setattr(att_mod, "CACHE_BUDGET_BYTES", 8192)
    for n in range(6):
        (tmp_path / f"f{n}.txt").write_bytes(b"y" * 3000)
        att_mod.read_b64({"filename": f"f{n}.txt", "mime": "text/plain",
                          "stored_name": f"f{n}.txt"})
    assert att_mod._cache_bytes <= att_mod.CACHE_BUDGET_BYTES
    assert len(att_mod._cache) == len(att_mod._cache_order)


def test_a_file_larger_than_the_whole_budget_is_never_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(att_mod.db, "ATTACH_DIR", str(tmp_path))
    monkeypatch.setattr(att_mod, "CACHE_BUDGET_BYTES", 64)
    (tmp_path / "huge.txt").write_bytes(b"z" * 4096)
    att = {"filename": "huge.txt", "mime": "text/plain", "stored_name": "huge.txt"}
    assert att_mod.read_b64(att)
    assert att_mod._cache == {}


def test_a_missing_file_still_raises_rather_than_caching_the_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(att_mod.db, "ATTACH_DIR", str(tmp_path))
    att = {"filename": "gone.pdf", "mime": "application/pdf",
           "stored_name": "gone.pdf"}
    with pytest.raises(OSError):
        att_mod.read_b64(att)


# ---------- the transcript hash still detects exactly what it did ----------

def _msgs(blob):
    return [{"role": "user", "content": [
        {"type": "text", "text": "have a look at this"},
        {"type": "document", "title": "report.pdf",
         "source": {"type": "base64", "media_type": "application/pdf",
                    "data": blob}}]}]


def test_the_hash_is_stable_for_an_unchanged_transcript():
    blob = "A" * 200_000
    assert providers._messages_hash(_msgs(blob)) == providers._messages_hash(_msgs(blob))


def test_the_hash_moves_when_an_attachment_changes():
    a = providers._messages_hash(_msgs("A" * 200_000))
    b = providers._messages_hash(_msgs("A" * 199_999 + "B"))
    assert a != b


def test_the_hash_moves_when_prose_changes_around_an_attachment():
    blob = "A" * 200_000
    one = _msgs(blob)
    two = _msgs(blob)
    two[0]["content"][0]["text"] = "have a look at this instead"
    assert providers._messages_hash(one) != providers._messages_hash(two)


def test_two_blobs_of_equal_length_are_not_confused():
    a = providers._messages_hash(_msgs("A" * 200_000))
    b = providers._messages_hash(_msgs("B" * 200_000))
    assert a != b


def test_short_strings_ride_verbatim_so_ordinary_turns_are_unaffected():
    plain = [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
    expected = providers._content_hash(
        json.dumps(plain, sort_keys=True, default=str))
    assert providers._messages_hash(plain) == expected


def test_an_unserialisable_message_still_hashes():
    assert providers._messages_hash([{"role": "user", "content": object()}])
