"""Loads fixture JSON files. The committed corpus under fixtures/ is made
up. A set built from real turns belongs outside the repository and is
passed with --fixtures-dir, as the other harnesses do it."""

import json
from pathlib import Path

from eval_intent.schema import Fixture, FixtureError

BUILTIN_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def _load_file(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise FixtureError(f"{path}: invalid JSON ({e})") from e
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return data
    raise FixtureError(f"{path}: expected a JSON object or array")


def load_fixtures(dirs=None, include_builtin: bool = True) -> list[Fixture]:
    search: list[Path] = []
    if include_builtin:
        search.append(BUILTIN_FIXTURES_DIR)
    search += [Path(d) for d in (dirs or [])]
    fixtures, seen = [], {}
    for d in search:
        if not d.is_dir():
            raise FixtureError(f"fixtures directory not found: {d}")
        for path in sorted(d.glob("*.json")):
            for raw in _load_file(path):
                fx = Fixture.from_dict(raw, source=str(path))
                if fx.id in seen:
                    raise FixtureError(f"duplicate fixture id {fx.id!r} in {path}")
                seen[fx.id] = path
                fixtures.append(fx)
    if not fixtures:
        raise FixtureError(f"no fixtures found in {search}")
    return fixtures
