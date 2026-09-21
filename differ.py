"""Idempotent diff: Things desired state vs mapped Reminders items."""

from __future__ import annotations

from dataclasses import dataclass, field

from remctl_client import snapshot_item
from things_reader import DesiredTask


@dataclass
class PendingCreate:
    things_uuid: str
    title: str
    notes: str
    due_date: str | None
    tags: tuple[str, ...]


@dataclass
class PendingUpdate:
    things_uuid: str
    remctl_id: str
    title: str
    changes: dict


@dataclass
class PendingComplete:
    things_uuid: str
    remctl_id: str
    title: str


@dataclass
class SyncPlan:
    creates: list[PendingCreate] = field(default_factory=list)
    updates: list[PendingUpdate] = field(default_factory=list)
    complete_reminders: list[PendingComplete] = field(default_factory=list)
    complete_things: list[tuple[str, str, str]] = field(default_factory=list)
    drop_mapping: list[str] = field(default_factory=list)
    unchanged: int = 0
    summaries: list[str] = field(default_factory=list)

    @property
    def writes(self) -> int:
        return (
            len(self.creates)
            + len(self.updates)
            + len(self.complete_reminders)
            + len(self.complete_things)
        )


def _tags_equal(desired: tuple[str, ...], actual: list[str] | None) -> bool:
    return set(desired) == set(actual or [])


def _item_complete(item: dict | None) -> bool:
    if item is None:
        return True
    return bool(snapshot_item(item)["completed"])


def build_plan(
    desired: list[DesiredTask],
    mapping: dict[str, str],
    items: dict[str, dict],
) -> SyncPlan:
    plan = SyncPlan()
    desired_uuids = {task.things_uuid for task in desired}

    for task in desired:
        remctl_id = mapping.get(task.things_uuid)
        item = items.get(remctl_id) if remctl_id else None

        if remctl_id and _item_complete(item):
            title = (item or {}).get("title") or task.title
            plan.complete_things.append((task.things_uuid, remctl_id, title))
            plan.summaries.append(f"complete-things {task.title!r} ({task.things_uuid})")
            continue

        if not remctl_id:
            _queue_create(plan, task)
            continue

        snap = snapshot_item(item or {})
        changes: dict = {}
        if snap["title"] != task.title:
            changes["title"] = task.title
        if snap["notes"] != task.description:
            changes["notes"] = task.description
        if not _tags_equal(task.tags, snap["tags"]):
            changes["tags"] = list(task.tags)
        if snap["due_date"] != task.due_date:
            changes["due"] = task.due_date

        if not changes:
            plan.unchanged += 1
            continue

        plan.updates.append(
            PendingUpdate(
                things_uuid=task.things_uuid,
                remctl_id=remctl_id,
                title=task.title,
                changes=changes,
            )
        )
        bits = ", ".join(sorted(changes))
        plan.summaries.append(f"update {task.title!r} [{bits}]")

    for things_uuid, remctl_id in mapping.items():
        if things_uuid in desired_uuids:
            continue
        item = items.get(remctl_id)
        title = (item or {}).get("title") or things_uuid
        if _item_complete(item):
            plan.drop_mapping.append(things_uuid)
            plan.summaries.append(f"drop-mapping {title!r} ({things_uuid})")
            continue
        plan.complete_reminders.append(
            PendingComplete(things_uuid=things_uuid, remctl_id=remctl_id, title=title)
        )
        plan.summaries.append(f"complete-reminder {title!r} ({things_uuid})")

    return plan


def _queue_create(plan: SyncPlan, task: DesiredTask) -> None:
    plan.creates.append(
        PendingCreate(
            things_uuid=task.things_uuid,
            title=task.title,
            notes=task.description,
            due_date=task.due_date,
            tags=task.tags,
        )
    )
    plan.summaries.append(
        f"create {task.title!r} tags={list(task.tags)} due={task.due_date}"
    )
