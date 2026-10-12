"""Have you finished talking? Two guesses at every pause, kept and never
acted on (#644, the end-of-turn trial).

A spoken turn ends after 1.5 seconds of quiet. A turn that's plainly
finished, like a whole question, could end sooner, and a sentence left
hanging could wait longer. Before anything changes, this asks two readers
at every pause and writes down what each said beside what you did next:

- Jev reads the words so far, and the last thing an AI said, and answers
  mid-thought, finished or unsure.
- Smart Turn, a small model on this Mac, hears the last 8 seconds of the
  turn's audio and gives the chance you've finished. Nothing leaves the
  Mac for it.

The answer comes from you. If you carry on talking before the turn ends,
that pause was mid-thought. If the turn ends, you'd finished.
scripts/turn_end_shadow_report.py sets each reader's guesses against that.

Off unless `turn_end_shadow` is on. Jev needs TYPESAFE_API_KEY and Smart
Turn needs onnxruntime and its model, fetched once and pinned by hash. A
reader that isn't there is left out. Rows hold the guesses, timings and
word counts, never words. Logging is content-free.
"""

import asyncio
import logging
import time

import numpy as np

log = logging.getLogger("crossband.turn_end")

RATE = 16000
SECONDS = 8
MODEL_FILE = "smart-turn-v3.2-cpu.onnx"
MODEL_URL = ("https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/"
             "f766f81d3cfdf7737ac64aad813d91bbfd56bf93/" + MODEL_FILE)
MODEL_SHA256 = "2bb026316b14a660486a75b1733cd3fbab8c2fd0314dc9af7be49f8cca967e4f"

# Whisper's log-mel features, which Smart Turn's encoder was trained on:
# 25 ms windows every 10 ms, 80 mel bands to 8 kHz, on the slaney scale.
N_FFT = 400
HOP = 160
N_MELS = 80


# ---------- Smart Turn ----------

def _hz_to_mel(f):
    f = np.asarray(f, dtype=np.float64)
    lin = 3.0 * f / 200.0
    log_part = 15.0 + np.log(np.maximum(f, 1e-10) / 1000.0) / (np.log(6.4) / 27.0)
    return np.where(f >= 1000.0, log_part, lin)


def _mel_to_hz(m):
    m = np.asarray(m, dtype=np.float64)
    lin = 200.0 * m / 3.0
    log_part = 1000.0 * np.exp((np.log(6.4) / 27.0) * (m - 15.0))
    return np.where(m >= 15.0, log_part, lin)


