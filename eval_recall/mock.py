"""A made-up ledger and a stand-in memory for `--mock` and the tests. The
stand-in scores facts by word overlap with the query, so the report has a
shape to look at. Its numbers say nothing about any install."""

import time

from eval_recall.ledger import Turn
from eval_recall.scoring import content_words

SUMMARY = ("Alex lives in a flat in the city and keeps bees on the roof. "
           "Alex works four days a week and rides to the office. "
           "Alex is learning the cello and has a lesson on Thursdays.")

FACTS = [
    {"id": 1, "content": "Alex keeps bees on the roof of the flat", "event_date": "2026-03-02"},
    {"id": 2, "content": "Alex has a cello lesson every Thursday evening", "event_date": "2026-04-11"},
    {"id": 3, "content": "Alex's sister Priya is moving to Hobart in June", "event_date": "2026-05-20"},
    {"id": 4, "content": "Alex is allergic to walnuts", "event_date": "2026-01-15"},
    {"id": 5, "content": "Alex rides to the office four days a week", "event_date": "2026-02-08"},
    {"id": 6, "content": "Alex wants to build a garden bench from spotted gum", "event_date": "2026-06-01"},
    {"id": 7, "content": "The hive swarmed once in October and was recaptured", "event_date": "2025-10-19"},
    {"id": 8, "content": "Alex's cello teacher is called Marta", "event_date": "2026-04-11"},
]


def mock_turns() -> list[Turn]:
    base = time.time() - 86400 * 30
    texts = [
        (1, "how are the bees going this spring, any swarm risk"),
        (1, "what wood should I use for a garden bench"),
        (2, "remind me what time my cello lesson is on thursday"),
        (2, "can you draft a note to Marta about missing next week"),
        (3, "is there anything I should avoid in this recipe with walnuts"),
        (3, "what's the weather like today"),
        (4, "Priya asked about removalists for Hobart, any tips"),
        (4, "tell me a joke"),
    ]
    return [Turn(chat, 100 + i, base + i * 3600, text)
            for i, (chat, text) in enumerate(texts)]


class MockMemory:
    """The three calls the runner makes, keyless and offline."""

    async def probe(self, force: bool = False) -> bool:
        return True

    async def get_summary(self) -> str:
        return SUMMARY

    async def recall(self, query, limit=10, origin="http", chat_id=None):
        qw = content_words(query)
        scored = []
        for f in FACTS:
            fw = content_words(f["content"])
            overlap = len(qw & fw)
            if overlap:
                scored.append((round(0.3 + 0.2 * overlap, 4), f))
        scored.sort(key=lambda x: (-x[0], x[1]["id"]))
        return [{**f, "score": s, "scope": "global"} for s, f in scored[:limit]]

    async def aclose(self):
        return None
