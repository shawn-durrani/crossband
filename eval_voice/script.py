"""One script is one made-up conversation: who is in it, what each of them
says, in order, and what the mix does to each line.

A turn is one line from one person, and it's what the app hears as one
spoken turn. A turn can carry a second line, `over`, that starts part of
the way through the first one, which is how the rig makes crosstalk. A
line can be quieter than the rest (`gain_db`), and the whole
conversation can sit on a noise bed. A turn can also carry the
introductions and spoken instructions it holds, so the run can say
whether the app heard them.

JSON shape, one object per file:

    {"id": "crosstalk", "about": "what this script tests",
     "cast": ["Alex", "Sam", "Dave"],
     "noise": {"kind": "cafe", "snr_db": 20},
     "turns": [
       {"speaker": "Alex", "text": "..."},
       {"speaker": "Sam", "text": "...", "gain_db": -10},
       {"speaker": "Sam", "text": "...",
        "over": {"speaker": "Dave", "text": "...", "at": 0.5}},
       {"speaker": "Alex", "text": "...", "events": [{"introduce": "Mateo"}]},
       {"speaker": "Alex", "text": "...", "events": [{"room": "on"}]}]}

Every name has to be on the synthetic roster (cast.py), so a script can
never carry a real person's name.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from eval_voice import cast as cast_mod

BUILTIN_DIR = Path(__file__).resolve().parent / "scripts"
NOISE_KINDS = ("cafe", "road")
MAX_TEXT = 300
GAIN_RANGE = (-30.0, 6.0)
SNR_RANGE = (0.0, 40.0)
GAP_RANGE = (0.0, 5.0)
DEFAULT_GAP_S = 0.6


class ScriptError(ValueError):
    """A script file or object failed validation."""


@dataclass(frozen=True)
class Line:
    speaker: str
    text: str
    gain_db: float = 0.0


@dataclass(frozen=True)
class Over:
    line: Line
    at: float          # where it starts, as a share of the main line's speech


@dataclass(frozen=True)
class Turn:
    line: Line
    over: Over | None = None
    events: tuple = ()      # ({"introduce": name} | {"room": "on"|"off"}, ...)
    gap_s: float = DEFAULT_GAP_S

    @property
    def speaker(self) -> str:
        return self.line.speaker


@dataclass
class Script:
    id: str
    cast: list
    turns: list
    about: str = ""
    noise: dict | None = None
    source: str = "fixed"
    path: str = ""

    def lines(self):
        """Every line the script speaks, main lines and crosstalk alike."""
        for t in self.turns:
            yield t.line
            if t.over:
                yield t.over.line

    def to_dict(self) -> dict:
        def line(ln):
            d = {"speaker": ln.speaker, "text": ln.text}
            if ln.gain_db:
                d["gain_db"] = ln.gain_db
            return d
        turns = []
        for t in self.turns:
            d = line(t.line)
            if t.over:
                d["over"] = {**line(t.over.line), "at": t.over.at}
            if t.events:
                d["events"] = [dict(e) for e in t.events]
            if t.gap_s != DEFAULT_GAP_S:
                d["gap_s"] = t.gap_s
            turns.append(d)
        out = {"id": self.id, "about": self.about, "cast": list(self.cast),
               "noise": self.noise, "turns": turns}
        return out


def _number(value, lo, hi, what, where):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScriptError(f"{where}: {what} must be a number")
    if not lo <= float(value) <= hi:
        raise ScriptError(f"{where}: {what} {value} is outside {lo} to {hi}")
    return float(value)


def _line(d, cast, where) -> Line:
    if not isinstance(d, dict):
        raise ScriptError(f"{where}: a line must be an object")
    speaker = d.get("speaker")
    if speaker not in cast:
        raise ScriptError(f"{where}: speaker {speaker!r} isn't in the "
                          f"script's cast {cast}")
    text = d.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ScriptError(f"{where}: a line needs text")
    if len(text) > MAX_TEXT:
        raise ScriptError(f"{where}: a line is at most {MAX_TEXT} characters")
    gain = _number(d.get("gain_db", 0.0), *GAIN_RANGE, "gain_db", where)
    return Line(speaker=speaker, text=text.strip(), gain_db=gain)


def _events(raw, where) -> tuple:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ScriptError(f"{where}: events must be a list")
    out = []
    for e in raw:
        if not isinstance(e, dict) or len(e) != 1:
            raise ScriptError(f"{where}: an event is one key, "
                              "introduce or room")
        (kind, value), = e.items()
        if kind == "introduce":
            if value not in cast_mod.CAST:
                raise ScriptError(f"{where}: introduce {value!r} isn't on "
                                  "the synthetic roster")
        elif kind == "room":
            if value not in ("on", "off"):
                raise ScriptError(f"{where}: room is on or off")
        else:
            raise ScriptError(f"{where}: unknown event {kind!r}")
        out.append({kind: value})
    return tuple(out)


def from_dict(d: dict, source: str = "<unknown>", origin: str = "fixed") -> Script:
    if not isinstance(d, dict):
        raise ScriptError(f"{source}: a script must be an object")
    sid = d.get("id")
    if not isinstance(sid, str) or not sid.strip():
        raise ScriptError(f"{source}: a script needs an id")
    where = f"{source} ({sid})"
    cast = d.get("cast")
    if not isinstance(cast, list) or not cast:
        raise ScriptError(f"{where}: cast must be a list of names")
    unknown = [n for n in cast if n not in cast_mod.CAST]
    if unknown:
        raise ScriptError(f"{where}: {unknown} aren't on the synthetic roster "
                          f"{list(cast_mod.CAST)}")
    if len(set(cast)) != len(cast):
        raise ScriptError(f"{where}: a name is in the cast twice")
    noise = d.get("noise")
    if noise is not None:
        if not isinstance(noise, dict) or noise.get("kind") not in NOISE_KINDS:
            raise ScriptError(f"{where}: noise kind is one of {NOISE_KINDS}")
        noise = {"kind": noise["kind"],
                 "snr_db": _number(noise.get("snr_db", 20.0), *SNR_RANGE,
                                   "snr_db", where)}
    raw_turns = d.get("turns")
    if not isinstance(raw_turns, list) or not raw_turns:
        raise ScriptError(f"{where}: a script needs turns")
    turns = []
    for i, t in enumerate(raw_turns):
        tw = f"{where} turn {i}"
        line = _line(t, cast, tw)
        over = None
        if t.get("over") is not None:
            o = t["over"]
            oline = _line(o, cast, f"{tw} over")
            if oline.speaker == line.speaker:
                raise ScriptError(f"{tw}: crosstalk needs a second person")
            at = _number(o.get("at", 0.5), 0.0, 0.95, "at", tw)
            over = Over(line=oline, at=at)
        gap = _number(t.get("gap_s", DEFAULT_GAP_S), *GAP_RANGE, "gap_s", tw)
        turns.append(Turn(line=line, over=over,
                          events=_events(t.get("events"), tw), gap_s=gap))
    return Script(id=sid.strip(), cast=list(cast), turns=turns,
                  about=str(d.get("about") or ""), noise=noise,
                  source=origin, path=source)


def load_file(path: Path, origin: str = "fixed") -> Script:
    try:
        data = json.loads(Path(path).read_text())
    except json.JSONDecodeError as e:
        raise ScriptError(f"{path}: invalid JSON ({e})") from e
    return from_dict(data, source=str(path), origin=origin)


def load_scripts(dirs=(), include_builtin: bool = True, only=None) -> list:
    """Every script in the built-in folder and any extra folders, in file
    name order, optionally narrowed to the ids in `only`."""
    search = ([BUILTIN_DIR] if include_builtin else []) + [Path(d) for d in dirs]
    scripts, seen = [], {}
    for d in search:
        if not d.is_dir():
            continue
        origin = "fixed" if d == BUILTIN_DIR else "extra"
        for path in sorted(d.glob("*.json")):
            s = load_file(path, origin=origin)
            if s.id in seen:
                raise ScriptError(f"script id {s.id!r} is in both "
                                  f"{seen[s.id]} and {path}")
            seen[s.id] = path
            scripts.append(s)
    if only:
        wanted = list(only)
        missing = [w for w in wanted if w not in seen]
        if missing:
            raise ScriptError(f"no script with id {missing}; have {sorted(seen)}")
        by_id = {s.id: s for s in scripts}
        scripts = [by_id[w] for w in wanted]
    if not scripts:
        raise ScriptError(f"no scripts found in {[str(p) for p in search]}")
    return scripts
