"""Client for Membro, the companion memory service (contract v1).

The service is optional: probe() checks GET /health and caches availability for
~30 seconds. When reachable and contract-compatible, memory features light up
(summary injection, recall/save/search tools, ingest+distill on chat leave);
when absent, every method degrades to a harmless no-op and the app runs fully
memoryless. All access goes through the versioned HTTP contract - never the
service's database.

search() is the one exception to "degrade quietly": once the service is
reachable, a failed or malformed /search response raises MemorySearchError
rather than returning [], because an empty list there would be
indistinguishable from a genuine no-results search (issue #63).
"""

import asyncio
import datetime
import json
import logging
import os
import re
import time

import httpx

log = logging.getLogger("crossband.memory")

CONTRACT_MAJOR = 1
# The historical source-app identifier this client sends on every write. It is
# a WIRE VALUE: Membro stores it against existing records and keys /ingest
# idempotency on (source_app, external_id), so changing it would orphan
# everything already written. It does not track the product name.
SOURCE_APP = "multi-model-chat"  # secret-scan: allow (historical wire value, see above)
PROBE_TTL = 30.0  # seconds to cache the health probe
MAX_ATTACH_BYTES = 25_000_000  # per-file sanity bound, matches the service's cap
# The service also caps attachments per MESSAGE (schema max_length=20) and
# rejects the whole ingest body with a 422 when one message exceeds it -
# which would wedge the chat: the watermark never advances and every later
# handoff retries the same rejected payload. The service tops up
# attachments on a re-ingest of the same external_id (add_attachment is
# content-addressed and idempotent), so an over-limit message is sent as
# the entry plus continuation entries carrying the remaining files.
MAX_ATTACH_PER_MESSAGE = 20  # mirrors the service's per-message schema cap


def _iso(ts: float) -> str:
    """Local-time ISO 8601 with offset for the ingest payload."""
    try:
        return datetime.datetime.fromtimestamp(ts).astimezone().isoformat()
    except (TypeError, ValueError, OSError):
        return datetime.datetime.now().astimezone().isoformat()


# ---- guest attribution on ingest (#28 phase 3, contract per membro#31) ----
#
# A new ADDITIVE speaker class rides /ingest beside the existing "user" and
# model slugs: "guest:<name>" for a turn confidently attributed to a named
# person in the room, and "guest:unknown" for a turn whose speaker the voice
# evidence could not confirm. The walls this feeds are membro's: guest facts
# quarantine by default, and an older membro treats any unrecognised speaker
# class as untrusted - so nothing here waits on the membro-side build.
#
# The non-negotiable, stated in both issues: a turn that is uncertain, or
# that carries an OPEN attribution flag (an unanswered "who is that?", or a
# mismatch doubt the owner has not resolved), must NEVER be sent as the
# owner. Before room mode existed, every voice near the microphone ingested
# as "user"; this is the corruption path being closed.

GUEST_UNKNOWN = "guest:unknown"

# #33, contract 1.2: how a label's provenance reads as an identity method
# on the wire. Anything unrecognised maps to by-elimination - the weakest
# claim, which membro's binding policy never auto-links.
IDENTITY_METHODS = {"local": "voice-match", "cold-start": "by-elimination",
                    "correction": "owner-correction",
                    "introduction": "introduced"}


def speaker_identity(msg, wire_speaker, slug_by_name):
    """The structured belief beside a guest wire label (#33, contract 1.2):
    which membro person record spoke, how confident, and how we know.
    Only the turns ingest_speaker names confidently (`guest:<name>`) carry
    one - everything weaker already ingests as guest:unknown and stays
    string-only. The confidence is the matcher's REAL per-turn score,
    stamped into the label at attach time; a turn labelled before scores
    were stamped sends 0, which membro never auto-binds on."""
    if not wire_speaker.startswith("guest:") or wire_speaker == GUEST_UNKNOWN:
        return None
    labels = msg.get("voice_labels") or {}
    if isinstance(labels, str):
        try:
            labels = json.loads(labels or "{}")
        except ValueError:
            labels = {}
    name = wire_speaker[len("guest:"):]
    return {"person": slug_by_name.get(name.casefold()),
            "confidence": float(labels.get("score") or 0.0),
            "method": IDENTITY_METHODS.get(labels.get("source") or "",
                                           "by-elimination")}
