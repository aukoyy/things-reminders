"""Planner tests for the shared-list sync. No live Things or Reminders calls."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from config import things_list_for_todo
from pair_sync import (
    Content,
    CreateTodo,
    Link,
    PairPlan,
    Pending,
    Reminder,
    Todo,
    _things_operations,
    build_pair_plan,
    classify_things,
    is_blocked,
)
from state import PairLink, PendingCreate, State

HANDLE = "🛒 Handleliste"
AUKNERS = "🏡 Aukners"
VASE = "💐 Full Vase"


def content(title: str, notes: str = "", due: str | None = None) -> Content:
    return Content(title, notes, due)


def todo(
    uuid: str,
    things_list: str,
    title: str,
    notes: str = "",
    due: str | None = None,
    blocked: bool = False,
    created: str | None = None,
) -> Todo:
    return Todo(uuid, things_list, content(title, notes, due), blocked, created)


def reminder(
    remctl_id: str,
    reminders_list: str,
    title: str,
    notes: str = "",
    due: str | None = None,
    completed: bool = False,
    in_list: bool = True,
    is_subtask: bool = False,
) -> Reminder:
    return Reminder(
        remctl_id,
        reminders_list,
        content(title, notes, due),
        completed,
        in_list,
        is_subtask,
    )


def link(
    things_uuid: str,
    remctl_id: str,
    things_list: str,
    reminders_list: str,
    title: str,
    notes: str = "",
    due: str | None = None,
) -> Link:
    return Link(things_uuid, remctl_id, reminders_list, things_list, content(title, notes, due))


def plan(**kwargs):
    defaults = dict(
        todos=[],
        reminders=[],
        links=[],
        pending=[],
        missing={},
        missing_containers=set(),
        ambiguous=set(),
    )
    defaults.update(kwargs)
    return build_pair_plan(**defaults)


class PlanTests(unittest.TestCase):
    def test_things_change_updates_reminder(self) -> None:
        result = plan(
            todos=[todo("t1", HANDLE, "Milk", notes="skim")],
            reminders=[reminder("9", "Handleliste", "Milk")],
            links=[link("t1", "9", HANDLE, "Handleliste", "Milk")],
        )
        self.assertEqual(len(result.update_reminders), 1)
        self.assertEqual(result.update_reminders[0].content.notes, "skim")
        self.assertEqual(result.update_things, [])

    def test_reminder_change_updates_things(self) -> None:
        result = plan(
            todos=[todo("t1", HANDLE, "Milk")],
            reminders=[reminder("9", "Handleliste", "Oat milk", due="2026-10-01")],
            links=[link("t1", "9", HANDLE, "Handleliste", "Milk")],
        )
        self.assertEqual(len(result.update_things), 1)
        self.assertEqual(result.update_things[0].content.title, "Oat milk")
        self.assertEqual(result.update_things[0].content.due, "2026-10-01")
        self.assertEqual(result.update_reminders, [])

    def test_both_sides_changed_things_wins(self) -> None:
        result = plan(
            todos=[todo("t1", HANDLE, "From Things")],
            reminders=[reminder("9", "Handleliste", "From Reminders")],
            links=[link("t1", "9", HANDLE, "Handleliste", "Original")],
        )
        self.assertEqual(len(result.update_reminders), 1)
        self.assertEqual(result.update_reminders[0].content.title, "From Things")
        self.assertEqual(result.update_things, [])

    def test_second_pass_is_unchanged(self) -> None:
        synced = plan(
            todos=[todo("t1", HANDLE, "Milk", notes="skim")],
            reminders=[reminder("9", "Handleliste", "Milk", notes="skim")],
            links=[link("t1", "9", HANDLE, "Handleliste", "Milk", notes="skim")],
        )
        self.assertEqual(synced.writes, 0)
        self.assertEqual(synced.unchanged, 1)

    def test_unmapped_items_are_created_on_both_sides(self) -> None:
        result = plan(
            todos=[todo("t1", HANDLE, "Milk")],
            reminders=[reminder("9", "Handleliste", "Milk")],
        )
        self.assertEqual(len(result.create_reminders), 1)
        self.assertEqual(result.create_reminders[0].things_uuid, "t1")
        self.assertEqual(len(result.create_things), 1)
        self.assertEqual(result.create_things[0].remctl_id, "9")

    def test_past_when_date_is_copied_as_is(self) -> None:
        result = plan(
            todos=[todo("t1", AUKNERS, "Call", due="2020-01-02")],
            reminders=[reminder("3", "Aukners Todo", "Call")],
            links=[link("t1", "3", AUKNERS, "Aukners Todo", "Call")],
        )
        self.assertEqual(result.update_reminders[0].content.due, "2020-01-02")

    def test_ma_tag_is_not_created_and_deletes_a_synced_reminder(self) -> None:
        blocked = plan(todos=[todo("t1", HANDLE, "Secret", blocked=True)])
        self.assertEqual(blocked.create_reminders, [])

        tagged = plan(
            todos=[todo("t1", HANDLE, "Secret", blocked=True)],
            reminders=[reminder("9", "Handleliste", "Secret")],
            links=[link("t1", "9", HANDLE, "Handleliste", "Secret")],
        )
        self.assertEqual(len(tagged.delete_reminders), 1)
        self.assertEqual(tagged.delete_reminders[0].reason, "ma")
        self.assertEqual(tagged.create_reminders, [])

    def test_ma_tag_is_case_insensitive(self) -> None:
        self.assertTrue(is_blocked(["MA"], "ma"))
        self.assertFalse(is_blocked(["maybe"], "ma"))
        self.assertFalse(is_blocked(["ma"], None))

    def test_completion_crosses_both_ways(self) -> None:
        things_done = plan(
            reminders=[reminder("9", "Handleliste", "Milk")],
            links=[link("t1", "9", HANDLE, "Handleliste", "Milk")],
            missing={"t1": "completed"},
        )
        self.assertEqual(len(things_done.complete_reminders), 1)
        self.assertEqual(things_done.delete_reminders, [])

        reminder_done = plan(
            todos=[todo("t1", HANDLE, "Milk")],
            reminders=[reminder("9", "Handleliste", "Milk", completed=True)],
            links=[link("t1", "9", HANDLE, "Handleliste", "Milk")],
        )
        self.assertEqual(len(reminder_done.complete_things), 1)

    def test_removal_cancels_things_and_deletes_reminder(self) -> None:
        reminder_gone = plan(
            todos=[todo("t1", HANDLE, "Milk")],
            links=[link("t1", "9", HANDLE, "Handleliste", "Milk")],
        )
        self.assertEqual(len(reminder_gone.cancel_things), 1)
        self.assertEqual(reminder_gone.create_reminders, [])

        things_gone = plan(
            reminders=[reminder("9", "Handleliste", "Milk")],
            links=[link("t1", "9", HANDLE, "Handleliste", "Milk")],
            missing={"t1": "removed"},
        )
        self.assertEqual(len(things_gone.delete_reminders), 1)
        self.assertEqual(things_gone.delete_reminders[0].reason, "removed")
        self.assertEqual(things_gone.create_things, [])

    def test_move_between_paired_lists_recreates_the_reminder(self) -> None:
        result = plan(
            todos=[todo("t1", AUKNERS, "Keys")],
            reminders=[reminder("9", "Handleliste", "Keys")],
            links=[link("t1", "9", HANDLE, "Handleliste", "Keys")],
        )
        self.assertEqual(len(result.delete_reminders), 1)
        self.assertEqual(result.create_reminders[0].reminders_list, "Aukners Todo")

    def test_subtasks_are_not_copied_into_things(self) -> None:
        result = plan(reminders=[reminder("9", "Handleliste", "Child", is_subtask=True)])
        self.assertEqual(result.create_things, [])

    def test_new_project_items_are_todo_objects(self) -> None:
        planned = PairPlan(
            create_projects=[VASE],
            create_things=[
                CreateTodo("9", "Ø Full Vase", VASE, content("Milk", notes="oat"), "Milk")
            ],
        )
        operations = _things_operations(planned, {})
        item = operations[0]["attributes"]["items"][0]
        self.assertEqual(item["type"], "to-do")
        self.assertEqual(item["attributes"]["title"], "Milk")
        self.assertEqual(item["attributes"]["notes"], "oat")
        self.assertNotIn("type", item["attributes"])

    def test_missing_things_list_creates_a_project(self) -> None:
        result = plan(missing_containers={VASE})
        self.assertEqual(result.create_projects, [VASE])

    def test_pending_adopts_only_a_todo_created_after_the_attempt(self) -> None:
        waiting = Pending("9", "Handleliste", HANDLE, content("Milk"), "2026-09-26 12:00:00")
        old = plan(
            todos=[todo("old", HANDLE, "Milk", created="2020-01-01 00:00:00")],
            reminders=[reminder("9", "Handleliste", "Milk")],
            pending=[waiting],
        )
        self.assertEqual(old.adopts, [])
        self.assertEqual(len(old.create_things), 1)

        fresh = plan(
            todos=[
                todo("old", HANDLE, "Milk", created="2020-01-01 00:00:00"),
                todo("new", HANDLE, "Milk", created="2026-09-26 12:00:05"),
            ],
            reminders=[reminder("9", "Handleliste", "Milk")],
            pending=[waiting],
        )
        self.assertEqual(len(fresh.adopts), 1)
        self.assertEqual(fresh.adopts[0].things_uuid, "new")
        self.assertEqual(fresh.create_things, [])
        self.assertEqual([item.things_uuid for item in fresh.create_reminders], ["old"])

    def test_one_way_exclusion_is_the_paired_container_only(self) -> None:
        self.assertEqual(things_list_for_todo(HANDLE, "Other"), HANDLE)
        self.assertEqual(things_list_for_todo("", HANDLE), HANDLE)
        self.assertIsNone(things_list_for_todo("Nested", HANDLE))
        self.assertIsNone(things_list_for_todo("Inbox", ""))


class ClassifyTests(unittest.TestCase):
    def test_project_area_and_heading_membership(self) -> None:
        projects = [
            {"uuid": "p-shop", "title": HANDLE, "status": "incomplete", "type": "project"},
            {"uuid": "p-nested", "title": "Nested", "status": "incomplete", "area": "a-home"},
        ]
        areas = [{"uuid": "a-home", "title": AUKNERS}]
        headings = [{"uuid": "h1", "project": "p-shop"}]
        tasks = [
            {"uuid": "in-project", "type": "to-do", "status": "incomplete", "title": "Milk", "project": "p-shop"},
            {
                "uuid": "under-heading",
                "type": "to-do",
                "status": "incomplete",
                "title": "Eggs",
                "heading": "h1",
            },
            {
                "uuid": "in-area",
                "type": "to-do",
                "status": "incomplete",
                "title": "Keys",
                "area": "a-home",
                "tags": ["ma"],
            },
            {
                "uuid": "nested",
                "type": "to-do",
                "status": "incomplete",
                "title": "Paint",
                "project": "p-nested",
            },
            {
                "uuid": "blocked",
                "type": "to-do",
                "status": "incomplete",
                "title": "Mine",
                "project": "p-shop",
                "tags": ["MA"],
                "start_date": "2020-05-01",
            },
        ]
        side = classify_things(tasks, projects, areas, headings, {}, {})
        by_id = {item.uuid: item for item in side.todos}
        self.assertEqual(set(by_id), {"in-project", "under-heading", "in-area", "blocked"})
        self.assertEqual(by_id["under-heading"].things_list, HANDLE)
        self.assertEqual(by_id["in-area"].things_list, AUKNERS)
        self.assertFalse(by_id["in-area"].blocked)
        self.assertTrue(by_id["blocked"].blocked)
        self.assertEqual(by_id["blocked"].content.due, "2020-05-01")
        self.assertIn(VASE, side.missing_containers)

    def test_duplicate_names_are_ambiguous_and_not_treated_as_removed(self) -> None:
        projects = [
            {"uuid": "p1", "title": HANDLE, "status": "incomplete"},
            {"uuid": "p2", "title": HANDLE, "status": "incomplete"},
        ]
        side = classify_things([], projects, [], [], {"t1": HANDLE}, {"t1": None})
        self.assertIn(HANDLE, side.ambiguous)
        self.assertNotIn("t1", side.missing)

    def test_completed_tracked_todo_is_not_a_removal(self) -> None:
        areas = [{"uuid": "a1", "title": VASE}]
        fetched = {"t1": {"uuid": "t1", "type": "to-do", "status": "completed", "title": "Done"}}
        side = classify_things([], [], areas, [], {"t1": VASE}, fetched)
        self.assertEqual(side.missing["t1"], "completed")


class StateTests(unittest.TestCase):
    def test_round_trip_keeps_one_way_mapping_and_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mapping.json"
            state = State.load(path)
            state.mapping["one-way"] = "4"
            state.pairs["t1"] = PairLink(
                remctl_id="9",
                reminders_list="Handleliste",
                things_list=HANDLE,
                title="Milk",
                notes="",
                due=None,
            )
            state.pending.append(
                PendingCreate(
                    remctl_id="8",
                    reminders_list="Handleliste",
                    things_list=HANDLE,
                    title="Bread",
                    notes="sourdough",
                    due="2026-10-02",
                    not_before="2026-09-26 12:00:00",
                )
            )
            state.save(path)
            loaded = State.load(path)
        self.assertEqual(loaded.mapping, {"one-way": "4"})
        self.assertEqual(loaded.pairs["t1"].remctl_id, "9")
        self.assertEqual(loaded.pairs["t1"].due, None)
        self.assertEqual(loaded.pending[0].title, "Bread")
        self.assertEqual(loaded.pending[0].due, "2026-10-02")


if __name__ == "__main__":
    unittest.main()
