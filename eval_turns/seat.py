"""A stand-in seat: a tiny OpenAI-compatible server on this computer that
gives fixed replies, at a fixed pace, for free.

The rig's second app calls it like any local model (an Ollama or MLX seat).
It answers the rig's opening question with the long reply the moment talks
over, and anything else with a short "Sure.", so a remark the app sends on
as a turn costs a word of speech and no more. The Responses route answers
404, which is how the app learns to use chat completions on a local server.

Replies stream in small pieces after FIRST_TOKEN_S, so the time to the
first sound is the app's own: the round, the speech and the playback.
"""

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FIRST_TOKEN_S = 0.3
PIECE_CHARS = 6
PIECE_GAP_S = 0.02
SHORT_REPLY = "Sure."
MODEL = "rig-seat"


# The opening question comes back as the transcriber heard it, inside the
# app's own layout of the chat, so it's matched on its words.
OPENING_MATCH = 0.7


def _words(text) -> list:
    return re.findall(r"[a-z0-9']+", str(text or "").lower())


def _text(content) -> str:
    if isinstance(content, list):
        return " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return str(content or "")


def reply_for(messages, opening: str, reply: str) -> str:
    """The long reply when the newest spoken turn carries most of the
    opening question's words, the short one otherwise. The app sends its
    context note as the last user message, after the turn itself, so the
    last two user messages are read."""
    want = set(_words(opening))
    users = [m for m in messages or [] if m.get("role") == "user"][-2:]
    for m in users:
        heard = set(_words(_text(m.get("content"))))
        if want and len(want & heard) / len(want) >= OPENING_MATCH:
            return reply
    return SHORT_REPLY


def pieces(text: str, size: int = PIECE_CHARS) -> list:
    return [text[i:i + size] for i in range(0, len(text), size)]


def _chunk(content=None, finish=None) -> bytes:
    delta = {"content": content} if content is not None else {}
    body = {"id": "rig", "object": "chat.completion.chunk", "model": MODEL,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return f"data: {json.dumps(body)}\n\n".encode()


class Seat:
    """Serves on 127.0.0.1 at an unused port until stop()."""

    def __init__(self, opening: str, reply: str, first_token_s=FIRST_TOKEN_S):
        self.opening, self.reply = opening, reply
        self.first_token_s = first_token_s
        self.requests = []
        seat = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    return json.loads(raw or b"{}")
                except ValueError:
                    return {}

            def _json(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path.rstrip("/").endswith("/models"):
                    return self._json(200, {"object": "list", "data": [
                        {"id": MODEL, "object": "model"}]})
                self._json(404, {"error": "not here"})

            def do_POST(self):
                body = self._body()   # read whole, so a 404 never races (#151)
                if not self.path.rstrip("/").endswith("/chat/completions"):
                    return self._json(404, {"error": "not here"})
                text = reply_for(body.get("messages"), seat.opening, seat.reply)
                seat.requests.append({"at": time.time(), "long": text == seat.reply})
                if not body.get("stream"):
                    return self._json(200, {
                        "id": "rig", "object": "chat.completion", "model": MODEL,
                        "choices": [{"index": 0, "finish_reason": "stop",
                                     "message": {"role": "assistant", "content": text}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                  "total_tokens": 2}})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                time.sleep(seat.first_token_s)
                try:
                    for piece in pieces(text):
                        self.wfile.write(_chunk(piece))
                        self.wfile.flush()
                        time.sleep(PIECE_GAP_S)
                    self.wfile.write(_chunk(finish="stop"))
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass   # the app stopped the round: a cut

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def start(self) -> "Seat":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
