#!/bin/bash
# Install the LaunchAgent that runs sync every 5 minutes while the Mac is awake.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.aukoyy.things-reminders"
DEST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="${HOME}/Library/Logs/things-reminders"
PYTHON="${ROOT}/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
  echo "Create the venv first:" >&2
  echo "  uv sync" >&2
  exit 1
fi

mkdir -p "$LOG_DIR" "${HOME}/Library/LaunchAgents"
chmod +x "${ROOT}/scripts/run-sync.sh"

sed -e "s|__REPO__|${ROOT}|g" -e "s|__HOME__|${HOME}|g" \
  "${ROOT}/launchd/${LABEL}.plist" > "$DEST"

UID_NUM="$(id -u)"
if launchctl bootout "gui/${UID_NUM}" "$DEST" 2>/dev/null; then
  :
elif launchctl unload "$DEST" 2>/dev/null; then
  :
fi

if launchctl bootstrap "gui/${UID_NUM}" "$DEST" 2>/dev/null; then
  :
else
  launchctl load "$DEST"
fi

echo "Loaded ${DEST}"
echo "Runs every 5 minutes while this Mac is awake (StartInterval 300)."
echo
echo "Grant Full Disk Access to these binaries so the job can read Things and Reminders:"
echo "  ${PYTHON}"
echo "  /bin/bash"
echo "System Settings → Privacy & Security → Full Disk Access"
echo
echo "If remctl asks for Reminders access, allow it for that same Python."
echo
echo "Logs: ${LOG_DIR}/sync.log"
echo "Dry-run first: ${PYTHON} ${ROOT}/sync.py"
