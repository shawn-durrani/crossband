"""Loads fixture JSON files from one or more directories.

The built-in seed corpus (eval_critic/fixtures/*.json) is fully synthetic and
committed to Git. Private replay fixtures (real historical drafts, scrubbed
or not) belong OUTSIDE this repo — pass their directory via
`--fixtures-dir /path/outside/git` (CLI) or `extra_dirs=[...]` (API); they are
loaded the same way but never committed here. Each file may contain either a
single fixture object or a JSON array of fixture objects.
"""

import json
from pathlib import Path

from eval_critic.schema import Fixture, FixtureError

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
    """dirs: additional directories to load *.json fixtures from (e.g. a
    private, non-Git replay set). include_builtin=False runs ONLY the
    supplied dirs — useful to eval a private set in isolation."""
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
