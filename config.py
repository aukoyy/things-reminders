"""Paths, secrets, and constants. Secrets come from env or Keychain — never a plist."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "things-reminders"
KEYCHAIN_SERVICE = "things-reminders"
REMINDERS_LIST = "Things"
LIST_TAGS = ("Inbox", "Anytime", "Someday")


@dataclass(frozen=True)
class ListPair:
    """Reminders list ↔ Things project or area. block_tag skips Things → Reminders."""

    reminders_list: str
    things_list: str
    block_tag: str | None = None


# Hardcoded shared lists. Reminders name first, Things name second.
# A Things to-do is in the pair when it is in that project, or when it sits
# directly in that area (not inside a project). Nested projects stay on the
# one-way sync. Missing Things containers are created as projects.
LIST_PAIRS: tuple[ListPair, ...] = (
    ListPair("Ø Full Vase", "💐 Full Vase"),
    ListPair("Aukners Todo", "🏡 Aukners"),
    ListPair("Handleliste", "🛒 Handleliste", block_tag="ma"),
)


def things_list_for_todo(project_title: str, area_title: str) -> str | None:
    """Paired Things list for a to-do, if it should stay off the one-way sync."""
    titles = {pair.things_list for pair in LIST_PAIRS}
    project_title = project_title.strip()
    area_title = area_title.strip()
    if project_title in titles:
        return project_title
    if not project_title and area_title in titles:
        return area_title
    return None


def pair_for_things_list(title: str) -> ListPair | None:
    for pair in LIST_PAIRS:
        if pair.things_list == title:
            return pair
    return None


def pair_for_reminders_list(title: str) -> ListPair | None:
    folded = title.casefold()
    for pair in LIST_PAIRS:
        if pair.reminders_list == title or pair.reminders_list.casefold() == folded:
            return pair
    return None

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
