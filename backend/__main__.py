"""Run the backend: .venv/bin/python -m backend"""

from types import FrameType

import uvicorn

from . import events
from .app import create_app
from .config import load_settings


class _Server(uvicorn.Server):
    """uvicorn's Server, plus the one thing it cannot know: which of this app's
    connections are endless on purpose.

    `handle_exit` is uvicorn's signal handler. It runs the instant SIGTERM or
    SIGINT arrives — before the connection drain — which is exactly when the
    live-events watcher streams need to be told to finish, or the drain waits on
    them forever. Delegating to super() afterwards keeps uvicorn's own
    semantics intact, including "a second Ctrl-C forces an immediate exit"."""

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        events.begin_shutdown()
        super().handle_exit(sig, frame)


def main():
    settings = load_settings()
    app = create_app(settings)
    if settings.host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit(
            f"refusing to bind {settings.host}: this app has no authentication and "
            "holds your chats and API-key-backed tools — it is localhost-only by design")
    # timeout_graceful_shutdown is the backstop under _Server above: cooperating
    # streams end at once, and anything still open after this many seconds (a
    # chat round mid-generation, a live voice call) is cancelled so the process
    # always stops in bounded time. uvicorn's own default is None — wait forever
    # — which is what made a stop indistinguishable from a hang.
    config = uvicorn.Config(app, host=settings.host, port=settings.port,
                            timeout_graceful_shutdown=settings.shutdown_timeout_s)
    _Server(config).run()


if __name__ == "__main__":
    main()
