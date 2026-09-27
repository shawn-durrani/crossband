"""A second crossband, started for one run, that can't touch yours.

The app reads its settings and keys from the checkout it runs from: the
committed config.json, then config.local.json, then .env. A copy started
from your own checkout would pick up your memory token, your connected
apps and every other key. So the rig never starts it there. It copies the
backend code and config.json alone into a run folder, where there's no
config.local.json and no .env, and starts it from that copy:

  * its own data folder, fresh for the run, so its people, voice banks,
    chats and anchor store are the rig's alone
  * its own port, never one of the fleet's
  * memory pointed at a closed port, and no memory token, so nothing
    reaches membro
  * no sibling apps, no Funnel check, no guest repos, no MCP servers
  * a process environment built from nothing: the path and home
    variables, the settings below, and the two keys it needs (ElevenLabs
    to transcribe, Anthropic for the one model call that reads each turn)

The voice models are shared across runs through a folder in the rig's
cache, so they download once.
"""

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FLEET_PORTS = set(range(8901, 8905)) | {8910}
DEFAULT_PORT = 8920
NOWHERE = "http://127.0.0.1:1"
# What the instance may inherit from the rig's own environment. Everything
# else, keys and CROSSBAND_ settings included, is left behind.
PASS_THROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR",
                "USER", "LOGNAME", "SHELL", "SSL_CERT_FILE")
KEYS = ("ELEVENLABS_API_KEY", "ANTHROPIC_API_KEY")
START_TIMEOUT_S = 90.0
STOP_TIMEOUT_S = 20.0


class InstanceError(RuntimeError):
    pass


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) != 0


def check_port(port: int) -> None:
    if port in FLEET_PORTS:
        raise InstanceError(f"port {port} belongs to the fleet; pick another")
    if not port_free(port):
        raise InstanceError(f"something is already listening on port {port}")


def snapshot_code(dest: Path, repo: Path = REPO) -> Path:
    """Copy the backend package and config.json, nothing else, into
    `dest/code`. What runs is the code as it is in this checkout now."""
    code = Path(dest) / "code"
    if code.exists():
        shutil.rmtree(code)
    code.mkdir(parents=True)
    shutil.copytree(repo / "backend", code / "backend",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(repo / "config.json", code / "config.json")
    return code


def instance_env(port: int, data_dir: Path, keys: dict, *, owner: str,
                 diariser: str = "", calibrated: bool = True,
                 environ=None) -> dict:
    """The instance's whole environment. Pure, so the tests can pin what
    goes in and what's left out."""
    environ = os.environ if environ is None else environ
    env = {k: environ[k] for k in PASS_THROUGH if environ.get(k)}
    env.update({
        "CROSSBAND_PORT": str(port),
        "CROSSBAND_DATA_DIR": str(data_dir),
        "CROSSBAND_MEMORY_URL": NOWHERE,
        "CROSSBAND_USER_NAME": owner,
        "CROSSBAND_SIBLING_APPS": "{}",
        "CROSSBAND_FUNNEL_CHECK_S": "0",
        "CROSSBAND_LOG_LEVEL": "INFO",
        "CROSSBAND_VOICE_CALIBRATED_SCORER": "true" if calibrated else "false",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    if diariser:
        env["CROSSBAND_DIARIZE_SHADOW_URL"] = diariser
    for k in KEYS:
        if keys.get(k):
            env[k] = keys[k]
    return env


class Instance:
    """One isolated crossband, started in `run_dir` and stopped by stop()."""

    def __init__(self, run_dir, *, port=DEFAULT_PORT, keys=None, owner="Alex",
                 diariser="", calibrated=True, models_dir=None,
                 python=None, repo=REPO):
        self.run_dir = Path(run_dir)
        self.port = port
        self.keys = dict(keys or {})
        self.owner = owner
        self.diariser = diariser
        self.calibrated = calibrated
        self.models_dir = Path(models_dir) if models_dir else None
        self.python = python or sys.executable
        self.repo = Path(repo)
        self.proc = None
        self.data_dir = self.run_dir / "data"
        self.log_path = self.run_dir / "instance.log"

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        check_port(self.port)
        live_data = (self.repo / "data").resolve()
        if self.data_dir.resolve() == live_data or \
                live_data in self.data_dir.resolve().parents:
            raise InstanceError("the rig's data folder can't be the app's own")
        if self.data_dir.exists() and any(self.data_dir.iterdir()):
            raise InstanceError(f"{self.data_dir} isn't empty; the rig "
                                "starts from a fresh data folder")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.data_dir, 0o700)
        if self.models_dir:
            self.models_dir.mkdir(parents=True, exist_ok=True)
            link = self.data_dir / "voice_models"
            if not link.exists():
                link.symlink_to(self.models_dir, target_is_directory=True)
        code = snapshot_code(self.run_dir, self.repo)
        env = instance_env(self.port, self.data_dir, self.keys,
                           owner=self.owner, diariser=self.diariser,
                           calibrated=self.calibrated)
        log = open(self.log_path, "ab")
        self.proc = subprocess.Popen(
            [self.python, "-m", "backend"], cwd=str(code), env=env,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        log.close()
        self._wait_up()

    def _wait_up(self) -> None:
        import httpx
        deadline = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise InstanceError(f"the instance exited at start; see "
                                    f"{self.log_path}")
            try:
                r = httpx.get(f"{self.base}/api/voice/health", timeout=2.0,
                              trust_env=False)
                if r.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        self.stop()
        raise InstanceError(f"the instance didn't answer in "
                            f"{START_TIMEOUT_S:.0f}s; see {self.log_path}")

    def stop(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        self.proc.send_signal(signal.SIGTERM)
        try:
            self.proc.wait(STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(5)

    def remove(self, log_too: bool = False) -> None:
        """Delete the run's code copy and data folder, and the whole run
        folder with its log when `log_too`. The shared model folder is
        only linked from it and stays."""
        self.stop()
        link = self.data_dir / "voice_models"
        if link.is_symlink():
            link.unlink()
        for part in (self.data_dir, self.run_dir / "code"):
            if part.exists():
                shutil.rmtree(part)
        if log_too and self.log_path.exists():
            self.log_path.unlink()
        if log_too and self.run_dir.exists() and not any(self.run_dir.iterdir()):
            self.run_dir.rmdir()
