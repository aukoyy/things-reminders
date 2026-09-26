"""Load and save Things UUID ↔ remctl numeric reminder id."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from config import STATE_PATH, ensure_dirs


@dataclass
class PairLink:
    """Last synced content for one Things to-do ↔ reminder pair."""

    remctl_id: str
    reminders_list: str
    things_list: str
    title: str
    notes: str
    due: str | None

    def as_dict(self) -> dict:
        return {
            "remctl_id": self.remctl_id,
            "reminders_list": self.reminders_list,
            "things_list": self.things_list,
            "title": self.title,
            "notes": self.notes,
            "due": self.due,
        }


@dataclass
class PendingCreate:
    """A Reminders → Things create whose new to-do id was not visible yet."""

    remctl_id: str
    reminders_list: str
    things_list: str
    title: str
    notes: str
    due: str | None
    not_before: str

    def as_dict(self) -> dict:
        return {
            "remctl_id": self.remctl_id,
            "reminders_list": self.reminders_list,
            "things_list": self.things_list,
            "title": self.title,
            "notes": self.notes,
            "due": self.due,
            "not_before": self.not_before,
        }


@dataclass
class State:
    mapping: dict[str, str] = field(default_factory=dict)
    pairs: dict[str, PairLink] = field(default_factory=dict)
    pending: list[PendingCreate] = field(default_factory=list)

    def dump(self) -> dict:
        return {
            "mapping": self.mapping,
            "pairs": {uuid: link.as_dict() for uuid, link in self.pairs.items()},
            "pending": [item.as_dict() for item in self.pending],
        }

    @classmethod
    def load(cls, path: Path = STATE_PATH) -> State:
        ensure_dirs()
        if not path.exists():
            return cls()
        with path.open(encoding="utf-8") as fh:
            raw = json.load(fh)
        mapping = dict(raw.get("mapping") or {})
        raw_pairs = raw.get("pairs") or {}
        if not isinstance(raw_pairs, dict):
            raw_pairs = {}
        pairs: dict[str, PairLink] = {}
        for uuid, item in raw_pairs.items():
            link = _pair_link(item)
            if link is not None:
                pairs[str(uuid)] = link
        raw_pending = raw.get("pending") or []
        if not isinstance(raw_pending, list):
            raw_pending = []
        pending: list[PendingCreate] = []
        for raw_item in raw_pending:
            item = _pending(raw_item)
            if item is not None:
                pending.append(item)
        return cls(
            mapping={str(key): str(value) for key, value in mapping.items()},
            pairs=pairs,
            pending=pending,
        )

    def save(self, path: Path = STATE_PATH) -> None:
        ensure_dirs()
        payload = json.dumps(self.dump(), indent=2, sort_keys=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".mapping.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.write("\n")
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def _pair_link(raw: object) -> PairLink | None:
    if not isinstance(raw, dict):
        return None
    remctl_id = raw.get("remctl_id")
    reminders_list = raw.get("reminders_list")
    things_list = raw.get("things_list")
    if not remctl_id or not reminders_list or not things_list:
        return None
    due = raw.get("due")
    return PairLink(
        remctl_id=str(remctl_id),
        reminders_list=str(reminders_list),
        things_list=str(things_list),
        title=str(raw.get("title") or ""),
        notes=str(raw.get("notes") or ""),
        due=str(due) if due else None,
    )


def _pending(raw: object) -> PendingCreate | None:
    if not isinstance(raw, dict):
        return None
    remctl_id = raw.get("remctl_id")
    reminders_list = raw.get("reminders_list")
    things_list = raw.get("things_list")
    not_before = raw.get("not_before")
    if not remctl_id or not reminders_list or not things_list or not not_before:
        return None
    due = raw.get("due")
    return PendingCreate(
        remctl_id=str(remctl_id),
        reminders_list=str(reminders_list),
        things_list=str(things_list),
        title=str(raw.get("title") or ""),
        notes=str(raw.get("notes") or ""),
        due=str(due) if due else None,
        not_before=str(not_before),
    )
