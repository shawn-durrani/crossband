"""The maths: which recalled facts the standing summary already carried, a
sweep over candidate score floors, what each rank adds, and the latency the
lookup costs. Pure functions over per-turn results, so the tests pin them
without membro."""

from dataclasses import dataclass, field
import re
import statistics

WORD = re.compile(r"[a-z0-9]+")
# Words that carry no fact on their own; the containment check ignores them.
STOP = {"that", "this", "with", "from", "have", "been", "were", "will",
        "they", "them", "their", "there", "about", "would", "could", "which",
        "when", "what", "where", "into", "than", "then", "also", "just",
        "very", "some", "more", "most", "other", "over", "your", "does",
        "each", "same", "such", "only", "like", "said", "says", "still"}
MIN_FACT_WORDS = 3
CARRIED_THRESHOLD = 0.6
DEFAULT_FLOORS = (0.0, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8)


def content_words(text: str) -> set[str]:
    return {w for w in WORD.findall((text or "").lower())
            if len(w) >= 4 and w not in STOP}


def carried_by_summary(fact_text: str, summary_words: set[str],
                       threshold: float = CARRIED_THRESHOLD) -> bool:
    """True when most of the fact's content words are in the summary already,
    so a seat had the fact in front of it without the recall. A fact too
    short to judge counts as new: the harness must not make the recall look
    redundant on words it never checked."""
    words = content_words(fact_text)
    if len(words) < MIN_FACT_WORDS:
        return False
    return len(words & summary_words) / len(words) >= threshold


@dataclass(frozen=True)
class Hit:
    rank: int
    fact_id: int
    score: float
    carried: bool
    content: str = ""
    event_date: str = ""


@dataclass
class TurnResult:
    chat_id: int
    message_id: int
    created_at: float
    query_chars: int
    recall_ms: float
    hits: list[Hit] = field(default_factory=list)
    query: str = ""


def hits_from_facts(facts: list[dict], summary_words: set[str]) -> list[Hit]:
    """The /recall projection (contract 1.6: id, content, event_date,
    confidence, origin_agent, score, scope) into ranked hits. A fact without a
    score, from an older membro, scores 0 and lands under every floor but 0."""
    out = []
    for rank, f in enumerate(facts, 1):
        content = str(f.get("content") or "")
        out.append(Hit(rank=rank, fact_id=int(f.get("id") or 0),
                       score=float(f.get("score") or 0.0),
                       carried=carried_by_summary(content, summary_words),
                       content=content, event_date=str(f.get("event_date") or "")))
    return out


def _pct(values, p):
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * p)))
    return ordered[idx]


def _mean(values):
    return statistics.fmean(values) if values else 0.0


def floor_sweep(results: list[TurnResult], floors, live_k: int) -> list[dict]:
    """For each floor: the share of turns that would put at least one fact in
    the prompt, how many facts they would put there within the live count,
    and how many of those the summary did not already carry."""
    rows = []
    for floor in floors:
        injecting, per_turn, new_per_turn = 0, [], []
        for r in results:
            kept = [h for h in r.hits if h.score >= floor][:live_k]
            if kept:
                injecting += 1
            per_turn.append(len(kept))
            new_per_turn.append(sum(1 for h in kept if not h.carried))
        total = sum(per_turn)
        rows.append({"floor": floor,
                     "turns_injecting": injecting / len(results) if results else 0.0,
                     "facts_per_turn": _mean(per_turn),
                     "new_facts_per_turn": _mean(new_per_turn),
                     "share_new": (sum(new_per_turn) / total) if total else 0.0})
    return rows


def rank_table(results: list[TurnResult], top_k: int) -> list[dict]:
    """What each rank adds: how often a fact exists there, its mean score, and
    how often it is new against the summary. Read down the table for the
    rank where new facts stop appearing."""
    rows = []
    for k in range(1, top_k + 1):
        present = [r.hits[k - 1] for r in results if len(r.hits) >= k]
        rows.append({"rank": k,
                     "turns_with_hit": len(present) / len(results) if results else 0.0,
                     "mean_score": _mean([h.score for h in present]),
                     "share_new": (sum(1 for h in present if not h.carried) / len(present))
                     if present else 0.0})
    return rows


def aggregate(results: list[TurnResult], floors=DEFAULT_FLOORS, live_k: int = 6,
              top_k: int = 10, with_content: bool = False) -> dict:
    top1 = [r.hits[0].score for r in results if r.hits]
    every = [h.score for r in results for h in r.hits]
    by_chat: dict[int, dict] = {}
    for r in results:
        row = by_chat.setdefault(r.chat_id, {"chat_id": r.chat_id, "turns": 0,
                                             "turns_with_new": 0})
        row["turns"] += 1
        if any(not h.carried for h in r.hits[:live_k]):
            row["turns_with_new"] += 1
    corpus = []
    for r in results:
        row = {"chat_id": r.chat_id, "message_id": r.message_id,
               "created_at": r.created_at, "query_chars": r.query_chars,
               "recall_ms": round(r.recall_ms, 1),
               "hits": [{"rank": h.rank, "fact_id": h.fact_id, "score": h.score,
                         "carried": h.carried} for h in r.hits]}
        if with_content:
            row["query"] = r.query
            for h, hit in zip(row["hits"], r.hits):
                h["content"] = hit.content
                h["event_date"] = hit.event_date
        corpus.append(row)
    return {
        "n_turns": len(results),
        "n_chats": len(by_chat),
        "turns_with_hits": sum(1 for r in results if r.hits),
        "hits_total": len(every),
        "live_k": live_k,
        "top_k": top_k,
        "score_top1": {"p10": _pct(top1, 0.1), "p50": _pct(top1, 0.5),
                       "p90": _pct(top1, 0.9)},
        "score_all": {"p10": _pct(every, 0.1), "p50": _pct(every, 0.5),
                      "p90": _pct(every, 0.9)},
        "floors": floor_sweep(results, floors, live_k),
        "ranks": rank_table(results, top_k),
        "recall_ms": {"p50": _pct([r.recall_ms for r in results], 0.5),
                      "p95": _pct([r.recall_ms for r in results], 0.95)},
        "by_chat": sorted(by_chat.values(), key=lambda c: c["chat_id"]),
        "corpus": corpus,
    }
