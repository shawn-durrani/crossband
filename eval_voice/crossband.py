"""The crossband adapter: plays each conversation into a crossband's voice
relay the way the browser does, and reads back what the app wrote.

It speaks to the app only through its routes, so it keeps working as the
voice code changes underneath:

  * people and their voices: POST /api/voice/people, then
    POST /api/voice/people/{id}/record with each person reading their
    passage, the Voices page's own recording route. It then waits on
    /api/voice/health, /api/voice/people and each person's
    /readiness until the matcher is ready and the calibration covers
    every recording.
  * a conversation: POST /api/chats with no seats, so no model replies
    and nothing is spent on answers. Then the /api/voice/stt-stream
    socket, with the frames the browser sends, the turn's audio in about
    85 ms pieces at real time and a commit frame carrying a turn id. The
    committed transcript goes to /api/chats/{id}/send with the same id,
    as the browser sends it.
  * what the app wrote: each message's voice_labels from
    /api/chats/{id}, the roster from /api/chats/{id}/roster, solo from
    /api/voice/health, and the naming rows from /api/voice/sessions.

When a conversation ends, the adapter closes its tracking sessions on the
diariser, found in those rows, so a run never holds the diariser's few
sessions for the ten minutes they'd otherwise stay open.
"""

import asyncio
import base64
import collections
import json
import re
import time
import uuid

from eval_voice import scoring
from eval_voice.adapter import Adapter, ConversationResult, EventCheck, Heard
from eval_voice.mix import SAMPLE_RATE, wav_bytes

CHUNK_SAMPLES = 1360            # what the browser sends, about 85 ms
PLACEHOLDER = re.compile(r"^Voice \d+\??$")


def turn_frames(pcm: bytes, turn_id: str, chunk: int = CHUNK_SAMPLES) -> list:
    """The frames one turn is sent as: audio pieces, then a commit with no
    audio that carries the turn id, as the browser sends them."""
    step = chunk * 2
    frames = [{"sample_rate": SAMPLE_RATE,
               "audio": base64.b64encode(pcm[i:i + step]).decode()}
              for i in range(0, len(pcm), step)]
    frames.append({"sample_rate": SAMPLE_RATE, "commit": True,
                   "turn_id": turn_id})
    return frames


def heard_from_label(raw, index: int) -> Heard:
    """A message's voice_labels, as the scorer reads it. The app's label
    shape is in backend/diarize.py (label_payload) and
    backend/crosstalk.py (label). Unknown keys are ignored, so a new
    marker never breaks the rig."""
    h = Heard(index=index)
    if not raw:
        return h
    try:
        p = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError):
        h.note = "label didn't parse"
        return h
    labels = [x for x in p.get("labels") or () if isinstance(x, str)]
    names = [x for x in labels if not PLACEHOLDER.match(x)]
    h.labelled = True
    h.names = names
    h.placeholders = len(labels) - len(names)
    h.unsure = [x for x in p.get("uncertain") or () if x in names]
    h.reason = str(p.get("unresolved") or "")
    if not h.reason and h.placeholders and not names:
        h.reason = "voices still listening"
    h.learning = bool(p.get("learning"))
    h.owner = bool(p.get("owner"))
    h.crosstalk = bool(p.get("crosstalk"))
    h.score = p.get("score") if isinstance(p.get("score"), (int, float)) \
        else None
    for seg in p.get("segments") or ():
        if not isinstance(seg, dict):
            continue
        label = str(seg.get("label") or "")
        h.segments.append({"name": None if PLACEHOLDER.match(label) else label,
                           "text": str(seg.get("text") or ""),
                           "unsure": bool(seg.get("uncertain"))})
    return h


class RigFailure(RuntimeError):
    pass


