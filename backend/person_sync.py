"""Person sync: crossband to membro and back (#33 slice 2, design on
membro#33).

Crossband stays the capture app and does all identification; membro is
the durable home. One pass does four jobs, entirely off the live voice
path (startup, and after rounds - never during a turn):

1. PULL: fetch person records changed since the last pass. A person
   marked forgotten whose bank we hold locally is forgotten here too
   (the one-press forget, step 3 of the issue's numbered list). A
   living person we don't hold is created locally and their clips
   downloaded - a fresh install re-learns every voice membro holds.
2. PUSH people: every local person without a membro mapping is created
   there (slug = the local person id, already unique and stable).
   Participant-boundary entries are never pushed - they are guard
   artefacts, not people (#65/#77).
3. PUSH clips: per mapped person, membro's stored sha256 list is
   diffed against the local clip files and missing ones are uploaded.
   Content-addressing makes re-runs no-ops, so the first pass after
   deploy IS the backfill of the installed base.
4. The pass records the newest change stamp it saw, so the next pull
   is a delta.

Membro down, or no MEMORY_AUTH_TOKEN, means the pass logs once and does
nothing - crossband behaves exactly as it did before membro existed.
"""

import base64
import hashlib
import io
import logging
import os
import threading
import time
import wave

import httpx

from . import anchors, db
from .introductions import _participant_names, participant_alias

log = logging.getLogger("crossband.person_sync")

DEBOUNCE_S = 120           # post-round passes at most this often
_state = {"last": 0.0, "warned": False}
_lock = threading.Lock()


def _token() -> str:
    return os.environ.get("MEMORY_AUTH_TOKEN", "")


def _wav_to_pcm(data: bytes):
    """(pcm16 bytes, sample_rate) out of a WAV blob membro handed back."""
    with wave.open(io.BytesIO(data)) as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise ValueError("expected 16-bit mono WAV")
        return w.readframes(w.getnframes()), w.getframerate()


def sync_once(memory_url: str, force: bool = False) -> dict:
    """One full pass. Serialised behind a lock (startup and a round end
    can coincide) and debounced unless forced."""
    with _lock:
        if not force and time.time() - _state["last"] < DEBOUNCE_S:
            return {"skipped": "debounced"}
        token = _token()
        if not token:
            if not _state["warned"]:
                log.info("person sync off: no MEMORY_AUTH_TOKEN in env")
                _state["warned"] = True
            return {"skipped": "no token"}
        _state["last"] = time.time()
        try:
            return _run(memory_url.rstrip("/"), token)
        except httpx.HTTPError as e:
            if not _state["warned"]:
                log.info("person sync skipped (membro unreachable): %s", e)
                _state["warned"] = True
            return {"skipped": f"unreachable: {e}"}


def _run(base: str, token: str) -> dict:
    store = anchors.store()
    client = httpx.Client(timeout=20,
                          headers={"Authorization": f"Bearer {token}"})
    out = {"pulled_people": 0, "pulled_clips": 0, "forgotten": 0,
           "pushed_people": 0, "pushed_clips": 0}
    try:
        since = store.get_sync_watermark()
        r = client.get(f"{base}/v1/persons", params={"since": since})
        r.raise_for_status()
        remote = r.json()["persons"]
        slugs = store.membro_slugs()             # local person_id -> slug
        by_slug = {v: k for k, v in slugs.items()}

        newest = since
        for person in remote:
            newest = max(newest, person.get("updated_at") or 0)
            slug = person["slug"]
            if person.get("forgotten_at"):
                # step 3 of the one-press forget: delete our local copies
                pid = by_slug.get(slug)
                if pid and store.forget(pid):
                    out["forgotten"] += 1
                continue
            if slug in by_slug:
                continue
            # a living person we don't hold: rebuild them locally
            pid = store.ensure_person(person["display_name"])
            if person.get("name_owner_set"):
                store.set_preferred_name(pid, person["display_name"],
                                         owner_set=True)
            store.set_membro_slug(pid, slug)
            by_slug[slug] = pid
            out["pulled_people"] += 1
            lst = client.get(f"{base}/v1/persons/{slug}/anchors")
            if lst.status_code != 200:
                continue
            for a in lst.json()["anchors"]:
                f = client.get(
                    f"{base}/v1/persons/{slug}/anchors/{a['id']}/file")
                if f.status_code != 200:
                    continue
                try:
                    pcm, rate = _wav_to_pcm(f.content)
                except (wave.Error, ValueError):
                    continue
                if store.add_clip(pid, pcm, rate,
                                  source=a.get("source") or "accumulated"):
                    out["pulled_clips"] += 1

        # PUSH: people first, then the clip diff per mapped person
        con = db.connect()
        try:
            pnames = _participant_names(con)
        finally:
            con.close()
        for p in store.people():
            pid = p["person_id"]
            if participant_alias(p["preferred_name"] or p["name"], pnames) \
                    or participant_alias(p["name"], pnames):
                continue                          # #65: never a person
            slug = store.membro_slugs().get(pid) or pid
            if store.membro_slugs().get(pid) is None:
                cr = client.post(f"{base}/v1/persons", json={
                    "slug": slug,
                    "display_name": p["preferred_name"] or p["name"],
                    "aliases": [p["name"]] + list(p["merged_names"] or []),
                    "origin_client": "multi-model-chat"})
                if cr.status_code != 200:
                    # a refusal (alias conflict, model-label backstop) is
                    # membro doing its job - log it, sync the rest
                    log.warning("person push refused for %s: %s",
                                pid, cr.text[:200])
                    continue
                store.set_membro_slug(pid, slug)
                out["pushed_people"] += 1

            lst = client.get(f"{base}/v1/persons/{slug}/anchors")
            if lst.status_code != 200:
                continue
            have = {a["sha256"] for a in lst.json()["anchors"]}
            for c in (store.clips_of(pid) or []):
                path = store.clip_path(pid, c["file"])
                if path is None:
                    continue
                data = path.read_bytes()
                if hashlib.sha256(data).hexdigest() in have:
                    continue
                up = client.post(
                    f"{base}/v1/persons/{slug}/anchors", json={
                        "data_b64": base64.b64encode(data).decode(),
                        "seconds": c.get("seconds", 0),
                        "score": c.get("score", 0),
                        "source": c.get("source", ""),
                        "captured_at": c.get("added_at", 0),
                        "client": "multi-model-chat"})
                if up.status_code == 200:
                    out["pushed_clips"] += 1

        store.set_sync_watermark(newest)
        _state["warned"] = False
        if any(out.values()):
            log.info("person sync: %s", out)
        return out
    finally:
        client.close()
