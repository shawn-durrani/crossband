"""Application factory. `python -m backend` runs it on 127.0.0.1:8902."""

import asyncio
import contextlib
import fcntl
import logging
import os
import re
import time
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import __version__, person_sync
from . import auth
from . import db
from . import egress
from . import engine
from . import events
from . import guest
from . import rounds
from . import tools as tools_mod
from . import voice_trace
from . import voice
from .config import (ROOT, Settings, deprecated_env_vars, key_status,
                     load_settings, report_missing_keys)
from .memory_client import MemoryClient
from .routers import attachments as attachments_router
from .routers import auth as auth_router
from .routers import chats as chats_router
from .routers import events as events_router
from .routers import import_export as import_router
from .routers import ingest as ingest_router
from .routers import integrations as integrations_router
from .routers import mcp_servers as mcp_servers_router
from .routers import models as models_router
from .routers import participants as participants_router
from .routers import pricing as pricing_router
from .routers import projects as projects_router
from .routers import room as room_router
from .routers import settings as settings_router
from .routers import setup as setup_router
from .routers import voice as voice_router

log = logging.getLogger("crossband")

# The deploy-notice route shape, for the machine side-channel's gate
# exemption (#62). Kept in lockstep with routers/chats.py's route.
_NOTICE_PATH_RE = re.compile(r"^/api/chats/\d+/notice$")


LOCK_WAIT_S = 10.0


