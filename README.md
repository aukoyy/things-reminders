# Things → Reminders sync

## Before you use this

Most of this repo was written by an AI coding agent. I have not audited it as a product. If you clone it, you are responsible for reading the code, the tokens you give it, and anything it writes to Reminders or Things. No warranty.

## What it does

One-way sync from [Things 3](https://culturedcode.com/things/) (macOS) into an Apple Reminders list named **Things**, via [remctl](https://github.com/viticci/remctl). Completing a reminder completes the matching Things to-do.

Things wins on title, notes, dates, and tags. Reminders wins only on completion. The Things database is never written; completions go through `things:///`. That URL scheme brings Things 3 to the front, so if you complete a reminder, Things might open on the next sync.

Every incomplete to-do becomes a reminder in the Things list. Organization is tags, not extra lists:

| Things                      | Reminders tag       |
| --------------------------- | ------------------- |
| Inbox / Someday             | `Inbox` / `Someday` |
| A project                   | that project's name |
| An area (no project)        | that area's name    |
| Anytime, no project or area | `Anytime`           |

Things When → reminder due date (all-day). Deadlines, headings, and checklists go in the notes.

macOS only. Needs [uv](https://docs.astral.sh/uv/) (`brew install uv`), a working remctl install, and Full Disk Access for `.venv/bin/python` (and `/bin/bash` if you use launchd) so the job can read the Things DB and so remctl can read Reminders.

## Setup

```bash
git clone https://github.com/aukoyy/things-reminders.git
cd things-reminders
uv sync
```

1. Install [remctl](https://github.com/viticci/remctl) if you have not already. `remctl lists --json` should work from the same binary that will run the sync. If remctl is not on `PATH` or at `~/bin/remctl`, set `REMCTL_BIN`.

2. In Things → Settings → General, enable **Things URLs**. The auth token is usually read from the Things DB; if reverse completion fails, set `THINGS_AUTH_TOKEN` in a gitignored `.env` or Keychain:

   ```
   THINGS_AUTH_TOKEN=your-token-here
   ```

   ```bash
   security add-generic-password -s things-reminders -a things-auth-token -w
   ```

3. System Settings → Privacy & Security → Full Disk Access: add `.venv/bin/python` (absolute path) and `/bin/bash`.

4. Dry-run, then apply:

   ```bash
   uv run python sync.py
   uv run python sync.py --apply
   ```

The first apply creates the Reminders list named Things if it is missing. Synced tags use remctl `--private`.

Logs: `~/Library/Logs/things-reminders/sync.log`. Mapping: `~/Library/Application Support/things-reminders/mapping.json`.

From the repo root, this installs the LaunchAgent that syncs every 5 minutes while the Mac is awake:

```bash
scripts/install-launchd.sh
```

Unload: `launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.aukoyy.things-reminders.plist`

Inspired by [stancl/things3-trello-sync](https://github.com/stancl/things3-trello-sync).
