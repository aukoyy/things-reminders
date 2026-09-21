"""Paths, secrets, and constants. Secrets come from env or Keychain — never a plist."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

APP_NAME = "things-reminders"
KEYCHAIN_SERVICE = "things-reminders"
REMINDERS_LIST = "Things"
LIST_TAGS = ("Inbox", "Anytime", "Someday")

HOME = Path.home()
ROOT = Path(__file__).resolve().parent
STATE_DIR = HOME / "Library" / "Application Support" / APP_NAME
LOG_DIR = HOME / "Library" / "Logs" / APP_NAME
STATE_PATH = STATE_DIR / "mapping.json"
LOG_PATH = LOG_DIR / "sync.log"
ENV_PATH = ROOT / ".env"


def load_dotenv(path: Path = ENV_PATH) -> None:
    """Load KEY=VALUE pairs from .env without overriding existing env vars."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ[key] = value


def ensure_dirs() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def _keychain(account: str) -> str | None:
    result = subprocess.run(
        [
            "security",
            "find-generic-password",
            "-s",
            KEYCHAIN_SERVICE,
            "-a",
            account,
            "-w",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def remctl_bin() -> str:
    """Resolve the installed remctl CLI. Prefer REMCTL_BIN, then PATH, then ~/bin."""
    load_dotenv()
    explicit = os.environ.get("REMCTL_BIN")
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise SystemExit(f"REMCTL_BIN={explicit} is not a file.")
        return str(path.resolve())
    found = shutil.which("remctl")
    if found:
        return found
    fallback = HOME / "bin" / "remctl"
    if fallback.is_file():
        return str(fallback)
    raise SystemExit(
        "remctl not found. Install remctl and put it on PATH or at ~/bin/remctl, "
        "or set REMCTL_BIN."
    )


def things_auth_token() -> str | None:
    load_dotenv()
    token = os.environ.get("THINGS_AUTH_TOKEN") or _keychain("things-auth-token")
    if token:
        return token
    try:
        import things

        return things.token()
    except Exception:
        return None
