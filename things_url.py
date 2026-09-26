"""Write to Things via the URL scheme. Never writes the SQLite DB."""

from __future__ import annotations

import json
import logging
import subprocess
from urllib.parse import quote, urlencode

log = logging.getLogger("things_reminders")

# `open` rejects very long URLs. Stay under that with room for encoding.
_MAX_URL_LENGTH = 32_000


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
    try:
        _open(url)
    except RuntimeError as exc:
        raise RuntimeError(f"things:///update failed for {things_uuid}: {exc}") from exc
    log.info("Opened things:///update completed=true id=%s", things_uuid)


def apply_json(operations: list[dict], auth_token: str) -> None:
    """Send create/update operations in one or more things:///json calls.

    reveal=false avoids navigating to each item. Things still comes forward,
    same as the completion URL. The URL is never logged: it carries the auth token.
    """
    if not operations:
        return
    for batch in _batches(operations, auth_token):
        payload = json.dumps(batch, ensure_ascii=False, separators=(",", ":"))
        query = urlencode(
            {"data": payload, "auth-token": auth_token, "reveal": "false"},
            quote_via=quote,
        )
        url = f"things:///json?{query}"
        _open(url)
        log.info("Opened things:///json operations=%d", len(batch))


def _batches(operations: list[dict], auth_token: str) -> list[list[dict]]:
    batches: list[list[dict]] = []
    current: list[dict] = []
    for operation in operations:
        trial = current + [operation]
        if current and _encoded_length(trial, auth_token) > _MAX_URL_LENGTH:
            batches.append(current)
            current = [operation]
            continue
        current = trial
    if current:
        batches.append(current)
    return batches


def _encoded_length(operations: list[dict], auth_token: str) -> int:
    payload = json.dumps(operations, ensure_ascii=False, separators=(",", ":"))
    query = urlencode(
        {"data": payload, "auth-token": auth_token, "reveal": "false"},
        quote_via=quote,
    )
    return len(f"things:///json?{query}")


def _open(url: str) -> None:
    result = subprocess.run(["open", url], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "things:/// open failed")
