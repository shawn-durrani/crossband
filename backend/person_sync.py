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
3. REPLAY corrections (slice 3): every local move, delete and merge
   the owner made is a judgement the durable home must reflect, or the
   correction resurrects through a rebuild. The store's correction
   ledger is replayed against membro's move/delete/merge routes; what
   lands (or has already converged) is removed, what cannot land yet
   stays for the next pass.
4. PUSH clips: per mapped person, membro's stored sha256 list is
   diffed against the local clip files and missing ones are uploaded.
   Content-addressing makes re-runs no-ops, so the first pass after
   deploy IS the backfill of the installed base. The same diff runs in
   REVERSE (#311): anchors membro holds that this install does not are
   downloaded and offered back through the ordinary acceptance path,
   but only while the person's bank wants more - a local bank thinned
   by rotation, eviction or a since-fixed gate refills from the
   archive that exists precisely for this.
5. The pass records the newest change stamp it saw, so the next pull
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
RESTORE_MAX_PER_PASS = 4   # anchor downloads per person per pass (#311)
_RESTORE_OFFERED_MAX = 4096
_state = {"last": 0.0, "warned": False}
_lock = threading.Lock()
# (person_id, sha) pairs already offered back to the bank this process
# (#311). Acceptance is the keep policy's call; remembering the offer -
# accepted or not - is what stops a refused anchor being re-downloaded
# every two minutes. In-process on purpose: a restart retries, which is
# also how a bank thinned since the refusal gets another look.
_restore_offered: dict = {}


def _remember_offered(pid, sha):
    _restore_offered[(pid, sha)] = True
    while len(_restore_offered) > _RESTORE_OFFERED_MAX:
        _restore_offered.pop(next(iter(_restore_offered)))


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


def _find_anchor(client, base, slug, sha):
    """Membro's anchor id for these bytes under this person, or None when the
    person's clip list genuinely lacks it. A person membro no longer holds
    (404/410) also reads as None: there is nothing durable left to correct.
    Any OTHER failure raises instead - the callers consume a None as
    "already converged", and treating an unreadable list (a 401 from a
    stale token, a 500) as converged permanently ate the pending
    correction, which is exactly the silent drop _replay_corrections
    promises never to make."""
    lst = client.get(f"{base}/v1/persons/{slug}/anchors")
    if lst.status_code in (404, 410):
        return None
    lst.raise_for_status()
    for a in lst.json()["anchors"]:
        if a["sha256"] == sha:
            return a["id"]
    return None


def _replay_corrections(client, base, store) -> int:
    """Replay the owner's moves, deletes and merges against membro (#33
    slice 3). Consumed when they land OR have already converged (the clip
    or person is not there to correct); kept pending when the target does
    not exist yet or membro cannot be reached - the next pass retries.
    Every branch is deliberate: dropping a correction silently is how a
    fixed mis-attribution resurrects through a rebuild."""
    done = []
    slugs = store.membro_slugs()
    for corr in store.pending_corrections():
        kind = corr.get("kind")
        try:
            if kind == "move":
                from_slug = slugs.get(corr["from"])
                to_slug = slugs.get(corr["to"])
                if from_slug is None:
                    done.append(corr["cid"])      # nothing durable to fix
                    continue
                if to_slug is None:
                    continue                       # target not pushed yet
                aid = _find_anchor(client, base, from_slug, corr["sha"])
                if aid is None:
                    done.append(corr["cid"])      # already converged
                    continue
                r = client.post(
                    f"{base}/v1/persons/{from_slug}/anchors/{aid}/move",
                    json={"to": to_slug})
                if r.status_code in (200, 404, 410):
                    done.append(corr["cid"])
                elif r.status_code == 409:
                    log.warning("clip move refused by membro: %s",
                                r.text[:200])
                    done.append(corr["cid"])
            elif kind == "delete":
                from_slug = slugs.get(corr["from"])
                if from_slug is None:
                    done.append(corr["cid"])
                    continue
                aid = _find_anchor(client, base, from_slug, corr["sha"])
                if aid is None:
                    done.append(corr["cid"])
                    continue
                r = client.delete(
                    f"{base}/v1/persons/{from_slug}/anchors/{aid}")
                if r.status_code in (200, 404, 410):
                    done.append(corr["cid"])
            elif kind == "merge":
                winner_slug = slugs.get(corr["winner"])
                if winner_slug is None:
                    continue                       # winner not pushed yet
                r = client.post(
                    f"{base}/v1/persons/{corr['loser_slug']}/merge",
                    json={"into": winner_slug})
                if r.status_code in (200, 404, 410):
                    done.append(corr["cid"])
                elif r.status_code == 409:
                    log.warning("merge refused by membro: %s", r.text[:200])
                    done.append(corr["cid"])
            else:
                done.append(corr.get("cid"))       # unknown kind: drop
        except httpx.HTTPError:
            break                                  # membro went away mid-pass
    store.remove_corrections([c for c in done if c])
    return len(done)


def _run(base: str, token: str) -> dict:
    store = anchors.store()
    client = httpx.Client(timeout=20,
                          headers={"Authorization": f"Bearer {token}"})
    out = {"pulled_people": 0, "pulled_clips": 0, "forgotten": 0,
           "pushed_people": 0, "pushed_clips": 0, "restored_clips": 0}
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
            if store.membro_slugs().get(pid):
                # the name resolves to a person we already hold under a
                # DIFFERENT slug - a duplicate membro record (typically
                # the not-yet-merged loser of a local merge). Never
                # clobber the survivor's mapping: the correction replay
                # owns reconciling duplicates.
                continue
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
                                  source=a.get("source") or "accumulated",
                                  membro_sha=a.get("sha256")):
                    out["pulled_clips"] += 1

        # PUSH people first (so replay targets exist), then REPLAY the
        # owner's corrections, then the clip diff per mapped person
        con = db.connect()
        try:
            pnames = _participant_names(con)
        finally:
            con.close()
        syncable = [p for p in store.people()
                    if not (participant_alias(p["preferred_name"]
                                              or p["name"], pnames)
                            or participant_alias(p["name"], pnames))]
        for p in syncable:
            pid = p["person_id"]
            if store.membro_slugs().get(pid) is None:
                cr = client.post(f"{base}/v1/persons", json={
                    "slug": pid,
                    "display_name": p["preferred_name"] or p["name"],
                    "aliases": [p["name"]] + list(p["merged_names"] or []),
                    "origin_client": "multi-model-chat"})
                if cr.status_code != 200:
                    # a refusal (alias conflict, model-label backstop) is
                    # membro doing its job - log it, sync the rest
                    log.warning("person push refused for %s: %s",
                                pid, cr.text[:200])
                    continue
                store.set_membro_slug(pid, pid)
                out["pushed_people"] += 1

        out["replayed"] = _replay_corrections(client, base, store)

        for p in syncable:
            pid = p["person_id"]
            slug = store.membro_slugs().get(pid)
            if slug is None:
                continue
            lst = client.get(f"{base}/v1/persons/{slug}/anchors")
            if lst.status_code != 200:
                continue
            remote = lst.json()["anchors"]
            have = {a["sha256"] for a in remote}
            stamps = store.membro_stamps(pid)
            local = set(stamps.values())
            for c in (store.clips_of(pid) or []):
                if c["file"] in stamps:
                    # Pulled from membro (#310): the canonical copy already
                    # sits there under the stamped sha, and the local bytes
                    # are trimmed, so re-hashing would mint a variant
                    # anchor on every pass.
                    continue
                path = store.clip_path(pid, c["file"])
                if path is None:
                    continue
                data = path.read_bytes()
                sha = hashlib.sha256(data).hexdigest()
                local.add(sha)
                if sha in have:
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
            # RESTORE (#311): the reverse diff. Only while the bank wants
            # more - a sufficient bank at capacity is already best-of, and
            # keep-best-N would just churn on what the archive holds.
            # Every restored clip runs the ordinary acceptance path (the
            # speech gate and the trim included) and lands stamped with
            # membro's own address, so it is never pushed back as a
            # variant and corrections still reach the durable copy.
            if p["sufficient"] and p["at_capacity"]:
                continue
            fetched = 0
            for a in remote:
                if fetched >= RESTORE_MAX_PER_PASS:
                    break
                sha = a.get("sha256")
                if (not sha or sha in local
                        or (pid, sha) in _restore_offered):
                    continue
                f = client.get(
                    f"{base}/v1/persons/{slug}/anchors/{a['id']}/file")
                if f.status_code != 200:
                    continue          # transient: next pass retries
                fetched += 1
                try:
                    pcm, rate = _wav_to_pcm(f.content)
                except (wave.Error, ValueError):
                    _remember_offered(pid, sha)
                    continue
                _remember_offered(pid, sha)
                if store.add_clip(pid, pcm, rate,
                                  source=a.get("source") or "accumulated",
                                  membro_sha=sha):
                    out["restored_clips"] += 1

        store.set_sync_watermark(newest)
        _state["warned"] = False
        if any(out.values()):
            log.info("person sync: %s", out)
        return out
    finally:
        client.close()
