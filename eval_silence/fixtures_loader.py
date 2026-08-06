"""Loads  silence-eval fixture JSON files from one or more directories.
Mirrors eval_critic/fixtures_loader.py's shape/behavior for consistency."""

import json
from pathlib import Path

from eval_silence.schema import Fixture, FixtureError

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
    raise FixtureError(f"{path}: expected a JSON object or array of objects")


def load_fixtures(dirs: list[str | Path] | None = None,
                   include_builtin: bool = True) -> list[Fixture]:
    search_dirs: list[Path] = []
    if include_builtin:
        search_dirs.append(BUILTIN_FIXTURES_DIR)
    for d in (dirs or []):
        search_dirs.append(Path(d))

    fixtures: list[Fixture] = []
    seen_ids: dict[str, Path] = {}
    for d in search_dirs:
        if not d.is_dir():
            raise FixtureError(f"fixtures directory not found: {d}")
        for path in sorted(d.glob("*.json")):
            for raw in _load_file(path):
                fx = Fixture.from_dict(raw, source=str(path))
                if fx.id in seen_ids:
                    raise FixtureError(
                        f"duplicate fixture id {fx.id!r} in {path} (first seen in "
                        f"{seen_ids[fx.id]})")
                seen_ids[fx.id] = path
                fixtures.append(fx)
    if not fixtures:
        raise FixtureError(f"no fixtures found in {search_dirs}")
    return fixtures
