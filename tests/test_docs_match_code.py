"""Documentation claims that a test can actually check (#233).

The docs are accurate almost everywhere, and the failures found by review were
all of one kind: a document describing a behaviour that later changed
underneath it. Prose cannot be pinned in general. These four things can, so
they are, and each guards a claim that had already gone stale:

- the cache telemetry log format, which had drifted by six fields
- the `.env` credential list, which called itself definitive and was not
- KEY_ROLES against the capability registry, which promises to name every
  missing key
- the frontend test command, which named one of the three gates CI runs
"""
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _declared_env_vars(capabilities):
    """Every env var the capability registry declares, whatever shape it is
    keyed by. The setup wizard reads this registry, so it is the list the docs
    have to agree with."""
    caps = capabilities.CAPABILITIES
    entries = caps.values() if isinstance(caps, dict) else caps
    out = set()
    for cap in entries:
        out.update(cap.get("env") or ())
    return out


def _field_names(fmt):
    return set(re.findall(r"\b([a-z_]+)=", fmt))


def test_the_documented_cache_log_format_lists_every_field_emitted():
    """docs/COST_TELEMETRY.md's fence is the thing an operator greps with. It
    omitted chat, tools_hash, tools_n, changed, thinking and effort, and named
    `chat=` as `chat_id`, so a grep written from the doc matched nothing."""
    src = (REPO / "backend" / "providers.py").read_text()
    start = src.index('"claude_chat_cache speaker=')
    emitted = _field_names(src[start:src.index('",\n', start)])

    doc = (REPO / "docs" / "COST_TELEMETRY.md").read_text()
    fence = doc[doc.index("claude_chat_cache speaker="):]
    documented = _field_names(fence[:fence.index("```")])

    missing = sorted(emitted - documented)
    assert not missing, (
        "docs/COST_TELEMETRY.md does not document these emitted fields: "
        + ", ".join(missing))
    invented = sorted(documented - emitted)
    assert not invented, (
        "docs/COST_TELEMETRY.md documents fields the code does not emit: "
        + ", ".join(invented))


def test_every_declared_capability_env_var_is_in_the_env_credential_list():
    """docs/CONFIG.md calls its list the place API keys live. capabilities.py
    is the registry the setup wizard reads, so the two have to agree."""
    from backend import capabilities

    declared = _declared_env_vars(capabilities)
    doc = (REPO / "docs" / "CONFIG.md").read_text()
    missing = sorted(v for v in declared if f"`{v}`" not in doc)
    assert not missing, (
        "docs/CONFIG.md's .env list omits declared capability keys: "
        + ", ".join(missing))


def test_report_missing_keys_can_name_every_capability_key():
    """KEY_ROLES drives the startup report, whose docstring promises to name
    every missing key. It omitted both Reddit variables, so it could not."""
    from backend import capabilities, config

    declared = _declared_env_vars(capabilities)
    # GitHub is satisfied by a logged-in gh CLI, so its absence is not a
    # missing key the startup report should nag about.
    declared -= {"GH_TOKEN", "GITHUB_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"}
    missing = sorted(v for v in declared if v not in config.KEY_ROLES)
    assert not missing, (
        "config.KEY_ROLES cannot name these declared keys: " + ", ".join(missing))


def test_the_documented_frontend_command_is_the_one_ci_runs():
    """CONTRIBUTING and docs/TESTING gave `node --test frontend/src/*.test.js`,
    which is one of the three gates. A contributor got a green local run and a
    red CI, in a repo whose whole doc discipline exists to prevent that."""
    pkg = json.loads((REPO / "frontend" / "package.json").read_text())
    script = pkg["scripts"]["test"]
    for gate in ("eslint", "node --test", "render-smoke"):
        assert gate in script, f"frontend `npm test` no longer runs {gate}"

    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text()
    for gate in ("lint", "node --test", "render-smoke"):
        assert gate in ci, f"CI no longer runs {gate}"

    for doc in ("CONTRIBUTING.md", "docs/TESTING.md"):
        text = (REPO / doc).read_text()
        assert "npm --prefix frontend test" in text, (
            f"{doc} should give the command that runs all three gates")
        assert "node --test frontend/src/*.test.js" not in text, (
            f"{doc} still gives one gate as though it were the whole suite")
