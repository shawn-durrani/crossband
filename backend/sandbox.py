"""macOS OS-sandbox for the browse worker (#148, point 1).

Defence in depth on top of the scrubbed environment, the vetting egress
proxy and Chromium's own sandbox - NEVER the boundary. The profile buys
three properties an escaped renderer would otherwise have for free:

- no IP traffic except the vetting proxy's loopback port, so a direct
  connection cannot bypass the proxy's SSRF policy or the page budget;
- no file writes outside the throwaway profile directory (plus the darwin
  per-user temp tree and /dev, which Chromium needs to run at all);
- no reads of the crown jewels this app knows about: its own data
  directory, its own .env, and ~/.ssh. Reads stay broad otherwise -
  Chromium needs frameworks, fonts and the dyld cache from all over.

Soft-fail is the contract (the issue's own words): sandbox-exec missing,
the platform not being macOS, or a profile the OS refuses to compile all
mean log once and render exactly as before. One refusal stops further
wrapping for the process lifetime - a broken profile must cost one retry,
not one retry per page view.

sandbox-exec is deprecated but functional; Apple's own daemons still ship
profiles. If it disappears in a future macOS, available() goes false and
the app quietly returns to today's posture.
"""

import logging
import shutil
import sys

log = logging.getLogger("crossband")

_broken = False  # set on the first OS refusal; checked by available()

# Profile text. Semantics: SBPL applies the LAST matching rule, so each
# deny narrows the allow-default and each later allow punches back the
# specific holes the worker needs.
_PROFILE = """(version 1)
(allow default)
; --- network: no IP traffic except the vetting proxy's loopback port ---
(deny network-outbound (remote ip))
(allow network-outbound (remote tcp "localhost:{port}"))
(deny network-bind (local ip))
(deny network-inbound (local ip))
; --- writes: the throwaway profile dir, darwin per-user temp, devices ---
(deny file-write*)
(allow file-write* (subpath "{profile_dir}")
                   (subpath "/private/var/folders")
                   (subpath "/dev"))
; --- reads stay broad (frameworks, fonts, dyld) minus the crown jewels ---
(deny file-read* (subpath "{data_dir}")
                 (literal "{env_file}")
                 (subpath "{ssh_dir}"))
"""


def available() -> bool:
    """Can this process wrap the worker at all? False is always safe: the
    caller just launches the worker exactly as before."""
    return (sys.platform == "darwin"
            and not _broken
            and shutil.which("sandbox-exec") is not None)


def profile(profile_dir: str, port: int, data_dir: str, env_file: str,
            ssh_dir: str) -> str:
    """The SBPL profile for one render. Raises ValueError on any path that
    could break out of its quoted string - the caller treats that as
    'sandbox unavailable', never as a reason to ship a mangled profile."""
    fields = {"profile_dir": profile_dir, "data_dir": data_dir,
              "env_file": env_file, "ssh_dir": ssh_dir}
    for name, value in fields.items():
        if not value or '"' in value or "\\" in value or "\n" in value:
            raise ValueError(f"{name} cannot be quoted into a sandbox profile")
    if not (0 < int(port) < 65536):
        raise ValueError("proxy port out of range")
    return _PROFILE.format(port=int(port), **fields)


def wrap(argv: list, profile_text: str) -> list:
    """The worker's argv, wrapped."""
    return ["sandbox-exec", "-p", profile_text] + list(argv)


def refused(stdout: bytes, stderr: bytes) -> bool:
    """Did this run die at the sandbox itself rather than inside the worker?
    An OS refusal produces no worker output at all and names sandbox-exec
    (or its apply call) on stderr; a render that failed INSIDE a healthy
    sandbox never matches, so a real page error is still reported as one."""
    if (stdout or b"").strip():
        return False
    s = (stderr or b"")[:400].decode("utf-8", "replace").lower()
    return "sandbox-exec" in s or "sandbox_apply" in s


def mark_broken(detail: str) -> None:
    """One strike: log the refusal once and stop wrapping for this process.
    The render that hit it is retried unwrapped by the caller."""
    global _broken
    if not _broken:
        log.warning(
            "OS sandbox refused its profile - rendering WITHOUT it from now "
            "on (defence in depth only, #148). Run scripts/sandbox_probe.py "
            "on this machine to debug. %s", detail[:300])
    _broken = True


def _reset_for_tests() -> None:
    global _broken
    _broken = False