def _pid_alive(pid: str) -> bool:
    """Is the pid recorded in the lock file still a live process? Signal 0 asks
    the kernel without delivering anything. A PermissionError means it exists
    and belongs to someone else, which for our purposes is still 'alive'."""
    try:
        os.kill(int(pid), 0)
    except (TypeError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True
    return True


def _lock_busy_message(lock_path, holder: str, waited_s: float) -> str:
    """The message an operator reads at the worst possible moment - mid-deploy,
    having just stopped the old instance. It must therefore be TRUE about
    whether anything is really still running, and say what to do next."""
    if holder and _pid_alive(holder):
        return (
            f"Another instance is already running against this data directory "
            f"(lock {lock_path} held by pid {holder}, still alive after waiting "
            f"{waited_s:.0f}s for it to release).\n"
            f"  Stop it:            kill {holder}\n"
            f"  Refuses to exit:    kill -9 {holder}  (it is stuck draining a "
            f"connection)"
        )
    return (
        f"The data directory is locked ({lock_path}), but the pid recorded there "
        f"({holder or 'none'}) is not running - so something holds the lock "
        f"without having claimed it.\n"
        f"  Find the holder:    lsof {lock_path}"
    )


def acquire_lock(lock_path, wait_s: float = LOCK_WAIT_S):
    """Single-instance guard: one process per data directory. Returns the held
    file object (keep it referenced for the process lifetime) or raises.

    Waits up to `wait_s` for the lock rather than failing instantly, because the
    common case for "locked" is not a second instance - it is the PREVIOUS
    instance, told to stop a moment ago, still finishing its shutdown. Failing
    at once told the operator "another instance is already running" about a
    process they had just killed, which is both false and the exact opposite of
    the useful advice. Blocking is safe here: this runs in lifespan startup,
    before uvicorn serves anything, so there is no request to starve."""
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    f = open(lock_path, "a+")
    deadline = time.monotonic() + max(0.0, wait_s)
    while True:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.monotonic() >= deadline:
                f.seek(0)
                holder = f.read().strip()
                f.close()
                raise RuntimeError(_lock_busy_message(lock_path, holder, wait_s))
            time.sleep(0.1)
    f.truncate(0)
    f.write(str(os.getpid()))
    f.flush()
    return f


def _secure_env_file(path) -> None:
    """`.env` holds live API keys, so it is owner-only - always.

    Enforced HERE as well as in start.sh because the service is often launched
    by a process supervisor that never runs start.sh. Tightening on every
    startup (not only at creation) is what repairs an .env that predates this,
    was copied in by hand, or was created by an older start.sh: any of those
    can leave a world-readable file full of live keys sitting in the working
    tree. Best-effort: a permissions failure must never stop the app
    booting."""
    try:
        if path.exists() and (path.stat().st_mode & 0o077):
            os.chmod(path, 0o600)
    except OSError:
        pass


def _configure_log_level(level_name: str) -> None:
    """Opt-in verbosity for the app's own "crossband.*" loggers (empty/default:
    no-op, so behavior is byte-for-byte unchanged from before this existed).
    uvicorn configures ITS OWN loggers (uvicorn / uvicorn.error / uvicorn.access)
    independently and always has - this only ever touches the root logger, and
    only when CROSSBAND_LOG_LEVEL is explicitly set, so it can't interact with that.
    Exists so content-free INFO-level diagnostics already being logged (e.g.
    providers.py's Claude-chat cache-telemetry line) are actually
    reachable in data/service.log for a deliberate sampling session, instead
    of being silently dropped by the standard library's WARNING default."""
    if not level_name:
        return
    level = getattr(logging, level_name.strip().upper(), None)
    if not isinstance(level, int):
        log.warning("CROSSBAND_LOG_LEVEL=%r is not a recognized level name - ignoring", level_name)
        return
    logging.basicConfig(level=level)
    logging.getLogger("crossband").setLevel(level)


def create_app(settings: Settings | None = None) -> FastAPI:
    _secure_env_file(ROOT / ".env")
    load_dotenv(ROOT / ".env")
    settings = settings or load_settings()
    _configure_log_level(settings.log_level)
    # v0.2 rename: old-prefix env vars still apply, but each one warns with
    # the exact rename so the operator fixes .env before v0.3 removes the
    # fallback. One line per variable, after log config so they are visible.
    for old_name, new_name in deprecated_env_vars():
        log.warning("%s is deprecated - rename it to %s; MMC_ support ends "
                    "in v0.3", old_name, new_name)
    db.configure(settings.resolved_data_dir(), backup_keep=settings.backup_keep,
                 mirror_dir=settings.backup_mirror_dir,
                 mirror_keep=settings.backup_mirror_keep)
    db.init(settings)
    # The anchor sufficiency knobs (#28 PR-B): applied once here so the
    # store's pure rules read the operator's bar without threading cfg
    # through every call site.
    from . import anchors as _anchors
    _anchors.configure_sufficiency(settings.voice_id_sufficient_seconds,
                                   settings.voice_id_min_short_clips)
    # Stamp the voice-trace measurement epoch at deploy time, so the
    # diagnostic's exclusion boundary is "when this build first ran", not
    # "when someone first asked".
    _con = db.connect()
    try:
        voice_trace.ensure_epoch(_con)
    finally:
        _con.close()
    report_missing_keys(settings, log)  # loud: names every missing key + impact

    memory = MemoryClient(settings.memory_url)

    # Browser gate (#25): membro's credential model, enrolment-activated (see
    # backend/auth.py for the posture and its stated tradeoff). The secret and
    # session store live on app.state; the enrolled flag is cached here and
    # updated by the setup/reset handlers, so the per-request check costs a
    # dict lookup, not a database read.
    import secrets as _secrets
    recovery_secret = settings.recovery_secret or _secrets.token_urlsafe(24)
    _con = db.connect()
    try:
        auth_enrolled = auth.is_enrolled(_con)
    finally:
        _con.close()
    for line in auth.startup_lines(enrolled=auth_enrolled,
                                   secret_configured=bool(settings.recovery_secret),
                                   secret=recovery_secret):
        log.warning("%s", line)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.lock = acquire_lock(str(db.LOCK_PATH))
        # Bind the global live-events bus to THIS loop before
        # anything can insert a message - a notify() before bind_loop() would
        # otherwise silently no-op.
        events.bind_loop(asyncio.get_running_loop())
        # #138 slice 1: one vetted egress path for every model-influenced
        # URL. Tools discover the proxy via egress.proxy_url() - a module
        # global bound to this process, exactly like the events bus above.
        app.state.egress = egress.EgressProxy(
            max_transfer_bytes=settings.egress_max_transfer_mb * 1024 * 1024,
            politeness_s=settings.egress_politeness_s,
            idle_timeout_s=settings.egress_idle_timeout_s,
            lifetime_s=settings.egress_tunnel_lifetime_s,
            page_budget_bytes=int(settings.browse_page_budget_mb * 1024 * 1024))
        await app.state.egress.start()
        egress.set_proxy_url(app.state.egress.url)
        egress.set_view_proxy_url(app.state.egress.view_url)  # #148
        log.info("egress proxy on %s (view listener: %s)",
                 app.state.egress.url, app.state.egress.view_url or "off")
        await memory.probe(force=True)
        st = memory.status()
        if st["available"]:
            log.info("memory service UP at %s (contract %s)", st["url"], st["contract_version"])
        else:
            log.warning("memory service not reachable at %s - running memoryless", st["url"])

        async def backup_loop():
            while True:
                await asyncio.sleep(settings.backup_interval_hours * 3600)
                try:
                    await asyncio.to_thread(db.backup_database)
                except Exception:
                    log.exception("periodic backup failed")

        backup_task = asyncio.create_task(backup_loop())
        # #33 slice 2: reconcile the local voice store with membro's person
        # records. Off the startup critical path (a worker thread); the
        # first pass after a deploy is the backfill of the installed base,
        # and a membro that is down makes this a logged no-op.
        person_sync_task = asyncio.create_task(asyncio.to_thread(
            person_sync.sync_once, settings.memory_url, True))
        # Voice latency traces are diagnostics, not records - prune the
        # table on startup so it self-limits instead of growing forever.
        try:
            con = db.connect()
            db.prune_voice_traces(con)
            con.commit()
            con.close()
        except Exception:
            log.exception("voice-trace prune failed")
        # These belong HERE, not in router.on_startup: Starlette ignores
        # on_startup/on_shutdown when a lifespan context is provided - the
        # sweep registered that way had silently never run.
        app.state.reflection_sweep = engine.spawn(
            engine.reflection_sweep_loop(settings.as_cfg, memory))
        app.state.mcp_task = engine.spawn(app.state.mcp.run())
        # #58: a restart inside a slash command's ack window must not eat the
        # dead-man warning - re-arm timers for recent unacked commands.
        chats_router.rearm_command_deadmen(app)
        try:
            yield
        finally:
            backup_task.cancel()
            person_sync_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await backup_task
            app.state.reflection_sweep.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await app.state.reflection_sweep
            await app.state.mcp.stop()
            await memory.aclose()
            egress.set_proxy_url(None)
            egress.set_view_proxy_url(None)
            await app.state.egress.stop()
            events.unbind_loop()
            try:
                app.state.lock.close()
                os.unlink(db.LOCK_PATH)
            except OSError:
                pass

    app = FastAPI(title="crossband", version=__version__, lifespan=lifespan)

    # DNS-rebinding defense: a malicious page can re-point its own domain at
    # 127.0.0.1 and the browser will treat http://evil.com:8902 as same-origin
    # with this app (CORS never enters into it). The Host header still says
    # evil.com though - so any request whose Host isn't an allowed name is
    # refused. Loopback is always allowed; `trusted_hosts` adds a Tailscale
    # tailnet name (the tailnet is the OUTER boundary - only your devices can
    # reach that name; the session gate below stands inside it).
    allowed_hosts = {"127.0.0.1", "localhost", "::1"} | {
        h.strip().lower() for h in settings.trusted_hosts.split(",") if h.strip()}
    app.state.allowed_hosts = allowed_hosts
    app.state.recovery_secret = recovery_secret
    app.state.auth_sessions = {}
    app.state.auth_enrolled = auth_enrolled
    app.state.webauthn_pending = {}  # in-flight passkey ceremonies (#25 slice 2)

    @app.middleware("http")
    async def _host_allowlist(request, call_next):
        host = (request.url.hostname or "").lower()
        if host not in allowed_hosts:
            return JSONResponse(status_code=403, content={
                "detail": "This app serves localhost (and configured trusted hosts) only."})
        # Defense-in-depth for the trusted-host (Tailscale) path: browsers stamp
        # Sec-Fetch-Site, so reject cross-site requests to /api/* - a malicious
        # page that knows the tailnet name can't drive the API from another origin.
        # Non-browser clients (curl, the app itself) don't send it → allowed.
        path = request.url.path
        if path.startswith("/api/") \
                and request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse(status_code=403, content={
                "detail": "cross-site requests to the API are not allowed"})
        # The machine side-channel (#62): /api/ingest and the deploy-notice
        # route authenticate with the ingest token, not a browser session -
        # local tooling has no cookie jar, and the gate below silently locked
        # both routes out the moment a password was enrolled. A valid bearer
        # on exactly these two shapes passes the gate; the routes themselves
        # re-check it, and everything else still needs a session.
        if (path == "/api/ingest" or _NOTICE_PATH_RE.match(path)) \
                and auth.machine_token_ok(request):
            return await call_next(request)
        # The browser gate (#25). Once a password is enrolled, every /api
        # route outside the login surface needs a session - loopback
        # included. Before enrolment, loopback keeps its historical open
        # posture (the startup banner nags), but a trusted non-loopback host
        # is held to the login surface either way: the tailnet should never
        # see more than the lock screen without a session.
        if path.startswith("/api/") and path not in auth_router.LOGIN_SURFACE \
                and not auth.request_session_ok(request):
            if app.state.auth_enrolled:
                return JSONResponse(status_code=401, content={
                    "detail": "locked - unlock first"})
            if host not in ("127.0.0.1", "localhost", "::1"):
                return JSONResponse(status_code=401, content={
                    "detail": "no owner password is enrolled yet - enrol from "
                              "the app (recovery secret required) first"})
        return await call_next(request)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5175", "http://127.0.0.1:5175"],  # vite dev
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.settings = settings
    app.state.memory = memory
    from .mcp_client import McpManager
    app.state.mcp = McpManager(settings.mcp_servers)


    @app.get("/api/state")
    async def get_state():
        cfg = settings.as_cfg()
        tools_mod.refresh_repo_maps(cfg)  # the live maps, not the boot copies (#24, #86)
        con = db.connect()
        projects = [dict(r) for r in con.execute("SELECT * FROM projects ORDER BY created_at")]
        chats = [dict(r) for r in con.execute(
            "SELECT id, project_id, title, voice_mode, web_enabled, memory_enabled, "
            "code_enabled, archived_at, created_at, updated_at "
            "FROM chats ORDER BY updated_at DESC")]
        participants = db.get_participants(con)
        shared_instructions = db.get_setting(con, "shared_instructions")
        # The seed value for the frontend's global live-events
        # watermark. Without this, a client opening GET /api/events/stream for
        # the first time would have to pass since=0 - replaying every message
        # ever created as a burst of "new" events on first load, which the UI
        # would misread as unread activity in every chat with history.
        latest_message_id = (con.execute(
            "SELECT COALESCE(MAX(id), 0) AS m FROM messages").fetchone())["m"]
        con.close()
        available = await memory.probe()
        return {
            "projects": projects,
            "chats": chats,
            "participants": participants,
            "settings": {"shared_instructions": shared_instructions},
            "memory": {"available": available, "url": settings.memory_url},
            "memory_writes": memory.write_status(),
            "latest_message_id": latest_message_id,
            # Chats with a round still generating. Detached rounds
            # keep running after you navigate away, so this is what lets the
            # sidebar show a background chat as busy - and clear it reliably.
            "running_chat_ids": rounds.active_chat_ids(),
            "config": {
                "user_name": cfg["user_name"],
                "max_attachment_mb": cfg["max_attachment_mb"],
                "keys": key_status(),
                "search": tools_mod.available_backends(),
                "voice_enabled": voice.enabled(),
                "code": guest.status(cfg),
                "github": {"available": tools_mod.github_available(cfg),
                           "repos": sorted((cfg.get("github_repos") or {}))},
                "slash_commands": cfg.get("slash_commands") or [],
                "mcp": app.state.mcp.status(),
            },
        }

    app.include_router(auth_router.router)
    app.include_router(participants_router.router)
    app.include_router(projects_router.router)
    app.include_router(chats_router.router)
    app.include_router(attachments_router.router)
    app.include_router(voice_router.router)
    app.include_router(room_router.router)
    app.include_router(models_router.router)
    app.include_router(settings_router.router)
    app.include_router(pricing_router.router)
    app.include_router(setup_router.router)
    app.include_router(integrations_router.router)
    app.include_router(mcp_servers_router.router)
    app.include_router(import_router.router)
    app.include_router(ingest_router.router)
    app.include_router(events_router.router)

    dist = ROOT / "frontend" / "dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="static")
    return app