def mel_filters() -> np.ndarray:
    """(201, 80) slaney-normalised triangles, as Whisper builds them."""
    fft_freqs = np.linspace(0, RATE // 2, 1 + N_FFT // 2)
    mels = np.linspace(_hz_to_mel(0.0), _hz_to_mel(RATE / 2), N_MELS + 2)
    edges = _mel_to_hz(mels)
    diff = np.diff(edges)
    slopes = edges[None, :] - fft_freqs[:, None]
    down = -slopes[:, :-2] / diff[:-1]
    up = slopes[:, 2:] / diff[1:]
    bank = np.maximum(0.0, np.minimum(down, up))
    return bank * (2.0 / (edges[2:N_MELS + 2] - edges[:N_MELS]))[None, :]


_FILTERS = mel_filters()
_WINDOW = np.hanning(N_FFT + 1)[:-1]


def fit(audio: np.ndarray) -> np.ndarray:
    """The last 8 seconds, with silence in front of a shorter turn."""
    n = SECONDS * RATE
    audio = np.asarray(audio, dtype=np.float32)
    if len(audio) >= n:
        return audio[-n:]
    return np.pad(audio, (n - len(audio), 0))


def features(audio: np.ndarray) -> np.ndarray:
    """(80, 800) log-mel features of the last 8 seconds."""
    x = fit(audio).astype(np.float64)
    x = (x - x.mean()) / np.sqrt(x.var() + 1e-7)
    padded = np.pad(x, N_FFT // 2, mode="reflect")
    frames = 1 + (len(padded) - N_FFT) // HOP
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(frames)[:, None]
    spec = np.abs(np.fft.rfft(padded[idx] * _WINDOW, n=N_FFT)) ** 2
    mel = np.log10(np.maximum(spec @ _FILTERS, 1e-10)).T[:, :-1]
    mel = np.maximum(mel, mel.max() - 8.0)
    return ((mel + 4.0) / 4.0).astype(np.float32)


class SmartTurn:
    """The model, loaded once. predict() takes 16 kHz float audio and
    returns (chance you've finished, milliseconds)."""

    def __init__(self, path):
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(path), sess_options=opts,
                                            providers=["CPUExecutionProvider"])

    def predict(self, audio) -> tuple:
        t0 = time.monotonic()
        out = self.session.run(None, {"input_features": features(audio)[None]})
        prob = float(np.asarray(out[0]).reshape(-1)[0])
        return prob, round((time.monotonic() - t0) * 1000, 1)


def onnxruntime_present() -> bool:
    import importlib.util
    return importlib.util.find_spec("onnxruntime") is not None


_model = None
_model_failed = False


def local_model():
    """The verified model, fetched once into the voice models folder, or
    None when onnxruntime or the file isn't there. Worker thread."""
    global _model, _model_failed
    if _model is not None or _model_failed:
        return _model
    if not onnxruntime_present():
        _model_failed = True
        return None
    from . import voiceid
    path = voiceid.fetch_verified(MODEL_URL, MODEL_SHA256,
                                  voiceid._models_dir() / MODEL_FILE)
    if path is None:
        _model_failed = True
        return None
    try:
        _model = SmartTurn(path)
    except Exception:
        log.warning("turn end: Smart Turn didn't load", exc_info=True)
        _model_failed = True
    return _model


# ---------- Jev ----------

# Listed first on purpose: Jev leans towards the first option, and a wrong
# "finished" would cut you off, where a wrong "mid-thought" only waits.
CHOICES = {
    "mid": "Mid-thought: the sentence or idea isn't complete yet, and they're "
           "likely to keep talking",
    "finished": "Finished: a complete question, request or remark, ready for "
                "an answer",
    "unsure": "Can't tell",
}
MAX_WORDS_CHARS = 600
MAX_SAID_CHARS = 300
JEV_TIMEOUT_S = 1.5


def describe(owner, seats, ai_said, words) -> str:
    lines = [f"A voice chat at home. {owner} is talking out loud with AI "
             f"assistants ({', '.join(seats) or 'the assistants'})."]
    if ai_said:
        lines.append(f"The last thing an assistant said: \"{ai_said[-MAX_SAID_CHARS:]}\"")
    lines.append(f"{owner} has just paused after saying: \"{words[:MAX_WORDS_CHARS]}\"")
    return "\n".join(lines)


def jev_questions() -> dict:
    return {"status": {"type": "choice",
                       "instructions": "Has the speaker finished what they "
                                       "wanted to say, so the assistants should "
                                       "answer now?",
                       "criteria": dict(CHOICES)}}


def parse_jev(answers) -> tuple:
    """(choice, confidence), ('', None) for anything off-shape."""
    try:
        ans = answers["status"]
        choice = str(ans.get("choice") or "")
        conf = ans.get("confidence")
        return (choice if choice in CHOICES else "",
                float(conf) if conf is not None else None)
    except (KeyError, TypeError, AttributeError, ValueError):
        return "", None


# ---------- the shadow ----------

def enabled(cfg) -> bool:
    return bool((cfg or {}).get("turn_end_shadow"))


def word_count(text) -> int:
    return len(str(text or "").split())


def _context(chat_id, cfg):
    """The owner, the chat's seats and the last thing a seat said (worker
    thread)."""
    from . import db
    con = db.connect()
    try:
        seats = {p["slug"]: p["name"] for p in db.get_chat_participants(con, chat_id)}
        row = None
        if seats:
            marks = ",".join("?" * len(seats))
            row = con.execute(
                f"SELECT * FROM messages WHERE chat_id=? AND speaker IN ({marks}) "
                "ORDER BY id DESC LIMIT 1", (chat_id, *seats)).fetchone()
        said = (db.heard_view(dict(row)).get("content") or "") if row else ""
    finally:
        con.close()
    return cfg.get("user_name") or "User", list(seats.values()), said


async def ask_jev(chat_id, words, cfg, client=None) -> dict:
    from . import jev
    if not jev.api_key():
        return {"jev": "", "jev_conf": None, "jev_ms": None}
    owner, seats, said = await asyncio.to_thread(_context, chat_id, cfg)
    got = await jev.ask(describe(owner, seats, said, words), jev_questions(),
                        timeout_s=JEV_TIMEOUT_S, client=client)
    choice, conf = parse_jev(got["answers"])
    if not got.get("error"):
        try:
            await asyncio.to_thread(jev.record_usage, chat_id, "turn_end_shadow",
                                    got, cfg)
        except Exception:
            log.debug("turn end spend row failed", exc_info=True)
    return {"jev": choice, "jev_conf": conf, "jev_ms": got["ms"],
            "error": got.get("error") or ""}


async def ask_local(audio: np.ndarray) -> dict:
    def run():
        model = local_model()
        if model is None or not len(audio):
            return None
        return model.predict(audio)
    got = await asyncio.to_thread(run)
    if got is None:
        return {"local": None, "local_ms": None}
    return {"local": round(got[0], 4), "local_ms": got[1]}


def store(chat_id, row: dict) -> None:
    """One turn_end_shadow row (worker thread). Counts and guesses only."""
    from . import db
    con = db.connect()
    try:
        con.execute(
            "INSERT INTO turn_end_shadow(chat_id, created_at, words, jev, jev_conf, "
            "jev_ms, local_prob, local_ms, outcome, gap_ms, error) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (chat_id, time.time(), row["words"], row.get("jev") or "",
             row.get("jev_conf"), row.get("jev_ms"), row.get("local"),
             row.get("local_ms"), row["outcome"], row["gap_ms"],
             (row.get("error") or "")[:60]))
        con.commit()
    finally:
        con.close()


