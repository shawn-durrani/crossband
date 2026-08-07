"""Client for Membro, the companion memory service (contract v1).

The service is optional: probe() checks GET /health and caches availability for
~30 seconds. When reachable and contract-compatible, memory features light up
(summary injection, recall/save/search tools, ingest+distill on chat leave);
when absent, every method degrades to a harmless no-op and the app runs fully
memoryless. All access goes through the versioned HTTP contract - never the
service's database.
"""

import asyncio
import datetime
import logging
import time

import httpx

log = logging.getLogger("mmc.memory")

CONTRACT_MAJOR = 1
# The historical source-app identifier this client sends on every write. It is
# a WIRE VALUE: Membro stores it against existing records and keys /ingest
# idempotency on (source_app, external_id), so changing it would orphan
# everything already written. It does not track the product name.
SOURCE_APP = "multi-model-chat"  # secret-scan: allow (historical wire value, see above)
PROBE_TTL = 30.0  # seconds to cache the health probe
MAX_ATTACH_BYTES = 25_000_000  # per-file sanity bound, matches the service's cap


def _iso(ts: float) -> str:
    """Local-time ISO 8601 with offset for the ingest payload."""
    try:
        return datetime.datetime.fromtimestamp(ts).astimezone().isoformat()
    except (TypeError, ValueError, OSError):
        return datetime.datetime.now().astimezone().isoformat()


class MemoryClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8901", timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.api = self.base_url + "/v1"
        self._client = httpx.AsyncClient(timeout=timeout)
        self._probe_ts = 0.0
        self._available = False
        self._contract_version: str | None = None
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
        if not await self.probe():
            return []
        try:
            r = await self._client.post(self.api + "/search",
                                        json={"query": query, "limit": limit})
            r.raise_for_status()
            return r.json().get("hits", [])
        except Exception as e:
            log.warning("memory /search failed: %s", e)
            return []

    # ---------- writes ----------

    async def save_fact(self, content: str, origin_agent: str,
                        event_date: str | None = None,
                        confidence: str = "medium") -> dict | None:
        """POST /facts. Returns the response dict, or None when the service is
        down / the write failed. Model-authored facts land quarantined when the
        service's trust gate says so - that's the service's call, not ours."""
        if not await self.probe():
            return None
        body = {
            "content": content,
            "event_date": event_date or datetime.date.today().isoformat(),
            "confidence": confidence,
            "origin_agent": origin_agent,
            "source_app": SOURCE_APP,
        }
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
            if atts:
                entry["attachments"] = atts
            out.append(entry)
        payload = {"source_app": SOURCE_APP, "conversation_id": conversation_id,
                   "messages": out}
        r = await self._client.post(self.api + "/ingest", json=payload)
        r.raise_for_status()
        return r.json()

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
