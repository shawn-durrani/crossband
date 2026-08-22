#!/usr/bin/env python3
"""Prove the browse worker's OS sandbox profile on THIS machine (#148).

The profile ships soft-fail (a refused profile just logs and renders
unwrapped), so the only way it ever protects anything is if it compiles AND
holds its three properties on the actual deploy box. This probe checks all
of it in a few seconds, read-only, no app required:

    .venv/bin/python scripts/sandbox_probe.py

PASS/FAIL per property, exit 0 only when every one holds. Then prove a real
render end to end (real Chromium, real proxy, wrapped launch):

    .venv/bin/python -m pytest "tests/test_browse.py::test_real_render_through_a_live_proxy" -q
"""

import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend import sandbox  # noqa: E402

FAILS = 0


def check(name: str, ok: bool, detail: str = ""):
    global FAILS
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILS += 1


def run_sandboxed(profile: str, code: str) -> subprocess.CompletedProcess:
    return subprocess.run(["sandbox-exec", "-p", profile, sys.executable, "-c", code],
                          capture_output=True, text=True, timeout=30)


def main() -> int:
    if sys.platform != "darwin":
        print("This probe only means anything on macOS.")
        return 1
    home = os.environ["HOME"]
    repo = Path(__file__).resolve().parent.parent
    data_dir = os.environ.get("CROSSBAND_DATA_DIR") or str(repo / "data")
    env_file = str(repo / ".env")
    ssh_dir = os.path.join(home, ".ssh")
    profile_dir = tempfile.mkdtemp(prefix="sandbox-probe-")

    # An allowed and a denied loopback port, both with live listeners so a
    # refused connect is the SANDBOX, never a closed port.
    allowed = socket.socket()
    allowed.bind(("127.0.0.1", 0))
    allowed.listen(1)
    denied = socket.socket()
    denied.bind(("127.0.0.1", 0))
    denied.listen(1)
    aport, dport = allowed.getsockname()[1], denied.getsockname()[1]

    prof = sandbox.profile(profile_dir=profile_dir, port=aport,
                           data_dir=data_dir, env_file=env_file,
                           ssh_dir=ssh_dir)

    r = subprocess.run(["sandbox-exec", "-p", prof, "/usr/bin/true"],
                       capture_output=True, text=True)
    check("profile compiles and applies", r.returncode == 0,
          (r.stderr or "").strip()[:120])
    if r.returncode != 0:
        return 1

    r = run_sandboxed(prof, f"import socket; socket.create_connection(('127.0.0.1',{aport}),3)")
    check("proxy port reachable", r.returncode == 0, (r.stderr or "").strip()[-120:])

    r = run_sandboxed(prof, f"import socket; socket.create_connection(('127.0.0.1',{dport}),3)")
    check("other ports blocked", r.returncode != 0)

    r = run_sandboxed(prof, f"open({profile_dir!r}+'/probe-w','w').write('x')")
    check("writes inside the profile dir work", r.returncode == 0,
          (r.stderr or "").strip()[-120:])

    r = run_sandboxed(prof, f"open({home!r}+'/sandbox-probe-escape','w')")
    check("writes outside are blocked", r.returncode != 0
          and not os.path.exists(home + "/sandbox-probe-escape"))

    if os.path.exists(env_file):
        r = run_sandboxed(prof, f"open({env_file!r}).read()")
        check(".env unreadable", r.returncode != 0)
    else:
        print("skip  .env unreadable (no .env on this box)")

    db = os.path.join(data_dir, "chat.db")
    if os.path.exists(db):
        r = run_sandboxed(prof, f"open({db!r},'rb').read(10)")
        check("data dir unreadable", r.returncode != 0)
    else:
        print("skip  data dir unreadable (no chat.db at " + data_dir + ")")

    # Chromium's basics must survive the profile: read a framework path and
    # the playwright browser dir if one exists.
    r = run_sandboxed(prof, "open('/System/Library/CoreServices/SystemVersion.plist','rb').read(10)")
    check("system reads still work", r.returncode == 0)

    allowed.close()
    denied.close()
    print("\nall properties hold" if not FAILS else f"\n{FAILS} propert{'y' if FAILS==1 else 'ies'} FAILED")
    if not FAILS:
        print("now prove a real render:\n  .venv/bin/python -m pytest "
              '"tests/test_browse.py::test_real_render_through_a_live_proxy" -q')
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
