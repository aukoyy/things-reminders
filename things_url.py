"""Complete a Things to-do via the URL scheme. Never writes the SQLite DB."""

from __future__ import annotations

import logging
import subprocess
from urllib.parse import quote, urlencode

log = logging.getLogger("things_reminders")


def complete_todo(things_uuid: str, auth_token: str) -> None:
    query = urlencode(
        {
            "id": things_uuid,
            "auth-token": auth_token,
            "completed": "true",
        },
        quote_via=quote,
    )
    url = f"things:///update?{query}"
    result = subprocess.run(
        ["open", url],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"things:///update failed for {things_uuid}: {result.stderr.strip()}"
        )
    log.info("Opened things:///update completed=true id=%s", things_uuid)