class CrossbandAdapter(Adapter):
    name = "crossband"

    def __init__(self, base: str, *, diariser: str = "", calibrated=True,
                 speed=1.0, event_wait_s=10.0, settle_s=4.0,
                 final_timeout_s=15.0, ready_timeout_s=900.0, say=print):
        self.base = base.rstrip("/")
        self.diariser = diariser.rstrip("/")
        self.calibrated = calibrated
        self.speed = max(0.1, float(speed))
        self.event_wait_s = event_wait_s
        self.settle_s = settle_s
        self.final_timeout_s = final_timeout_s
        self.ready_timeout_s = ready_timeout_s
        self.say = say
        self.people = {}
        self.stt_seconds = 0.0

    # ---- plain HTTP, for the steps that aren't on the clock -------------

    def _client(self):
        import httpx
        return httpx.Client(base_url=self.base, timeout=30.0, trust_env=False)

    def enrol(self, voices: dict) -> dict:
        notes = {"people": {}}
        with self._client() as c:
            for name, pcm in voices.items():
                r = c.post("/api/voice/people", json={"name": name})
                if r.status_code == 409:
                    pid = r.json()["detail"]["conflict"]["person_id"]
                elif r.status_code == 200:
                    pid = r.json()["person_id"]
                else:
                    raise RigFailure(f"couldn't add {name}: HTTP {r.status_code}")
                r = c.post(f"/api/voice/people/{pid}/record",
                           content=wav_bytes(pcm),
                           headers={"Content-Type": "audio/wav"})
                if r.status_code != 200:
                    raise RigFailure(f"the app turned {name}'s recording away: "
                                     f"HTTP {r.status_code} {r.text[:200]}")
                got = r.json()
                self.people[name] = pid
                notes["people"][name] = {"clips": got.get("saved"),
                                         "seconds": got.get("seconds")}
                self.say(f"  {name}: {got.get('saved')} clips, "
                         f"{got.get('seconds')}s kept")
            notes.update(self._wait_ready(c))
        return notes

    def _wait_ready(self, c) -> dict:
        """Wait until the matcher is loaded and, with the calibrated scorer
        on, its last build saw every recording as it is now."""
        deadline = time.monotonic() + self.ready_timeout_s
        said = ""
        while True:
            matcher = c.get("/api/voice/health").json().get("matcher")
            people = c.get("/api/voice/people").json()
            status = people.get("readiness") or {}
            states = {n: c.get(f"/api/voice/people/{pid}/readiness")
                      .json().get("state") for n, pid in self.people.items()}
            now = (f"matcher {matcher}, scorer {status.get('state')}, "
                   f"people {sorted(set(states.values()))}")
            if now != said:
                self.say(f"  waiting: {now}")
                said = now
            ready = matcher == "ready" and (
                not self.calibrated
                or (status.get("state") == "ready"
                    and all(s == "current" for s in states.values())))
            if ready:
                break
            if matcher == "unavailable" or status.get("state") in (
                    "unavailable", "failed"):
                raise RigFailure(f"the app's voice check can't start: {now}")
            if time.monotonic() > deadline:
                raise RigFailure(f"the voice check wasn't ready in "
                                 f"{self.ready_timeout_s:.0f}s: {now}")
            time.sleep(2.0)
        readiness = {}
        by_pid = {p["person_id"]: p for p in people.get("people") or ()}
        for name, pid in self.people.items():
            r = (by_pid.get(pid) or {}).get("readiness") or {}
            readiness[name] = {k: r.get(k) for k in
                               ("ready", "reason", "pieces", "share_right",
                                "named_as_other", "days", "speech_seconds")}
        return {"matcher": matcher,
                "calibration": {k: status.get(k) for k in
                                ("state", "calibrated", "people", "clips",
                                 "pieces", "unknown")},
                "readiness": readiness}

    # ---- one conversation, on the clock ---------------------------------

    def converse(self, script_id: str, turns: list) -> ConversationResult:
        return asyncio.run(self._converse(script_id, turns))

    async def _converse(self, script_id, turns):
        import httpx
        import websockets
        async with httpx.AsyncClient(base_url=self.base, timeout=30.0,
                                     trust_env=False) as http:
            r = await http.post("/api/chats", json={
                "title": f"voice rig {script_id}", "participant_ids": []})
            r.raise_for_status()
            chat_id = r.json()["id"]
            ws_url = self.base.replace("http://", "ws://") + "/api/voice/stt-stream"
            heard = [Heard(index=mt.truth.index) for mt in turns]
            ids = [f"rig-{uuid.uuid4().hex[:10]}-{i}" for i in range(len(turns))]
            events = []
            finals = {}
            errors = []
            got_final = asyncio.Event()
            async with websockets.connect(ws_url, max_size=16 * 1024 * 1024) as ws:
                await ws.send(json.dumps({"chat_id": chat_id,
                                          "sample_rate": SAMPLE_RATE}))
                hello = json.loads(await asyncio.wait_for(ws.recv(), 15))
                if "session" not in hello:
                    raise RigFailure(f"the voice relay refused the rig: "
                                     f"{hello.get('error') or hello}")

                async def reader():
                    try:
                        async for raw in ws:
                            msg = json.loads(raw)
                            if "final" in msg:
                                finals[msg.get("turn_id") or ""] = msg["final"]
                                got_final.set()
                            elif "error" in msg:
                                errors.append(str(msg["error"])[:200])
                                got_final.set()
                    except websockets.ConnectionClosed:
                        pass

                read_task = asyncio.create_task(reader())
                try:
                    for i, mt in enumerate(turns):
                        try:
                            await self._turn(http, ws, chat_id, mt, ids[i],
                                             heard[i], events, finals,
                                             errors, got_final)
                        except websockets.ConnectionClosed:
                            # The relay went away mid-conversation: the
                            # turns it never heard say so, and the ones it
                            # did are still scored.
                            errors.append("relay closed")
                            for h in heard[i:]:
                                h.note = h.note or "relay closed"
                            break
                    else:
                        await ws.send(json.dumps({"done": True}))
                finally:
                    read_task.cancel()
            await asyncio.sleep(self.settle_s)
            chat = (await http.get(f"/api/chats/{chat_id}")).json()
            by_turn = {m.get("voice_turn_id"): m for m in chat.get("messages") or ()
                       if m.get("speaker") == "user"}
            for i, h in enumerate(heard):
                msg = by_turn.get(ids[i])
                if msg is None:
                    continue
                final = heard_from_label(msg.get("voice_labels"), h.index)
                for keep in ("in_time", "transcript", "note"):
                    setattr(final, keep, getattr(h, keep))
                heard[i] = final
            diag = await self._session_rows(http, chat_id, ids)
        diag["relay_errors"] = int(bool(errors))
        return ConversationResult(script=script_id, heard=heard, events=events,
                                  diagnostics=diag)

    async def _turn(self, http, ws, chat_id, mt, turn_id, h, events, finals,
                    errors, got_final):
        before = {}
        for e in mt.truth.events:
            before[_event_key(e)] = await self._event_state(http, chat_id, e)
        frames = turn_frames(mt.pcm, turn_id)
        piece = CHUNK_SAMPLES / SAMPLE_RATE / self.speed
        start = time.monotonic()
        for k, frame in enumerate(frames):
            await ws.send(json.dumps(frame))
            if "audio" in frame:
                wait = start + (k + 1) * piece - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
        self.stt_seconds += mt.seconds
        deadline = time.monotonic() + self.final_timeout_s
        while turn_id not in finals and time.monotonic() < deadline \
                and not errors:
            got_final.clear()
            try:
                await asyncio.wait_for(got_final.wait(),
                                       max(0.05, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                break
        text = finals.get(turn_id)
        if text is None:
            h.note = "relay error" if errors else "no transcript"
        elif not text.strip():
            h.note = "no transcript"
            h.transcript = ""
        else:
            h.transcript = text
            saved = await self._send(http, chat_id, text, turn_id)
            if saved is None:
                h.note = "send refused"
            else:
                h.in_time = bool(saved.get("voice_labels"))
        for e in mt.truth.events:
            events.append(await self._check_event(
                http, chat_id, mt.truth.index, e, before[_event_key(e)]))
        await asyncio.sleep(mt.gap_s / self.speed)

    async def _send(self, http, chat_id, text, turn_id):
        """POST the turn as the browser does, and read its stream to the end.
        Returns the saved message, or None when the app refused it."""
        for _ in range(20):
            saved = None
            async with http.stream("POST", f"/api/chats/{chat_id}/send",
                                   json={"text": text, "turn_id": turn_id},
                                   timeout=60.0) as resp:
                if resp.status_code == 409:
                    await asyncio.sleep(0.5)
                    continue
                if resp.status_code != 200:
                    return None
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        ev = json.loads(line[6:])
                    except ValueError:
                        continue
                    if ev.get("type") == "user_saved":
                        saved = ev.get("message") or {}
                    elif ev.get("type") == "done":
                        break
            return saved
        return None

    async def _event_state(self, http, chat_id, event):
        kind, value = next(iter(event.items()))
        if kind == "introduce":
            roster = (await http.get(f"/api/chats/{chat_id}/roster")).json()
            names = [r.get("display_name") or r.get("name") or ""
                     for r in roster.get("roster") or ()]
            match = [n for n in names if scoring.canonical(n) == value]
            return match[0] if match else ""
        health = (await http.get("/api/voice/health",
                                 params={"chat_id": chat_id})).json()
        chat = health.get("chat") or {}
        if value == "on":
            return "on" if chat.get("room_mode") else ""
        return "off" if chat.get("ambient_off") and not chat.get("room_mode") \
            else ""

    async def _check_event(self, http, chat_id, index, event, before):
        kind, value = next(iter(event.items()))
        if before:
            return EventCheck(index=index, kind=kind, value=value,
                              result="already", seen=before)
        deadline = time.monotonic() + self.event_wait_s
        while True:
            seen = await self._event_state(http, chat_id, event)
            if seen:
                return EventCheck(index=index, kind=kind, value=value,
                                  result="heard", seen=seen)
            if time.monotonic() > deadline:
                return EventCheck(index=index, kind=kind, value=value,
                                  result="missed")
            await asyncio.sleep(0.5)

    async def _session_rows(self, http, chat_id, ids) -> dict:
        """Content-free facts from the session naming's rows: how each turn
        was named, any diariser errors, and the tracking sessions, which
        are then closed."""
        try:
            got = (await http.get("/api/voice/sessions", params={
                "chat_id": chat_id, "rows": "true", "limit": 500})).json()
        except Exception:
            return {"session_rows": 0}
        rows = got.get("rows") or []
        index = {tid: i for i, tid in enumerate(ids)}
        methods = collections.Counter(r.get("method") or "none" for r in rows
                                      if not r.get("error"))
        errs = collections.Counter(r["error"] for r in rows if r.get("error"))
        per_turn = {index[r["turn_id"]]: r.get("method") or r.get("error") or ""
                    for r in rows if r.get("turn_id") in index}
        sessions = sorted({r["session"] for r in rows if r.get("session")})
        released = 0
        if self.diariser:
            import httpx
            async with httpx.AsyncClient(timeout=3.0, trust_env=False,
                                         follow_redirects=False) as d:
                for sid in sessions:
                    try:
                        r = await d.delete(f"{self.diariser}/sessions/{sid}")
                        released += r.status_code < 400
                    except httpx.HTTPError:
                        pass
        return {"session_rows": len(rows), "methods": dict(methods),
                "errors": dict(errs), "per_turn_method": per_turn,
                "sessions": len(sessions), "sessions_closed": released,
                "feed": {k: (got.get("status") or {}).get(k)
                         for k in ("on", "diariser", "diariser_refused")}}

    def close(self) -> dict:
        """The app's own record of what the run cost it: transcription
        minutes and the model call that reads each turn."""
        try:
            with self._client() as c:
                got = c.get("/api/usage/summary").json()
            return {"app_ledger": (got.get("windows") or {}).get("all") or {}}
        except Exception:
            return {}


def _event_key(event) -> str:
    kind, value = next(iter(event.items()))
    return f"{kind}:{value}"
