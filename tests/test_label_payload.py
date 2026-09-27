"""The one payload builder behind every label write (#237, #482).

Six passes once hand-built the labels_json payload and two drifted. One
builder, diarize.label_payload, now makes every shape the voice check and
the session naming write, so the shapes are pinned here without a room.
"""
from backend.diarize import label_payload

SESSION = ("session",)


def test_a_named_turn_carries_its_score():
    assert label_payload(["Blair"], clusters=SESSION, source="session",
                         score=0.912345) == {
        "clusters": ["session"], "labels": ["Blair"], "uncertain": [],
        "source": "session", "score": 0.912}


def test_owner_shapes_carry_the_marker():
    p = label_payload(["Alex"], clusters=SESSION, source="session",
                      score=0.9, owner=True)
    assert p["owner"] is True and p["source"] == "session"
    assert "learning" not in p


def test_learning_is_both_label_and_guess():
    assert label_payload(["Robin"], clusters=SESSION, uncertain=["Robin"],
                         source="session", learning=True) == {
        "clusters": ["session"], "labels": ["Robin"], "uncertain": ["Robin"],
        "source": "session", "learning": True}


def test_an_unnamed_turn_names_nobody_and_says_why():
    assert label_payload([], clusters=SESSION, source="session",
                         unresolved="new_voice") == {
        "clusters": ["session"], "labels": [], "uncertain": [],
        "source": "session", "unresolved": "new_voice"}


def test_a_missing_score_is_zero_never_omitted():
    """A named turn always writes a score, `or 0` when the naming gave
    none; only an unnamed turn omits the key."""
    assert label_payload(["Blair"], score=0) == {
        "clusters": ["local"], "labels": ["Blair"], "uncertain": [],
        "source": "local", "score": 0}
    assert "score" not in label_payload(["Blair"])
    assert "source" not in label_payload(["Blair"], source=None)
