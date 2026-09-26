"""Two-way sync for the hardcoded Reminders ↔ Things lists.

Things wins when both sides changed since the last snapshot. A removal on
either side removes the other: Reminders deletes, Things cancels (the URL
scheme has no delete). Completion still completes the other side.

Items already on both sides are not matched by title. Each unmapped open
item is created on the other side.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from config import LIST_PAIRS, ListPair, pair_for_reminders_list, things_auth_token
from remctl_client import UNSET, RemctlClient, RemctlError, snapshot_item
from state import PairLink, PendingCreate, State
from things_url import apply_json

log = logging.getLogger("things_reminders")

_POLL_SECONDS = 15.0
_POLL_INTERVAL = 0.4
_CREATED_SKEW = timedelta(seconds=120)


@dataclass(frozen=True)
class Content:
    title: str
    notes: str
    due: str | None


@dataclass(frozen=True)
class Container:
    uuid: str
    kind: str
    title: str


@dataclass(frozen=True)
class Todo:
    uuid: str
    things_list: str
    content: Content
    blocked: bool
    created: str | None = None


@dataclass(frozen=True)
class Reminder:
    remctl_id: str
    reminders_list: str
    content: Content
    completed: bool
    in_list: bool
    is_subtask: bool = False


@dataclass(frozen=True)
class Link:
    things_uuid: str
    remctl_id: str
    reminders_list: str
    things_list: str
    content: Content


@dataclass(frozen=True)
class Pending:
    remctl_id: str
    reminders_list: str
    things_list: str
    content: Content
    not_before: str


@dataclass(frozen=True)
class ThingsSide:
    todos: list[Todo]
    missing: dict[str, str]
    containers: dict[str, Container]
    missing_containers: set[str]
    ambiguous: set[str]


@dataclass
class CreateReminder:
    things_uuid: str
    reminders_list: str
    things_list: str
    content: Content
    title: str


@dataclass
class CreateTodo:
    remctl_id: str
    reminders_list: str
    things_list: str
    content: Content
    title: str


@dataclass
class UpdateReminder:
    things_uuid: str
    remctl_id: str
    reminders_list: str
    things_list: str
    content: Content
    fields: tuple[str, ...]
    title: str


@dataclass
class UpdateTodo:
    things_uuid: str
    remctl_id: str
    content: Content
    fields: tuple[str, ...]
    title: str


@dataclass
class CompleteReminder:
    things_uuid: str
    remctl_id: str
    title: str


@dataclass
class CompleteTodo:
    things_uuid: str
    remctl_id: str
    title: str


@dataclass
class DeleteReminder:
    things_uuid: str
    remctl_id: str
    title: str
    reason: str


@dataclass
class CancelTodo:
    things_uuid: str
    remctl_id: str
    title: str


@dataclass
class Refresh:
    things_uuid: str
    content: Content


@dataclass
class Adopt:
    things_uuid: str
    remctl_id: str
    reminders_list: str
    things_list: str
    content: Content
    title: str


@dataclass
class PairPlan:
    create_reminders: list[CreateReminder] = field(default_factory=list)
    create_things: list[CreateTodo] = field(default_factory=list)
    update_reminders: list[UpdateReminder] = field(default_factory=list)
    update_things: list[UpdateTodo] = field(default_factory=list)
    complete_reminders: list[CompleteReminder] = field(default_factory=list)
    complete_things: list[CompleteTodo] = field(default_factory=list)
    delete_reminders: list[DeleteReminder] = field(default_factory=list)
    cancel_things: list[CancelTodo] = field(default_factory=list)
    refreshes: list[Refresh] = field(default_factory=list)
    adopts: list[Adopt] = field(default_factory=list)
    unlinks: list[str] = field(default_factory=list)
    drop_pending: list[str] = field(default_factory=list)
    hold_pending: list[str] = field(default_factory=list)
    create_projects: list[str] = field(default_factory=list)
    unchanged: int = 0
    summaries: list[str] = field(default_factory=list)

    @property
    def writes(self) -> int:
        return (
            len(self.create_reminders)
            + len(self.create_things)
            + len(self.update_reminders)
            + len(self.update_things)
            + len(self.complete_reminders)
            + len(self.complete_things)
            + len(self.delete_reminders)
            + len(self.cancel_things)
            + len(self.create_projects)
            + len(self.adopts)
            + len(self.unlinks)
            + len(self.refreshes)
            + len(self.drop_pending)
        )


def content_from_task(task: dict) -> Content:
    title = (task.get("title") or "").strip() or "(untitled)"
    notes = (task.get("notes") or "").strip()
    return Content(title=title, notes=notes, due=_date(task.get("start_date")))


def content_from_reminder(item: dict) -> Content:
    snap = snapshot_item(item)
    title = (snap["title"] or "").strip() or "(untitled)"
    notes = (snap["notes"] or "").strip()
    return Content(title=title, notes=notes, due=snap["due_date"])


def is_blocked(tags: object, block_tag: str | None) -> bool:
    if not block_tag or not isinstance(tags, list):
        return False
    wanted = block_tag.casefold()
    return any(str(tag).casefold() == wanted for tag in tags)


def classify_things(
    tasks: list[dict],
    projects: list[dict],
    areas: list[dict],
    headings: list[dict],
    tracked: dict[str, str],
    fetched: dict[str, dict | None],
) -> ThingsSide:
    """Index open paired to-dos and the fate of linked to-dos that are gone."""
    containers, missing_containers, ambiguous = _containers(projects, areas)
    heading_by_id = {row["uuid"]: row for row in headings if row.get("uuid")}
    todos: list[Todo] = []
    open_ids: set[str] = set()
    seen: set[str] = set()
    for task in tasks:
        if task.get("type") != "to-do" or task.get("status") not in (None, "incomplete"):
            continue
        uuid = task.get("uuid")
        if not uuid or uuid in seen:
            continue
        seen.add(uuid)
        things_list = _membership(task, heading_by_id, containers)
        if not things_list:
            continue
        pair = _pair_by_things(things_list)
        todos.append(
            Todo(
                uuid=uuid,
                things_list=things_list,
                content=content_from_task(task),
                blocked=is_blocked(task.get("tags"), pair.block_tag if pair else None),
                created=_created_stamp(task.get("created")),
            )
        )
        open_ids.add(uuid)

    missing: dict[str, str] = {}
    for uuid, things_list in tracked.items():
        if things_list in ambiguous or uuid in open_ids:
            continue
        missing[uuid] = _fate(fetched.get(uuid))
    return ThingsSide(
        todos=todos,
        missing=missing,
        containers=containers,
        missing_containers=missing_containers,
        ambiguous=ambiguous,
    )


def build_pair_plan(
    *,
    todos: list[Todo],
    reminders: list[Reminder],
    links: list[Link],
    pending: list[Pending],
    missing: dict[str, str],
    missing_containers: set[str],
    ambiguous: set[str],
) -> PairPlan:
    plan = PairPlan()
    for title in sorted(ambiguous):
        plan.summaries.append(
            f"skip {title!r}: more than one Things project or area uses this name"
        )

    open_todos = {todo.uuid: todo for todo in todos}
    reminders_by_id = {item.remctl_id: item for item in reminders}
    links_by_uuid = {link.things_uuid: link for link in links}
    claimed_remctl = {link.remctl_id for link in links}
    surviving: set[str] = set()

    for link in links:
        if link.things_list in ambiguous:
            surviving.add(link.things_uuid)
            continue
        todo = open_todos.get(link.things_uuid)
        if todo is not None and todo.things_list != link.things_list:
            todo = None
        reminder = reminders_by_id.get(link.remctl_id)
        if todo is not None and todo.blocked:
            _queue_blocked(plan, link, reminder)
            continue
        if todo is not None:
            _queue_open_link(plan, link, todo, reminder, surviving)
            continue
        _queue_closed_link(plan, link, reminder, missing.get(link.things_uuid, "removed"))

    reserved = _queue_pending(
        plan,
        pending,
        todos,
        {item.remctl_id for item in reminders if item.in_list and not item.completed},
        surviving,
    )

    for todo in todos:
        if todo.things_list in ambiguous:
            continue
        if todo.uuid in surviving or todo.uuid in reserved or todo.blocked:
            continue
        if todo.uuid in links_by_uuid and todo.uuid not in {item.things_uuid for item in plan.delete_reminders}:
            # Still linked, and this pass is not moving it off that link.
            if links_by_uuid[todo.uuid].things_list == todo.things_list:
                continue
        pair = _pair_by_things(todo.things_list)
        if pair is None:
            continue
        plan.create_reminders.append(
            CreateReminder(
                things_uuid=todo.uuid,
                reminders_list=pair.reminders_list,
                things_list=todo.things_list,
                content=todo.content,
                title=todo.content.title,
            )
        )
        plan.summaries.append(
            f"create-reminder {todo.content.title!r} → {pair.reminders_list!r}"
        )

    pending_ids = {item.remctl_id for item in pending}
    for reminder in reminders:
        if reminder.remctl_id in claimed_remctl or reminder.remctl_id in pending_ids:
            continue
        if not reminder.in_list or reminder.completed or reminder.is_subtask:
            continue
        pair = pair_for_reminders_list(reminder.reminders_list)
        if pair is None or pair.things_list in ambiguous:
            continue
        plan.create_things.append(
            CreateTodo(
                remctl_id=reminder.remctl_id,
                reminders_list=pair.reminders_list,
                things_list=pair.things_list,
                content=reminder.content,
                title=reminder.content.title,
            )
        )
        plan.summaries.append(
            f"create-things {reminder.content.title!r} → {pair.things_list!r}"
        )

    for title in sorted(missing_containers):
        if title in ambiguous:
            continue
        plan.create_projects.append(title)
        plan.summaries.append(f"create-project {title!r}")

    return plan


def sync_pairs(client: RemctlClient, state: State, *, dry_run: bool) -> None:
    links = _links_from_state(state)
    try:
        side = read_things_side({link.things_uuid: link.things_list for link in links})
    except SystemExit:
        raise
    except Exception as exc:
        log.error("paired Things read failed: %s", exc)
        return

    try:
        reminders = _load_reminders(client, links)
    except RemctlError as exc:
        log.error("paired Reminders read failed: %s", exc)
        return

    plan = build_pair_plan(
        todos=side.todos,
        reminders=reminders,
        links=links,
        pending=_pending_from_state(state),
        missing=side.missing,
        missing_containers=side.missing_containers,
        ambiguous=side.ambiguous,
    )
    if plan.writes == 0:
        for line in plan.summaries:
            log.info("pair %s", line)
        log.info("paired lists noop: %d already in sync", plan.unchanged)
        return
    apply_pair_plan(plan, side, client, state, dry_run=dry_run)


def read_things_side(tracked: dict[str, str]) -> ThingsSide:
    try:
        import things
    except ImportError as exc:
        raise SystemExit("things.py is not installed. Run: uv sync") from exc

    try:
        tasks = things.tasks(type="to-do") or []
        headings = things.tasks(type="heading", status=None) or []
        projects = things.projects(status=None) or []
        areas = things.areas() or []
    except Exception as exc:
        log.error(
            "Could not read the Things database: %s. "
            "Grant Full Disk Access to this Python binary in "
            "System Settings → Privacy & Security → Full Disk Access.",
            exc,
        )
        raise SystemExit(1) from exc

    open_ids = {task.get("uuid") for task in tasks}
    fetched: dict[str, dict | None] = {}
    for uuid in tracked:
        if uuid in open_ids:
            continue
        try:
            fetched[uuid] = things.get(uuid)
        except ValueError:
            fetched[uuid] = None
    return classify_things(tasks, projects, areas, headings, tracked, fetched)


def apply_pair_plan(
    plan: PairPlan,
    side: ThingsSide,
    client: RemctlClient,
    state: State,
    *,
    dry_run: bool,
) -> None:
    if dry_run:
        for line in plan.summaries:
            log.info("dry-run %s", line)
        log.info(
            "dry-run pairs: %d reminder creates, %d Things creates, %d reminder updates, "
            "%d Things updates, %d reminder completions, %d Things completions, "
            "%d reminder deletes, %d Things cancels, %d project creates, %d unchanged",
            len(plan.create_reminders),
            len(plan.create_things),
            len(plan.update_reminders),
            len(plan.update_things),
            len(plan.complete_reminders),
            len(plan.complete_things),
            len(plan.delete_reminders),
            len(plan.cancel_things),
            len(plan.create_projects),
            plan.unchanged,
        )
        return

    for refresh in plan.refreshes:
        _store_content(state, refresh.things_uuid, refresh.content)
    state.save()

    for update in plan.update_reminders:
        try:
            client.edit(
                update.remctl_id,
                title=update.content.title if "title" in update.fields else None,
                notes=update.content.notes if "notes" in update.fields else None,
                due=update.content.due if "due" in update.fields else UNSET,
            )
        except RemctlError as exc:
            log.error("pair update failed for %r: %s", update.title, exc)
            continue
        _store_content(state, update.things_uuid, update.content)
        state.save()
        log.info("pair updated reminder %r [%s]", update.title, ", ".join(update.fields))

    for complete in plan.complete_reminders:
        try:
            client.done(complete.remctl_id)
        except RemctlError as exc:
            log.error("pair complete reminder failed for %r: %s", complete.title, exc)
            continue
        state.pairs.pop(complete.things_uuid, None)
        state.save()
        log.info("pair completed reminder %r", complete.title)

    blocked_creates: set[str] = set()
    for delete in plan.delete_reminders:
        try:
            client.delete(delete.remctl_id)
        except RemctlError as exc:
            log.error("pair delete reminder failed for %r: %s", delete.title, exc)
            blocked_creates.add(delete.things_uuid)
            continue
        state.pairs.pop(delete.things_uuid, None)
        state.save()
        log.info("pair deleted reminder %r (%s)", delete.title, delete.reason)

    for things_uuid in plan.unlinks:
        if things_uuid in state.pairs:
            state.pairs.pop(things_uuid, None)
            log.info("pair unlinked %s", things_uuid)
    if plan.unlinks:
        state.save()

    things_ok = _apply_things(plan, side, state)
    if not things_ok:
        log.error("Things writes for paired lists did not run")

    mapped_ids = set(state.mapping.values()) | {link.remctl_id for link in state.pairs.values()}
    for create in plan.create_reminders:
        if create.things_uuid in blocked_creates:
            continue
        try:
            remctl_id, _payload = client.add(
                title=create.content.title,
                notes=create.content.notes,
                due_date=create.content.due,
                tags=(),
                list_name=create.reminders_list,
                mapped_ids=mapped_ids,
            )
        except RemctlError as exc:
            log.error("pair create reminder failed for %r: %s", create.title, exc)
            continue
        mapped_ids.add(remctl_id)
        state.pairs[create.things_uuid] = _link(create.things_uuid, remctl_id, create.reminders_list, create.things_list, create.content)
        state.save()
        log.info("pair created reminder %r → %s", create.title, remctl_id)

    for adopt in plan.adopts:
        state.pairs[adopt.things_uuid] = _link(
            adopt.things_uuid,
            adopt.remctl_id,
            adopt.reminders_list,
            adopt.things_list,
            adopt.content,
        )
        state.pending = [item for item in state.pending if item.remctl_id != adopt.remctl_id]
        log.info("pair linked existing Things to-do %r (%s)", adopt.title, adopt.things_uuid)
    if plan.adopts:
        state.save()

    dropped_pending = set(plan.drop_pending)
    if dropped_pending:
        state.pending = [item for item in state.pending if item.remctl_id not in dropped_pending]
        state.save()


def _apply_things(plan: PairPlan, side: ThingsSide, state: State) -> bool:
    needs_things = bool(
        plan.create_things or plan.update_things or plan.cancel_things or plan.complete_things or plan.create_projects
    )
    if not needs_things:
        return True
    auth = things_auth_token()
    if not auth:
        log.error(
            "Need THINGS_AUTH_TOKEN (env or Keychain) or Things URL auth enabled "
            "to write %d paired to-do change(s) in Things.",
            len(plan.create_things)
            + len(plan.update_things)
            + len(plan.cancel_things)
            + len(plan.complete_things)
            + len(plan.create_projects),
        )
        return False

    containers = dict(side.containers)
    before = _snapshot_ids(containers, plan)
    not_before = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    operations = _things_operations(plan, containers)
    if not operations:
        return True
    try:
        apply_json(operations, auth)
    except Exception as exc:
        log.error("things:///json failed: %s", exc)
        return False

    for update in plan.update_things:
        _store_content(state, update.things_uuid, update.content)
        log.info("pair updated Things %r [%s]", update.title, ", ".join(update.fields))
    for complete in plan.complete_things:
        state.pairs.pop(complete.things_uuid, None)
        log.info("pair completed Things %r", complete.title)
    for cancel in plan.cancel_things:
        state.pairs.pop(cancel.things_uuid, None)
        log.info("pair canceled Things %r", cancel.title)

    found = _poll_creates(containers, plan.create_things, before, not_before)
    found_ids = set(found)
    for create in plan.create_things:
        uuid = found.get(create.remctl_id)
        if not uuid:
            continue
        state.pairs[uuid] = _link(uuid, create.remctl_id, create.reminders_list, create.things_list, create.content)
        log.info("pair created Things %r (%s)", create.title, uuid)
    state.pending = [item for item in state.pending if item.remctl_id not in found_ids and item.remctl_id not in {c.remctl_id for c in plan.create_things}]
    for create in plan.create_things:
        if create.remctl_id in found_ids:
            continue
        state.pending.append(
            PendingCreate(
                remctl_id=create.remctl_id,
                reminders_list=create.reminders_list,
                things_list=create.things_list,
                title=create.content.title,
                notes=create.content.notes,
                due=create.content.due,
                not_before=not_before,
            )
        )
        log.error(
            "Things did not show a new to-do for %r; will retry next run",
            create.title,
        )
    state.save()
    return True


def _things_operations(plan: PairPlan, containers: dict[str, Container]) -> list[dict]:
    grouped: dict[str, list[CreateTodo]] = defaultdict(list)
    for create in plan.create_things:
        grouped[create.things_list].append(create)
    operations: list[dict] = []
    for title in plan.create_projects:
        items = grouped.pop(title, [])
        attributes: dict = {"title": title, "when": "anytime"}
        if items:
            attributes["items"] = [
                {"type": "to-do", "attributes": _todo_attributes(item.content)}
                for item in items
            ]
        operations.append({"type": "project", "attributes": attributes})
    for things_list, items in grouped.items():
        container = containers.get(things_list)
        if container is None:
            log.error("No Things list %r to receive %d to-do(s)", things_list, len(items))
            continue
        for item in items:
            operations.append(
                {
                    "type": "to-do",
                    "attributes": _todo_attributes(item.content, list_id=container.uuid),
                }
            )
    for update in plan.update_things:
        operations.append(
            {
                "type": "to-do",
                "operation": "update",
                "id": update.things_uuid,
                "attributes": _changed_attributes(update.content, update.fields),
            }
        )
    for cancel in plan.cancel_things:
        operations.append(
            {
                "type": "to-do",
                "operation": "update",
                "id": cancel.things_uuid,
                "attributes": {"canceled": True},
            }
        )
    for complete in plan.complete_things:
        operations.append(
            {
                "type": "to-do",
                "operation": "update",
                "id": complete.things_uuid,
                "attributes": {"completed": True},
            }
        )
    return operations


def _changed_attributes(content: Content, fields: tuple[str, ...]) -> dict:
    attributes: dict = {}
    if "title" in fields:
        attributes["title"] = content.title
    if "notes" in fields:
        attributes["notes"] = content.notes
    if "due" in fields:
        attributes["when"] = content.due or "anytime"
    return attributes


def _todo_attributes(content: Content, list_id: str | None = None) -> dict:
    attributes: dict = {
        "title": content.title,
        "when": content.due or "anytime",
    }
    if content.notes:
        attributes["notes"] = content.notes
    if list_id:
        attributes["list-id"] = list_id
    return attributes


def _snapshot_ids(containers: dict[str, Container], plan: PairPlan) -> dict[str, set[str]]:
    titles = {create.things_list for create in plan.create_things} | set(plan.create_projects)
    found: dict[str, set[str]] = {}
    for title in titles:
        container = containers.get(title)
        found[title] = _container_ids(container) if container else set()
    return found


def _poll_creates(
    containers: dict[str, Container],
    creates: list[CreateTodo],
    before: dict[str, set[str]],
    not_before: str,
) -> dict[str, str]:
    if not creates:
        return {}
    remaining = list(creates)
    found: dict[str, str] = {}
    deadline = time.monotonic() + _POLL_SECONDS
    while remaining:
        newly = _match_new_todos(containers, remaining, before, not_before)
        for remctl_id, uuid in newly.items():
            found[remctl_id] = uuid
        remaining = [item for item in remaining if item.remctl_id not in found]
        if not remaining or time.monotonic() >= deadline:
            break
        time.sleep(_POLL_INTERVAL)
    return found


def _match_new_todos(
    containers: dict[str, Container],
    creates: list[CreateTodo],
    before: dict[str, set[str]],
    not_before: str,
) -> dict[str, str]:
    by_list: dict[str, list[CreateTodo]] = defaultdict(list)
    for create in creates:
        by_list[create.things_list].append(create)
    matched: dict[str, str] = {}
    for things_list, group in by_list.items():
        container = containers.get(things_list) or _reload_container(things_list)
        if container is None:
            continue
        containers[things_list] = container
        seen = before.get(things_list, set())
        fresh = [
            todo
            for todo in _container_todos(container)
            if todo.uuid not in seen and _created_after(todo.created, not_before)
        ]
        matched.update(_pair_by_content(group, fresh))
    return matched


def _pair_by_content(creates: list[CreateTodo], todos: list[Todo]) -> dict[str, str]:
    grouped_creates: dict[Content, list[CreateTodo]] = defaultdict(list)
    for create in creates:
        grouped_creates[create.content].append(create)
    grouped_todos: dict[Content, list[Todo]] = defaultdict(list)
    for todo in todos:
        grouped_todos[todo.content].append(todo)
    matched: dict[str, str] = {}
    for content, wanted in grouped_creates.items():
        available = grouped_todos.get(content, [])
        if len(available) != len(wanted):
            continue
        available = sorted(available, key=lambda todo: todo.created or "")
        for create, todo in zip(wanted, available, strict=True):
            matched[create.remctl_id] = todo.uuid
    return matched


def _queue_pending(
    plan: PairPlan,
    pending: list[Pending],
    todos: list[Todo],
    open_reminder_ids: set[str],
    surviving: set[str],
) -> set[str]:
    """Link to-dos created on a previous run. Return Things uuids reserved by that."""
    reserved: set[str] = set()
    groups: dict[tuple[str, Content], list[Pending]] = defaultdict(list)
    for item in pending:
        if item.remctl_id not in open_reminder_ids:
            plan.drop_pending.append(item.remctl_id)
            plan.summaries.append(f"drop-pending {item.content.title!r}")
            continue
        groups[(item.things_list, item.content)].append(item)

    for (things_list, content), group in groups.items():
        earliest = min(item.not_before for item in group)
        candidates = [
            todo
            for todo in todos
            if todo.things_list == things_list
            and todo.content == content
            and not todo.blocked
            and todo.uuid not in surviving
            and _created_after(todo.created, earliest)
        ]
        if len(candidates) == len(group):
            ordered = sorted(candidates, key=lambda todo: todo.created or "")
            waiting = sorted(group, key=lambda item: item.not_before)
            for item, todo in zip(waiting, ordered, strict=True):
                plan.adopts.append(
                    Adopt(
                        things_uuid=todo.uuid,
                        remctl_id=item.remctl_id,
                        reminders_list=item.reminders_list,
                        things_list=item.things_list,
                        content=item.content,
                        title=item.content.title,
                    )
                )
                reserved.add(todo.uuid)
                plan.summaries.append(f"adopt {item.content.title!r} ({todo.uuid})")
            continue
        if not candidates:
            pair = _pair_by_things(things_list)
            if pair is None:
                continue
            for item in group:
                plan.create_things.append(
                    CreateTodo(
                        remctl_id=item.remctl_id,
                        reminders_list=item.reminders_list,
                        things_list=item.things_list,
                        content=item.content,
                        title=item.content.title,
                    )
                )
                plan.summaries.append(f"retry-things {item.content.title!r}")
            continue
        for item in group:
            plan.hold_pending.append(item.remctl_id)
            plan.summaries.append(f"hold {item.content.title!r}: several new Things to-dos match")
        for todo in candidates:
            reserved.add(todo.uuid)
    return reserved


def _queue_blocked(plan: PairPlan, link: Link, reminder: Reminder | None) -> None:
    if reminder is not None and reminder.in_list and not reminder.completed:
        plan.delete_reminders.append(
            DeleteReminder(link.things_uuid, link.remctl_id, link.content.title, "ma")
        )
        plan.summaries.append(f"delete-reminder {link.content.title!r} (ma)")
        return
    plan.unlinks.append(link.things_uuid)
    plan.summaries.append(f"unlink {link.content.title!r} (ma)")


def _queue_open_link(
    plan: PairPlan,
    link: Link,
    todo: Todo,
    reminder: Reminder | None,
    surviving: set[str],
) -> None:
    if reminder is None or not reminder.in_list:
        if reminder is not None and reminder.completed:
            plan.complete_things.append(CompleteTodo(link.things_uuid, link.remctl_id, todo.content.title))
            plan.summaries.append(f"complete-things {todo.content.title!r}")
            return
        plan.cancel_things.append(CancelTodo(link.things_uuid, link.remctl_id, todo.content.title))
        plan.summaries.append(f"cancel-things {todo.content.title!r}")
        return
    if reminder.completed:
        plan.complete_things.append(CompleteTodo(link.things_uuid, link.remctl_id, todo.content.title))
        plan.summaries.append(f"complete-things {todo.content.title!r}")
        return

    things_changed = link.content != todo.content
    reminder_changed = link.content != reminder.content
    if not things_changed and not reminder_changed:
        plan.unchanged += 1
        surviving.add(link.things_uuid)
        return
    if things_changed:
        if reminder.content != todo.content:
            fields = _diff(reminder.content, todo.content)
            plan.update_reminders.append(
                UpdateReminder(
                    things_uuid=link.things_uuid,
                    remctl_id=link.remctl_id,
                    reminders_list=link.reminders_list,
                    things_list=link.things_list,
                    content=todo.content,
                    fields=fields,
                    title=todo.content.title,
                )
            )
            plan.summaries.append(f"update-reminder {todo.content.title!r} [{', '.join(fields)}]")
        else:
            plan.refreshes.append(Refresh(link.things_uuid, todo.content))
            plan.summaries.append(f"refresh {todo.content.title!r}")
        surviving.add(link.things_uuid)
        return
    if todo.content != reminder.content:
        fields = _diff(todo.content, reminder.content)
        plan.update_things.append(
            UpdateTodo(
                things_uuid=link.things_uuid,
                remctl_id=link.remctl_id,
                content=reminder.content,
                fields=fields,
                title=reminder.content.title,
            )
        )
        plan.summaries.append(f"update-things {reminder.content.title!r} [{', '.join(fields)}]")
    else:
        plan.refreshes.append(Refresh(link.things_uuid, reminder.content))
        plan.summaries.append(f"refresh {reminder.content.title!r}")
    surviving.add(link.things_uuid)


def _queue_closed_link(plan: PairPlan, link: Link, reminder: Reminder | None, fate: str) -> None:
    reminder_open = reminder is not None and reminder.in_list and not reminder.completed
    title = link.content.title
    if fate == "completed":
        if reminder_open:
            plan.complete_reminders.append(CompleteReminder(link.things_uuid, link.remctl_id, title))
            plan.summaries.append(f"complete-reminder {title!r}")
            return
        plan.unlinks.append(link.things_uuid)
        plan.summaries.append(f"unlink {title!r} (completed)")
        return
    if reminder_open:
        plan.delete_reminders.append(DeleteReminder(link.things_uuid, link.remctl_id, title, "removed"))
        plan.summaries.append(f"delete-reminder {title!r} (removed from Things)")
        return
    plan.unlinks.append(link.things_uuid)
    plan.summaries.append(f"unlink {title!r} (removed)")


def _containers(
    projects: list[dict],
    areas: list[dict],
) -> tuple[dict[str, Container], set[str], set[str]]:
    containers: dict[str, Container] = {}
    missing: set[str] = set()
    ambiguous: set[str] = set()
    for pair in LIST_PAIRS:
        projects_hit = [
            row
            for row in projects
            if (row.get("title") or "").strip() == pair.things_list
            and row.get("status") == "incomplete"
            and row.get("uuid")
        ]
        areas_hit = [
            row
            for row in areas
            if (row.get("title") or "").strip() == pair.things_list and row.get("uuid")
        ]
        if len(projects_hit) > 1 or len(areas_hit) > 1 or (projects_hit and areas_hit):
            ambiguous.add(pair.things_list)
            continue
        if projects_hit:
            containers[pair.things_list] = Container(projects_hit[0]["uuid"], "project", pair.things_list)
        elif areas_hit:
            containers[pair.things_list] = Container(areas_hit[0]["uuid"], "area", pair.things_list)
        else:
            missing.add(pair.things_list)
    return containers, missing, ambiguous


def _membership(
    task: dict,
    headings: dict[str, dict],
    containers: dict[str, Container],
) -> str | None:
    project_uuid = task.get("project")
    if not project_uuid and task.get("heading"):
        project_uuid = (headings.get(task["heading"]) or {}).get("project")
    area_uuid = None if project_uuid else task.get("area")
    for title, container in containers.items():
        if container.kind == "project" and project_uuid == container.uuid:
            return title
        if container.kind == "area" and not project_uuid and area_uuid == container.uuid:
            return title
    return None


def _fate(task: dict | None) -> str:
    if not task or task.get("trashed") or task.get("type") not in (None, "to-do"):
        return "removed"
    if task.get("status") == "completed":
        return "completed"
    return "removed"


def _diff(current: Content, target: Content) -> tuple[str, ...]:
    fields: list[str] = []
    if current.title != target.title:
        fields.append("title")
    if current.notes != target.notes:
        fields.append("notes")
    if current.due != target.due:
        fields.append("due")
    return tuple(fields)


def _date(value: object) -> str | None:
    if not value:
        return None
    day = str(value)[:10]
    if len(day) == 10 and day[4] == "-" and day[7] == "-":
        return day
    return None


def _created_stamp(value: object) -> str | None:
    if not value:
        return None
    text = str(value).strip().replace("T", " ")
    return text[:19]


def _created_after(created: str | None, not_before: str) -> bool:
    stamp = _created_stamp(created)
    threshold = _created_stamp(not_before)
    if not stamp or not threshold:
        return False
    try:
        start = datetime.strptime(threshold, "%Y-%m-%d %H:%M:%S") - _CREATED_SKEW
    except ValueError:
        return False
    return stamp >= start.strftime("%Y-%m-%d %H:%M:%S")


def _pair_by_things(title: str) -> ListPair | None:
    for pair in LIST_PAIRS:
        if pair.things_list == title:
            return pair
    return None


def _links_from_state(state: State) -> list[Link]:
    links: list[Link] = []
    for things_uuid, item in state.pairs.items():
        links.append(
            Link(
                things_uuid=things_uuid,
                remctl_id=item.remctl_id,
                reminders_list=item.reminders_list,
                things_list=item.things_list,
                content=Content(item.title, item.notes, item.due),
            )
        )
    return links


def _pending_from_state(state: State) -> list[Pending]:
    return [
        Pending(
            remctl_id=item.remctl_id,
            reminders_list=item.reminders_list,
            things_list=item.things_list,
            content=Content(item.title, item.notes, item.due),
            not_before=item.not_before,
        )
        for item in state.pending
    ]


def _load_reminders(client: RemctlClient, links: list[Link]) -> list[Reminder]:
    found: list[Reminder] = []
    for pair in LIST_PAIRS:
        listed = client.find_list(pair.reminders_list)
        if listed is None:
            raise RemctlError(f"Reminders list {pair.reminders_list!r} does not exist")
        shown_ids: set[str] = set()
        for item in client.show_list(pair.reminders_list):
            item_id = item.get("id")
            if item_id is None:
                continue
            remctl_id = str(item_id)
            shown_ids.add(remctl_id)
            found.append(_reminder(remctl_id, pair.reminders_list, item, in_list=True))
        for link in links:
            if link.reminders_list != pair.reminders_list or link.remctl_id in shown_ids:
                continue
            detail = client.info(link.remctl_id)
            if not detail or detail.get("id") is None:
                continue
            found.append(
                _reminder(
                    str(detail["id"]),
                    pair.reminders_list,
                    detail,
                    in_list=_same_list(detail.get("list"), pair.reminders_list),
                )
            )
    return found


def _reminder(remctl_id: str, reminders_list: str, item: dict, *, in_list: bool) -> Reminder:
    snap = snapshot_item(item)
    return Reminder(
        remctl_id=remctl_id,
        reminders_list=reminders_list,
        content=content_from_reminder(item),
        completed=bool(snap["completed"]),
        in_list=in_list,
        is_subtask=bool(item.get("isSubtask")),
    )


def _same_list(name: object, expected: str) -> bool:
    if name is None:
        return False
    text = str(name)
    return text == expected or text.casefold() == expected.casefold()


def _store_content(state: State, things_uuid: str, content: Content) -> None:
    link = state.pairs.get(things_uuid)
    if link is None:
        return
    state.pairs[things_uuid] = PairLink(
        remctl_id=link.remctl_id,
        reminders_list=link.reminders_list,
        things_list=link.things_list,
        title=content.title,
        notes=content.notes,
        due=content.due,
    )


def _link(things_uuid: str, remctl_id: str, reminders_list: str, things_list: str, content: Content) -> PairLink:
    del things_uuid
    return PairLink(
        remctl_id=remctl_id,
        reminders_list=reminders_list,
        things_list=things_list,
        title=content.title,
        notes=content.notes,
        due=content.due,
    )


def _container_ids(container: Container) -> set[str]:
    return {todo.uuid for todo in _container_todos(container)}


def _container_todos(container: Container) -> list[Todo]:
    import things

    if container.kind == "project":
        rows = things.tasks(type="to-do", project=container.uuid) or []
    else:
        rows = things.tasks(type="to-do", area=container.uuid, project=False) or []
    todos: list[Todo] = []
    seen: set[str] = set()
    for row in rows:
        uuid = row.get("uuid")
        if not uuid or uuid in seen:
            continue
        seen.add(uuid)
        todos.append(
            Todo(
                uuid=uuid,
                things_list=container.title,
                content=content_from_task(row),
                blocked=False,
                created=_created_stamp(row.get("created")),
            )
        )
    return todos


def _reload_container(title: str) -> Container | None:
    import things

    projects = [
        row
        for row in (things.projects() or [])
        if (row.get("title") or "").strip() == title and row.get("status") == "incomplete" and row.get("uuid")
    ]
    if len(projects) == 1:
        return Container(projects[0]["uuid"], "project", title)
    if projects:
        return None
    areas = [
        row
        for row in (things.areas() or [])
        if (row.get("title") or "").strip() == title and row.get("uuid")
    ]
    if len(areas) == 1:
        return Container(areas[0]["uuid"], "area", title)
    return None
