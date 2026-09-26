"""Call the installed remctl CLI with --json. Never write the Reminders SQLite DB."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Any

from config import REMINDERS_LIST, remctl_bin

log = logging.getLogger("things_reminders")

_TIMEOUT = 90
UNSET = object()


class RemctlError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        returncode: int | None = None,
        payload: Any = None,
    ) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.payload = payload


class RemctlClient:
    def __init__(self, binary: str | None = None) -> None:
        self.binary = binary or remctl_bin()

    def lists(self) -> list[dict]:
        data = self.run(["lists", "--json"])
        if not isinstance(data, list):
            raise RemctlError("remctl lists --json did not return an array")
        return data

    def find_list(self, name: str) -> dict | None:
        wanted = name.casefold()
        for item in self.lists():
            if item.get("isGroup"):
                continue
            title = str(item.get("title") or "")
            if title == name or title.casefold() == wanted:
                return item
        return None

    def ensure_list(self, name: str, *, create: bool) -> dict | None:
        found = self.find_list(name)
        if found:
            return found
        if not create:
            return None
        log.info("creating Reminders list %r", name)
        self.run(["list-create", name, "--json"])
        found = self.find_list(name)
        if found is None:
            raise RemctlError(f"Created list {name!r} but remctl lists does not show it yet")
        return found

    def show_list(self, name: str = REMINDERS_LIST) -> list[dict]:
        data = self.run(["show", name, "--json"])
        if not isinstance(data, list):
            raise RemctlError(f"remctl show {name} --json did not return an array")
        return data

    def info(self, remctl_id: str) -> dict | None:
        try:
            data = self.run(["info", str(remctl_id), "--json"])
        except RemctlError as exc:
            if "not found" in str(exc).lower():
                return None
            raise
        if not isinstance(data, dict):
            raise RemctlError(f"remctl info {remctl_id} --json did not return an object")
        return data

    def pull(
        self,
        list_name: str,
        mapping: dict[str, str],
        *,
        create_list: bool,
    ) -> dict[str, dict]:
        found = self.ensure_list(list_name, create=create_list)
        items: dict[str, dict] = {}
        if found is None:
            log.info("Reminders list %r does not exist yet", list_name)
        else:
            for item in self.show_list(list_name):
                item_id = item.get("id")
                if item_id is None:
                    continue
                items[str(item_id)] = item
        missing = [rid for rid in mapping.values() if rid not in items]
        for remctl_id in missing:
            detail = self.info(remctl_id)
            if detail and detail.get("id") is not None:
                items[str(detail["id"])] = detail
        log.info("Pulled %d reminders from list %r", len(items), list_name)
        return items

    def add(
        self,
        *,
        title: str,
        notes: str,
        due_date: str | None,
        tags: tuple[str, ...] | list[str],
        list_name: str = REMINDERS_LIST,
        mapped_ids: set[str] | None = None,
    ) -> tuple[str, dict]:
        args = ["add", "-l", list_name]
        if notes:
            args += ["-n", notes]
        if due_date:
            args += ["-d", due_date]
        tag_csv = format_tags(tags)
        if tag_csv:
            args += ["--private", "-t", tag_csv]
        args += ["--json", "--", title]
        payload = self.run(args)
        if not isinstance(payload, dict):
            raise RemctlError("remctl add --json did not return an object")
        status = payload.get("status")
        if status not in {"created", "partial"}:
            raise RemctlError(
                f"remctl add failed: {payload.get('message') or payload}",
                payload=payload,
            )
        remctl_id = self._created_id(payload, title=title, list_name=list_name, mapped_ids=mapped_ids)
        if not remctl_id:
            raise RemctlError(
                f"Created {title!r} but remctl did not return a numeric id",
                payload=payload,
            )
        return remctl_id, payload

    def edit(
        self,
        remctl_id: str,
        *,
        title: str | None = None,
        notes: str | None = None,
        due: str | None | object = UNSET,
        tags: list[str] | tuple[str, ...] | None = None,
    ) -> dict:
        args = ["edit", str(remctl_id)]
        if title is not None:
            args += ["--title", title]
        if notes is not None:
            args += ["-n", notes]
        if due is not UNSET:
            args += ["-d", due if due else "clear"]
        if tags is not None:
            args += ["--private"]
            tag_csv = format_tags(tags)
            if tag_csv:
                args += ["--set-tags", tag_csv]
            else:
                args += ["--clear-tags"]
        args.append("--json")
        payload = self.run(args)
        if not isinstance(payload, dict):
            raise RemctlError(f"remctl edit {remctl_id} --json did not return an object")
        status = payload.get("status")
        if status not in {"updated", "unchanged", "partial"}:
            raise RemctlError(
                f"remctl edit failed: {payload.get('message') or payload}",
                payload=payload,
            )
        return payload

    def done(self, remctl_id: str) -> dict:
        payload = self.run(["done", str(remctl_id), "--json"])
        if not isinstance(payload, dict):
            raise RemctlError(f"remctl done {remctl_id} --json did not return an object")
        if payload.get("status") != "completed":
            raise RemctlError(
                f"remctl done failed: {payload.get('message') or payload}",
                payload=payload,
            )
        return payload

    def _created_id(
        self,
        payload: dict,
        *,
        title: str,
        list_name: str,
        mapped_ids: set[str] | None,
    ) -> str | None:
        numeric = payload.get("numericId")
        if numeric is not None:
            return str(numeric)
        already = mapped_ids or set()
        for item in self.show_list(list_name):
            item_id = item.get("id")
            if item_id is None:
                continue
            if item.get("title") == title and str(item_id) not in already:
                return str(item_id)
        return None

    def run(self, args: list[str]) -> Any:
        cmd = [self.binary, *args]
        log.debug("remctl %s", " ".join(args))
        env = os.environ.copy()
        home_bin = str(os.path.expanduser("~/bin"))
        path = env.get("PATH") or ""
        if home_bin not in path.split(":"):
            env["PATH"] = f"{home_bin}:{path}" if path else home_bin
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT,
                env=env,
            )
        except FileNotFoundError as exc:
            raise RemctlError(f"remctl binary not found: {self.binary}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RemctlError(f"remctl timed out after {_TIMEOUT}s: {' '.join(args)}") from exc
        if result.returncode != 0:
            raise _error_from_result(result)
        if not (result.stdout or "").strip():
            raise RemctlError(f"remctl produced no JSON: {' '.join(args)}")
        return _parse_json(result.stdout)


def canonical_tag(name: str) -> str:
    """Tag form Reminders actually stores.

    Reminders strips whitespace from synced tags, so ``Side quest`` comes
    back as ``Sidequest``. Comparing or writing the spaced form makes every
    run look dirty and rewrites the reminder.
    """
    return "".join(ch for ch in str(name).replace(",", " ") if not ch.isspace())


def format_tags(tags: tuple[str, ...] | list[str]) -> str:
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        name = canonical_tag(raw)
        if not name or name in seen:
            continue
        seen.add(name)
        cleaned.append(name)
    return ",".join(cleaned)


def snapshot_item(item: dict) -> dict:
    raw_due = item.get("dueDate")
    due_date = str(raw_due)[:10] if raw_due else None
    tags = item.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    names = []
    for tag in tags:
        name = canonical_tag(str(tag))
        if name:
            names.append(name)
    return {
        "title": item.get("title") or "",
        "notes": item.get("notes") or "",
        "tags": names,
        "due_date": due_date,
        "completed": bool(item.get("completed") or item.get("completionDate")),
    }


def _parse_json(text: str) -> Any:
    payload = text.strip()
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        for index, char in enumerate(payload):
            if char in "{[":
                return json.loads(payload[index:])
        raise RemctlError("remctl stdout was not JSON") from None


def _error_from_result(result: subprocess.CompletedProcess[str]) -> RemctlError:
    err = (result.stderr or "").strip()
    out = (result.stdout or "").strip()
    for text in (err, out):
        if not text:
            continue
        try:
            payload = json.loads(text[text.find("{") :] if "{" in text else text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            message = payload.get("message") or payload.get("error") or text
            return RemctlError(str(message), returncode=result.returncode, payload=payload)
    return RemctlError(err or out or f"remctl exited {result.returncode}", returncode=result.returncode)