_VOICE_ORDINAL_RE = re.compile(r"^Voice \d+$")


def _parse_voice_labels(raw):
    """(labels, uncertain, crosstalk) out of a persisted voice_labels JSON
    string. Anything malformed reads as unlabelled - the same degradation as
    the transcript projection's."""
    if not raw or not isinstance(raw, str):
        return [], set(), False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return [], set(), False
    if not isinstance(data, dict):
        return [], set(), False
    labels = [l for l in (data.get("labels")
                          if isinstance(data.get("labels"), list) else [])
              if isinstance(l, str) and l.strip()]
    uncertain = {u for u in (data.get("uncertain")
                             if isinstance(data.get("uncertain"), list) else [])
                 if isinstance(u, str)}
    return labels, uncertain, data.get("crosstalk") is True


def _wire_name(name: str) -> str:
    """A guest name as it may appear inside the speaker class: single-line,
    colon-free (the class separator), bounded."""
    return " ".join((name or "").replace(":", " ").split())[:40]


def ingest_speaker(msg, open_flag_ids=frozenset(), owner_name="",
                   identity_names=None) -> str:
    """The speaker class one message carries into /ingest.

    - a non-user message (model slug, system) passes through untouched;
    - a user turn with NO voice labels is the owner typing or speaking
      alone: "user", exactly as it has always been sent;
    - an OPEN attribution flag on the turn means the attribution is doubted:
      "guest:unknown", even if the label itself reads confident;
    - a CROSSTALK-marked turn (#28 phase 4): two voices shared the
      utterance and the text may be missing or merging the quieter one's
      words, so the words cannot be trusted as any one person's -
      "guest:unknown", unconditionally, even when every label reads
      confident and even when one of the voices is the owner's, and a
      later human correction of WHO spoke does not repair WHAT the
      transcript may have lost;
    - any uncertain or ordinal label: "guest:unknown" - never the owner,
      never the guessed name;
    - confident labels that are all the owner: "user";
    - exactly one confident guest name: "guest:<identity name>" (#56). The
      wire string is PERMANENT PROVENANCE - the memory ledger keys a
      guest's history on it forever - so it carries the anchor-store
      identity name, the one string "names are law" keeps stable, resolved
      through `identity_names` (labels written under a preferred spelling
      or a merged-away name map back to the one survivor). The cosmetic,
      correctable preferred name stays a DISPLAY concern and never rides
      the wire; a label no map covers falls back to itself, as spoken;
    - anything else (several people confidently sharing one turn): the turn
      is not attributable to one speaker, so it fails safe to
      "guest:unknown"."""
    speaker = msg.get("speaker")
    if speaker != "user":
        return speaker
    if msg.get("id") in (open_flag_ids or frozenset()):
        return GUEST_UNKNOWN
    labels, uncertain, crosstalk = _parse_voice_labels(msg.get("voice_labels"))
    if crosstalk:
        return GUEST_UNKNOWN
    if not labels:
        return "user"
    if any(l in uncertain or _VOICE_ORDINAL_RE.match(l) for l in labels):
        return GUEST_UNKNOWN
    distinct = []
    for l in labels:
        if l not in distinct:
            distinct.append(l)
    owner = (owner_name or "").casefold()
    if all(n.casefold() == owner for n in distinct):
        return "user"
    if len(distinct) != 1:
        return GUEST_UNKNOWN
    name = distinct[0]
    identity = (identity_names or {}).get(name.casefold()) or name
    wire = _wire_name(identity) or _wire_name(name)
    return f"guest:{wire}" if wire else GUEST_UNKNOWN


