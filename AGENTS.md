# Things 3 → Apple Reminders sync

One-way sync from Things 3 (macOS) into a Reminders list named Things, via
[remctl](https://github.com/viticci/remctl). Plus a reverse leg that completes
Things to-dos when they're completed in Reminders.

## Hard constraints

- macOS only. The Things SQLite DB lives under
  ~/Library/Group Containers/ — reading it requires Full Disk Access for
  whatever binary runs the sync (Terminal, the venv Python, or the
  launchd job). If reads fail with a permissions error, this is why.
  Grant it in System Settings → Privacy & Security → Full Disk Access.
- The Things DB is READ-ONLY. Never write to it, never open it read-write,
  never migrate it. All writes to Things go through the things:/// URL
  scheme, which needs the auth token from Things → Settings → General →
  Enable Things URLs.
- Never write the Reminders SQLite database. All Reminders reads and writes
  go through the installed remctl CLI (`--json`). Synced tags require
  remctl `--private` (ReminderKit). Never use `--via-eventkit`.
- Secrets (Things auth token) come from the environment or Keychain. Never
  commit them, never hardcode them in a plist. remctl itself needs no token.
  The process that invokes remctl needs Full Disk Access (and Reminders
  access) so remctl can read the local store and write via EventKit.

## Design

- Read: things.py against the local SQLite DB. Sync every incomplete
  to-do (not projects or headings themselves).
- Write: remctl into one Reminders list named Things (create it if missing).
  Organization is tags, with hierarchy Project > Area > Anytime: a project
  tag if the to-do is in a project, else an area tag, else Anytime. Inbox
  and Someday always apply when the to-do is in those lists. Things When →
  remctl due date (all-day `YYYY-MM-DD`). A When date of today or earlier is
  written as today's date: Things rolls those into Today and has no overdue
  state, and a past date shows up as overdue in Todoist. A future When date
  stays that date. Deadline is appended to the notes. Reminder tags cannot
  contain whitespace; compare and write them with spaces removed so a run
  does not rewrite reminders whose tags already match.
  Unused Reminders tags are left in place (tags are global; remctl has no
  safe tag-delete).
- Reverse: poll remctl for completed or missing mapped reminders, then
  things:///update?id=<uuid>&completed=true. When a Things to-do disappears,
  complete the matching reminder (`remctl done`). Do not delete reminders.
- State: local file mapping Things UUID ↔ remctl numeric reminder ID. The
  differ must be idempotent — running it twice in a row changes nothing.
- Schedule: launchd with StartInterval 300, not cron, so it survives sleep.

## Conflict rule

Things wins on all content (title, notes, dates, tags). Reminders wins only
on completion. Never write content back to Things.

## Non-negotiables for any change

- Every run supports --dry-run, and it's the default until explicitly
  disabled with --apply. This is a script that mutates two live task systems.
- Log what changed on every run, to a file, with timestamps. When it
  silently stops working at 6am you'll need this.

## Run / schedule

```
uv sync

# Optional Things URL token: .env (gitignored), the environment, or Keychain
# (service things-reminders). Falls back to the token in Things' DB.
# THINGS_AUTH_TOKEN=...
# security add-generic-password -s things-reminders -a things-auth-token -w

# remctl must already be installed. `remctl lists --json` should work.
# Override the binary with REMCTL_BIN=... if it is not on PATH or ~/bin/remctl.

uv run python sync.py                 # dry-run (default)
uv run python sync.py --apply         # write

scripts/install-launchd.sh            # every 5 minutes while the Mac is awake
```

Grant Full Disk Access to the venv interpreter
(`.venv/bin/python`) and/or `/bin/bash` (the launchd wrapper) so the job can
read the Things DB. Logs: `~/Library/Logs/things-reminders/sync.log`.

## Prior art

stancl/things3-trello-sync — same idea, Trello target.