class Shadow:
    """One relay session's pauses. The relay calls audio() with each chunk
    it sends upstream, pause() on the browser's pause hint, partial() with
    each partial transcript and commit() when the turn ends. Each pause's
    guesses are written down with what came next, and nothing else here
    touches the relay."""

    def __init__(self, chat_id, cfg, *, readers=None):
        self.chat_id = chat_id
        self.cfg = cfg
        self.on = bool(chat_id) and enabled(cfg)
        self._audio = bytearray()
        self._pending = None
        self._tasks = set()
        self._readers = readers or (lambda words, audio: self._read(words, audio))

    def audio(self, raw: bytes) -> None:
        if not self.on:
            return
        self._audio += raw
        cap = SECONDS * RATE * 2
        if len(self._audio) > cap:
            del self._audio[:len(self._audio) - cap]

    async def _read(self, words, audio):
        jev_part, local_part = await asyncio.gather(
            ask_jev(self.chat_id, words, self.cfg), ask_local(audio),
            return_exceptions=True)
        out = {}
        for part in (jev_part, local_part):
            if isinstance(part, dict):
                out.update(part)
            else:
                out["error"] = type(part).__name__
        return out

    def _spawn(self, coro):
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def pause(self, words: str) -> None:
        if not self.on or self._pending is not None or not word_count(words):
            return
        audio = np.frombuffer(bytes(self._audio), dtype="<i2").astype(np.float32) / 32768
        self._pending = {"at": time.monotonic(), "words": word_count(words),
                         "task": self._spawn(self._readers(words, audio))}

    def partial(self, text: str) -> None:
        if self._pending is not None and word_count(text) > self._pending["words"]:
            self._settle("resumed")

    def commit(self) -> None:
        self._audio.clear()
        if self._pending is not None:
            self._settle("ended")

    def _settle(self, outcome):
        pending, self._pending = self._pending, None
        gap = round((time.monotonic() - pending["at"]) * 1000)

        async def finish():
            try:
                row = dict(await pending["task"])
            except Exception as e:
                row = {"error": type(e).__name__}
            row.update(words=pending["words"], outcome=outcome, gap_ms=gap)
            try:
                await asyncio.to_thread(store, self.chat_id, row)
            except Exception:
                log.debug("turn end row failed", exc_info=True)
            log.info("turn end shadow: chat=%s outcome=%s jev=%s local=%s gap_ms=%s",
                     self.chat_id, outcome, row.get("jev") or "none",
                     row.get("local"), gap)
        self._spawn(finish())
