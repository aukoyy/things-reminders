#!/usr/bin/env python3
"""Sync Things 3 with Reminders. Dry-run by default; pass --apply to write."""

from __future__ import annotations

import argparse
import logging
import sys

from config import REMINDERS_LIST, remctl_bin, things_auth_token
from differ import SyncPlan, build_plan
from logging_setup import log_path, setup_logging
from pair_sync import sync_pairs
from remctl_client import UNSET, RemctlClient, RemctlError
from state import State
from things_reader import read_things
from things_url import complete_todo

log = logging.getLogger("things_reminders")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Sync incomplete Things to-dos into the Reminders list 'Things', "
            "and two-way sync the shared lists."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write to Reminders and Things. Without this flag the run is a dry-run.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log intended changes without writing (default).",
    )
    return parser.parse_args(argv)


def apply_plan(plan: SyncPlan, client: RemctlClient, state: State, dry_run: bool) -> None:
    if dry_run:
        for line in plan.summaries:
            log.info("dry-run %s", line)
        log.info(
            "dry-run summary: %d creates, %d updates, %d reminder completions, "
            "%d Things completions, %d mapping drops, %d unchanged",
            len(plan.creates),
            len(plan.updates),
            len(plan.complete_reminders),
            len(plan.complete_things),
            len(plan.drop_mapping),
            plan.unchanged,
        )
        return

    mapped_ids = set(state.mapping.values())
    for pending in plan.creates:
        try:
            remctl_id, payload = client.add(
                title=pending.title,
                notes=pending.notes,
                due_date=pending.due_date,
                tags=pending.tags,
                list_name=REMINDERS_LIST,
                mapped_ids=mapped_ids,
            )
        except RemctlError as exc:
            log.error("create failed for %r: %s", pending.title, exc)
            continue
        state.mapping[pending.things_uuid] = remctl_id
        mapped_ids.add(remctl_id)
        state.save()
        if payload.get("status") == "partial":
            log.warning(
                "created partial %r → %s (%s); next run will finish metadata",
                pending.title,
                remctl_id,
                payload.get("failed") or payload.get("error") or "private step failed",
            )
        else:
            log.info("created %r → %s", pending.title, remctl_id)

    for pending in plan.updates:
        changes = pending.changes
        try:
            client.edit(
                pending.remctl_id,
                title=changes.get("title"),
                notes=changes.get("notes"),
                due=changes["due"] if "due" in changes else UNSET,
                tags=changes.get("tags"),
            )
        except RemctlError as exc:
            log.error("update failed for %r: %s", pending.title, exc)
            continue
        log.info("updated %r [%s]", pending.title, ", ".join(sorted(changes)))

    for pending in plan.complete_reminders:
        try:
            client.done(pending.remctl_id)
        except RemctlError as exc:
            log.error("Reminders complete failed for %r: %s", pending.title, exc)
            continue
        state.mapping.pop(pending.things_uuid, None)
        log.info("completed in Reminders %r (%s)", pending.title, pending.things_uuid)

    for things_uuid in plan.drop_mapping:
        state.mapping.pop(things_uuid, None)

    auth = things_auth_token() if plan.complete_things else None
    if plan.complete_things and not auth:
        log.error(
            "Need THINGS_AUTH_TOKEN (env or Keychain) or Things URL auth enabled "
            "to complete %d to-do(s) in Things.",
            len(plan.complete_things),
        )
    elif auth:
        for things_uuid, _remctl_id, title in plan.complete_things:
            try:
                complete_todo(things_uuid, auth)
                state.mapping.pop(things_uuid, None)
                log.info("completed in Things %r (%s)", title, things_uuid)
            except Exception as exc:
                log.error("failed to complete Things to-do %s: %s", things_uuid, exc)

    state.save()
    log.info(
        "applied: %d creates, %d updates, %d reminder completions, "
        "%d Things completions, %d unchanged",
        len(plan.creates),
        len(plan.updates),
        len(plan.complete_reminders),
        len(plan.complete_things),
        plan.unchanged,
    )


def run(apply: bool) -> int:
    setup_logging()
    dry_run = not apply
    log.info("start (%s) log=%s", "apply" if apply else "dry-run", log_path())

    snapshot = read_things()
    desired = snapshot.todos

    try:
        client = RemctlClient()
    except SystemExit as exc:
        if dry_run:
            log.warning("%s", exc)
            for task in desired:
                log.info(
                    "dry-run would sync %r tags=%s due=%s",
                    task.title,
                    list(task.tags),
                    task.due_date,
                )
            return 0
        raise

    log.info("remctl=%s list=%r", remctl_bin(), REMINDERS_LIST)
    state = State.load()
    one_way_failed = False
    try:
        items = client.pull(REMINDERS_LIST, state.mapping, create_list=apply)
    except RemctlError as exc:
        if dry_run:
            log.warning(
                "remctl pull failed: %s. Planning against an empty Reminders snapshot.",
                exc,
            )
            items = {}
        else:
            log.error("%s", exc)
            one_way_failed = True
            items = None

    if items is not None:
        plan = build_plan(desired, state.mapping, items)
        if not plan.summaries:
            log.info("noop: %d tasks already in sync", plan.unchanged)
            state.save()
        else:
            apply_plan(plan, client, state, dry_run=dry_run)

    sync_pairs(client, state, dry_run=dry_run)
    return 1 if one_way_failed else 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    apply = bool(args.apply) and not args.dry_run
    try:
        return run(apply)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
