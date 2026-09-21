"""Read incomplete Things to-dos and map them onto Reminders tags."""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("things_reminders")


@dataclass(frozen=True)
class DesiredTask:
    things_uuid: str
    title: str
    description: str
    list_label: str
    tags: tuple[str, ...]
    due_date: str | None


@dataclass(frozen=True)
class ThingsSnapshot:
    todos: list[DesiredTask]
    active_containers: frozenset[str]
    retired_containers: frozenset[str]


def list_label(start: str | None) -> str:
    """Exclusive Things-list label. Today is a due date, not a label."""
    if start == "Inbox":
        return "Inbox"
    if start == "Someday":
        return "Someday"
    return "Anytime"


def build_description(task: dict) -> str:
    parts: list[str] = []
    notes = (task.get("notes") or "").strip()
    if notes:
        parts.append(notes)

    extras: list[str] = []
    heading = (task.get("heading_title") or "").strip()
    if heading:
        extras.append(f"Heading: {heading}")
    deadline = task.get("deadline")
    if deadline:
        extras.append(f"Deadline: {deadline}")
    checklist = task.get("checklist") or []
    if checklist:
        extras.append("Checklist:")
        for item in checklist:
            mark = "x" if item.get("status") == "completed" else " "
            extras.append(f"- [{mark}] {item.get('title') or ''}")
    if extras:
        parts.append("\n".join(extras))
    return "\n\n".join(parts)


def _index_by_uuid(rows: list[dict]) -> dict[str, dict]:
    return {row["uuid"]: row for row in rows if row.get("uuid")}


def _resolve_project_area(
    task: dict,
    headings: dict[str, dict],
    projects: dict[str, dict],
) -> tuple[str, str]:
    """things.py omits project_title when the to-do sits under a heading."""
    project_title = (task.get("project_title") or "").strip()
    area_title = (task.get("area_title") or "").strip()
    project_uuid = task.get("project")

    if not project_uuid and task.get("heading"):
        heading = headings.get(task["heading"]) or {}
        project_uuid = heading.get("project")
        if not project_title:
            project_title = (heading.get("project_title") or "").strip()

    if project_uuid:
        project = projects.get(project_uuid) or {}
        if not project_title:
            project_title = (project.get("title") or "").strip()
        if not area_title:
            area_title = (project.get("area_title") or "").strip()

    return project_title, area_title


def _tags_for(list_name: str, project_title: str, area_title: str) -> tuple[str, ...]:
    """Project > Area > Anytime. Inbox and Someday always apply when present."""
    names: list[str] = []
    if list_name in ("Inbox", "Someday"):
        names.append(list_name)
    if project_title:
        names.append(project_title)
    elif area_title:
        names.append(area_title)
    elif list_name == "Anytime":
        names.append(list_name)
    seen: set[str] = set()
    unique: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            unique.append(name)
    return tuple(unique)


def _as_due(start_date: str | None) -> str | None:
    if not start_date:
        return None
    return str(start_date)[:10]


def _container_titles(rows: list[dict], statuses: set[str] | None = None) -> set[str]:
    titles: set[str] = set()
    for row in rows:
        title = (row.get("title") or "").strip()
        if not title:
            continue
        if statuses is not None and row.get("status") not in statuses:
            continue
        titles.add(title)
    return titles


def read_things() -> ThingsSnapshot:
    try:
        import things
    except ImportError as exc:
        raise SystemExit(
            "things.py is not installed. Run: uv sync"
        ) from exc

    try:
        raw = things.tasks(type="to-do", include_items=True)
        headings = _index_by_uuid(things.tasks(type="heading", status=None))
        all_projects = things.projects(status=None)
        projects = _index_by_uuid(all_projects)
        areas = things.areas()
    except Exception as exc:
        log.error(
            "Could not read the Things database: %s. "
            "Grant Full Disk Access to this Python binary in "
            "System Settings → Privacy & Security → Full Disk Access.",
            exc,
        )
        raise SystemExit(1) from exc

    active_projects = _container_titles(all_projects, {"incomplete"})
    retired_projects = _container_titles(all_projects, {"completed", "canceled"})
    active_areas = _container_titles(areas)
    active_containers = frozenset(active_projects | active_areas)
    retired_containers = frozenset(retired_projects - active_containers)

    desired: list[DesiredTask] = []
    for task in raw:
        if task.get("type") != "to-do":
            continue
        uuid = task["uuid"]
        start = task.get("start")
        list_name = list_label(start)
        title = (task.get("title") or "").strip() or "(untitled)"
        project_title, area_title = _resolve_project_area(task, headings, projects)
        if project_title not in active_projects:
            project_title = ""
        if area_title not in active_areas:
            area_title = ""
        desired.append(
            DesiredTask(
                things_uuid=uuid,
                title=title,
                description=build_description(task),
                list_label=list_name,
                tags=_tags_for(list_name, project_title, area_title),
                due_date=_as_due(task.get("start_date")),
            )
        )
    log.info(
        "Read %d open Things to-dos. Active containers=%d retired=%d",
        len(desired),
        len(active_containers),
        len(retired_containers),
    )
    return ThingsSnapshot(
        todos=desired,
        active_containers=active_containers,
        retired_containers=retired_containers,
    )