class MemorySearchError(Exception):
    """The service was reachable (probe() said so) but /search itself did
    not behave: an error status, a transport failure, or a response that
    doesn't carry the "hits" list the contract promises. Distinct from a
    genuine zero-result search, and distinct from the service simply being
    absent (that path still degrades to [] - see search() below). The
    message is a bounded, content-free summary - never the response body,
    which may carry verbatim transcript text."""


class MemoryClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8901", timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.api = self.base_url + "/v1"
        self._client = httpx.AsyncClient(timeout=timeout)
        self._probe_ts = 0.0
        self._available = False
        self._contract_version: str | None = None
        self._stamp_warned = False  # one line, once, when the service predates 1.3
        self._warned_mismatch = False
        # Leave-hook write jobs: chat_id -> {"state": running|ok|failed, "error", "ts"}
        # Surfaced in /api/state; a failure also warns the models next round.
        self.writes: dict[int, dict] = {}

    # ---------- availability ----------

    async def probe(self, force: bool = False) -> bool:
        nowt = time.time()
        if not force and nowt - self._probe_ts < PROBE_TTL:
            return self._available
        self._probe_ts = nowt
        try:
            r = await self._client.get(self.api + "/health")
            r.raise_for_status()
            data = r.json()
            version = str(data.get("contract_version") or "")
            self._contract_version = version
            major = int(version.split(".")[0]) if version else -1
            if major != CONTRACT_MAJOR:
                if not self._warned_mismatch:
                    log.warning(
                        "memory service at %s speaks contract %s but this app needs "
                        "major %d - treating memory as ABSENT", self.base_url,
                        version or "?", CONTRACT_MAJOR)
                    self._warned_mismatch = True
                self._available = False
            else:
                self._available = data.get("status") == "ok"
        except Exception:
            self._available = False
        return self._available

    def status(self) -> dict:
        """Last-known availability for /api/state (probe separately)."""
        return {"available": self._available, "url": self.base_url,
                "contract_version": self._contract_version}

    def write_status(self) -> dict:
        failed = [{"chat_id": cid, "error": j["error"], "ts": j["ts"]}
                  for cid, j in self.writes.items() if j["state"] == "failed"]
        pending = [cid for cid, j in self.writes.items() if j["state"] == "running"]
        return {"failed": failed, "pending": pending}

    def any_write_failed(self) -> bool:
        return any(j["state"] == "failed" for j in self.writes.values())

    # ---------- recall & summary (read path) ----------

    async def get_summary(self) -> str:
        if not await self.probe():
            return ""
        try:
            r = await self._client.get(self.api + "/summary")
            r.raise_for_status()
            return (r.json().get("summary") or "").strip()
        except Exception as e:
            log.warning("memory /summary failed: %s", e)
            return ""

    async def recall(self, query: str, limit: int = 10,
                     include_superseded: bool = False,
                     origin: str = "http") -> list[dict]:
        """origin="auto" marks ambient recalls (fired per user message to
        prepare context) apart from a model's deliberate tool call - the
        service's access log and live view keep the two distinguishable.
        Older services without the field simply ignore it."""
        if not await self.probe():
            return []
        try:
            r = await self._client.post(self.api + "/recall", json={
                "query": query, "limit": limit,
                "include_superseded": include_superseded,
                "origin": origin,
            })
            r.raise_for_status()
            return r.json().get("facts", [])
        except Exception as e:
            log.warning("memory /recall failed: %s", e)
            return []

    async def search(self, query: str, limit: int = 20) -> list[dict]:
        """POST /search - verbatim transcript search, gated behind the same
        owner MEMORY_AUTH_TOKEN as membro's other exact-row reads (unlike
        /recall and /summary, which stay open). An absent service still
        degrades to [] (that's the documented no-memory posture); anything
        else that goes wrong once the service IS reachable - a bad/missing
        token, a transport failure, or a response that doesn't carry the
        "hits" list - raises MemorySearchError instead of quietly looking
        like zero results."""
        if not await self.probe():
            return []
        headers = {}
        token = os.environ.get("MEMORY_AUTH_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            r = await self._client.post(self.api + "/search",
                                        json={"query": query, "limit": limit},
                                        headers=headers)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            # Bounded and content-free: exception text from httpx/json is a
            # status/URL/parse summary, never the response body.
            log.warning("memory /search failed: %s: %s", type(e).__name__, e)
            raise MemorySearchError(
                f"memory /search request failed: {type(e).__name__}") from e
        hits = data.get("hits") if isinstance(data, dict) else None
        if not isinstance(hits, list):
            log.warning("memory /search returned an unexpected shape "
                        "(keys=%s)",
                        sorted(data.keys()) if isinstance(data, dict) else type(data).__name__)
            raise MemorySearchError("memory /search returned an unexpected response shape")
        return hits

    # ---------- writes ----------

    async def save_fact(self, content: str, origin_agent: str,
                        event_date: str | None = None,
                        confidence: str = "medium",
                        web_sources: list[str] | None = None) -> dict | None:
        """POST /facts. Returns the response dict, or None when the service is
        down / the write failed. Model-authored facts land quarantined when the
        service's trust gate says so - that's the service's call, not ours.

        web_sources (#138 slice 4, contract 1.3): the round's web stamp, so a
        save that happened after reading a page is held for review. Additive;
        a pre-1.3 service ignores it, which is the pre-#138 baseline."""
        if not await self.probe():
            return None
        body = {
            "content": content,
            "event_date": event_date or datetime.date.today().isoformat(),
            "confidence": confidence,
            "origin_agent": origin_agent,
            "source_app": SOURCE_APP,
        }
        if web_sources:
            body["web_sources"] = list(web_sources)[:20]
            self._warn_if_stamp_unsupported()
        try:
            r = await self._client.post(self.api + "/facts", json=body)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning("memory /facts failed: %s", e)
            return None

    async def ingest(self, conversation_id: str, messages: list[dict]) -> dict | None:
        """POST /ingest - idempotent on (source_app, external_id). Messages may
        carry attachments ({filename, mime, data_b64}); they ride along so the
        memory's episodic record holds everything that traveled with the chat -
        the point is to never lose this stuff. A message that is attachments-only
        (no text) is still sent, as a placeholder line, so its files land."""
        if not messages or not await self.probe():
            return None
        out = []
        for m in messages:
            atts = m.get("attachments") or []
            if not (m.get("content") or "").strip() and not atts:
                continue
            entry = {
                "external_id": str(m["id"]),
                "speaker": m["speaker"],  # "user" or participant slug
                "content": m["content"] or "(sent attached file(s))",
                "created_at": _iso(m["created_at"]),
            }
            if m.get("speaker_identity"):
                # #33 contract 1.2: the structured belief beside the label
                entry["speaker_identity"] = m["speaker_identity"]
            ws = m.get("web_sources")
            if ws:
                # #138 slice 4, contract 1.3: the db row stores json text;
                # a malformed value drops the stamp, never the message.
                if isinstance(ws, str):
                    try:
                        ws = json.loads(ws)
                    except ValueError:
                        ws = []
                if ws:
                    entry["web_sources"] = list(ws)[:20]
                    self._warn_if_stamp_unsupported()
            if atts:
                entry["attachments"] = atts[:MAX_ATTACH_PER_MESSAGE]
            out.append(entry)
            for i in range(MAX_ATTACH_PER_MESSAGE, len(atts),
                           MAX_ATTACH_PER_MESSAGE):
                cont = dict(entry)
                cont["attachments"] = atts[i:i + MAX_ATTACH_PER_MESSAGE]
                out.append(cont)
        payload = {"source_app": SOURCE_APP, "conversation_id": conversation_id,
                   "messages": out}
        r = await self._client.post(self.api + "/ingest", json=payload)
        r.raise_for_status()
        return r.json()

    def _warn_if_stamp_unsupported(self):
        """One line, once per process: a web_sources stamp sent to a pre-1.3
        service is ignored there. That is the pre-#138 baseline, so ingest
        continues either way - but silently losing a hold is worth a line."""
        if self._stamp_warned or not self._contract_version:
            return
        try:
            major, minor = (int(x) for x in
                            self._contract_version.split(".")[:2])
        except ValueError:
            return
        if (major, minor) < (1, 3):
            self._stamp_warned = True
            log.warning(
                "memory service contract %s predates web_sources - "
                "web-derived facts will not be held for review",
                self._contract_version)

    async def distill(self, conversation_id: str) -> None:
        """POST /distill - async on the service side (202 + job)."""
        r = await self._client.post(self.api + "/distill", json={
            "source_app": SOURCE_APP, "conversation_id": conversation_id,
        })
        r.raise_for_status()

    async def _wait_job(self, job_id: str, timeout: float = 900.0):
        """Poll a Membro job to completion (bulk import runs jobs serially so
        one slow conversation can't fan out into hundreds of threads)."""
        for _ in range(int(timeout / 0.5)):
            try:
                r = await self._client.get(self.api + f"/jobs/{job_id}")
                if r.status_code == 200 and r.json().get("status") != "running":
                    return r.json()
            except Exception:
                pass
            await asyncio.sleep(0.5)
        return None

    async def distill_and_wait(self, conversation_id: str, regenerate: bool = True):
        """Mine one conversation and wait. regenerate=False defers the summary
        rebuild - bulk import rebuilds once at the end, not once per chat."""
        if not await self.probe():
            return None
        try:
            r = await self._client.post(self.api + "/distill", json={
                "source_app": SOURCE_APP, "conversation_id": conversation_id,
                "regenerate_summary": regenerate,
            })
            r.raise_for_status()
            return await self._wait_job(r.json()["job_id"])
        except Exception as e:
            log.warning("memory /distill failed for %s: %s", conversation_id, e)
            return None

    async def regenerate_summary_and_wait(self):
        if not await self.probe():
            return None
        try:
            r = await self._client.post(self.api + "/summary/regenerate")
            r.raise_for_status()
            return await self._wait_job(r.json()["job_id"])
        except Exception as e:
            log.warning("memory summary regenerate failed: %s", e)
            return None

    # ---------- leave hook ----------

    async def handoff_chat(self, chat_id: int, get_new_messages, advance_watermark):
        """Fire-and-forget leave hook: ingest messages past the watermark, advance
        it, then trigger the service-side reflection pass. Failures are recorded
        in self.writes (surfaced in /api/state and to the models next round).

        get_new_messages() -> list of message dicts newer than ingested_upto
        advance_watermark(last_id) -> persists chats.ingested_upto
        """
        self.writes[chat_id] = {"state": "running", "error": None, "ts": time.time()}
        try:
            if not await self.probe():
                # service absent: not an error - silently off
                self.writes.pop(chat_id, None)
                return
            msgs = await asyncio.to_thread(get_new_messages)
            if msgs:
                await self.ingest(str(chat_id), msgs)
                await asyncio.to_thread(advance_watermark, msgs[-1]["id"])
            await self.distill(str(chat_id))
            self.writes[chat_id] = {"state": "ok", "error": None, "ts": time.time()}
        except Exception as e:
            log.warning("memory handoff for chat %s failed: %s", chat_id, e)
            self.writes[chat_id] = {"state": "failed", "error": str(e)[:300],
                                    "ts": time.time()}

    async def aclose(self):
        await self._client.aclose()
