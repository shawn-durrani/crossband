"""The one payload builder behind every label write (#237).

Six passes hand-built the labels_json payload and two drifted. The builder
is pure, so the six shapes are pinned here without a room, byte-for-byte
against what each pass wrote before the consolidation.
"""
from backend.diarize import COLD_START_SOURCE, label_payload


def test_fast_pass_shape():
    assert label_payload(["Blair"], score=0.912345) == {
        "clusters": ["local"], "labels": ["Blair"], "uncertain": [],
        "source": "local", "score": 0.912}


def test_owner_shapes_carry_the_marker():
    p = label_payload(["Alex"], score=0.9, owner=True)
    assert p["owner"] is True and p["source"] == "local"
    assert "learning" not in p


def test_cold_start_is_both_label_and_guess():
    assert label_payload(["Robin"], uncertain=["Robin"],
                         source=COLD_START_SOURCE, learning=True) == {
        "clusters": ["local"], "labels": ["Robin"], "uncertain": ["Robin"],
        "source": COLD_START_SOURCE, "learning": True}


def test_room_and_ambient_shapes_have_no_source():
    room = label_payload(["Blair", "Voice 2"], clusters=["c1", "c2"],
                         uncertain=["Voice 2"], source=None)
    assert room == {"clusters": ["c1", "c2"], "labels": ["Blair", "Voice 2"],
                    "uncertain": ["Voice 2"]}
    amb = label_payload(["Voice 1"], clusters=["ambient_unknown"],
                        uncertain=["Voice 1"], source=None)
    assert amb == {"clusters": ["ambient_unknown"], "labels": ["Voice 1"],
                   "uncertain": ["Voice 1"]}


def test_a_none_score_is_rounded_zero_never_null():
    assert label_payload(["Blair"], score=None) == {
        "clusters": ["local"], "labels": ["Blair"], "uncertain": [],
        "source": "local"}
    assert label_payload(["Blair"], score=0)["score"] == 0
