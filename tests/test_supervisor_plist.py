"""The launchd supervisor is a placeholder template rendered
by ops/install-supervisor.sh at install time. These tests pin the two things
that make it safe and correct without needing macOS or launchd:

  1. The committed template carries NO real path - only placeholders - so no
     personal home directory can leak into the repo (the same discipline the
     leak scanner enforces).
  2. After substitution the result is a valid plist that actually configures a
     self-restarting, run-at-load agent pointing at start.sh.

Keyless and cross-platform: plistlib is stdlib, so this runs in CI on Linux
where `plutil` does not exist.
"""
import plistlib
import re
import stat
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TEMPLATE = REPO / "ops" / "dev.crossband.server.plist.template"
INSTALLER = REPO / "ops" / "install-supervisor.sh"

PLACEHOLDERS = ("{{REPO_DIR}}", "{{HOME}}", "{{PATH}}")


def _render(repo_dir="/opt/example/sideband", home="/home/example",
            path="/usr/bin:/bin"):
    text = TEMPLATE.read_text()
    return (text.replace("{{REPO_DIR}}", repo_dir)
                .replace("{{HOME}}", home)
                .replace("{{PATH}}", path))


def test_template_and_installer_present():
    assert TEMPLATE.exists(), "the plist template ships in ops/"
    assert INSTALLER.exists(), "the installer ships in ops/"


def test_template_has_no_real_path_only_placeholders():
    """No committed personal path: every machine-specific value is a
    placeholder the installer fills in. This is what keeps the leak scanner
    green and the template reusable."""
    text = TEMPLATE.read_text()
    assert "/Users/" not in text, "a real macOS home path leaked into the template"
    for ph in PLACEHOLDERS:
        assert ph in text, f"template lost its {ph} placeholder"


def test_installer_is_executable():
    mode = INSTALLER.stat().st_mode
    assert mode & stat.S_IXUSR, "install-supervisor.sh must be executable"


def test_rendered_plist_is_valid_and_self_restarting():
    rendered = _render()
    assert "{{" not in rendered, "a placeholder survived substitution"
    doc = plistlib.loads(rendered.encode())

    assert doc["Label"] == "dev.crossband.server"
    # the two keys that make it a supervisor: come up on load, stay up
    assert doc["RunAtLoad"] is True
    assert doc["KeepAlive"] is True
    # it launches the project's real entrypoint in the repo it was rendered for
    assert doc["ProgramArguments"][-1].endswith("/start.sh")
    assert doc["WorkingDirectory"] == "/opt/example/sideband"
    # a throttle so a hard boot failure can't spin
    assert int(doc["ThrottleInterval"]) >= 1


def test_installer_takes_over_cleanly_and_validates():
    """The installer must not blindly load: it boots out any prior agent,
    frees the port, and refuses to load a plist with a leftover placeholder."""
    src = INSTALLER.read_text()
    assert "launchctl bootout" in src, "must remove a prior agent before loading"
    assert "launchctl bootstrap" in src, "must load the new agent"
    assert re.search(r"grep -q '\{\{'", src), \
        "must fail if a placeholder was left unsubstituted"
    assert "plutil -lint" in src, "must validate the generated plist"


def test_service_log_rotates_at_boot():
    """launchd appends every spawn's stdout and stderr to data/service.log
    and never rotates it, so start.sh must, at boot (#243). The rotation has
    to be copy-then-truncate (a rename strands launchd's already-open
    descriptor on the archived file), size-gated so small logs are left
    alone, and it must keep one prior generation rather than discarding the
    tail."""
    src = (REPO / "start.sh").read_text()
    assert "MAX_LOG_BYTES" in src, "rotation must be size-gated"
    assert 'cp -p "$LOG" "$LOG.1"' in src, "must archive one prior generation"
    assert ': > "$LOG"' in src, "must truncate in place, keeping launchd's fd"
    assert 'mv "$LOG"' not in src, "rename-rotation strands the open descriptor"
