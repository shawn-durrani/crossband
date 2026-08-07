#!/usr/bin/env bash
# Install (or refresh) the launchd user agent that keeps Crossband running on
# macOS: it starts the service at login, restarts it within ~1s if it ever
# exits, and brings it back after a reboot — so a crash or stray signal no
# longer leaves the app dark until someone notices.
#
# Idempotent: safe to re-run after a `git pull`. It takes over cleanly —
# unloading any prior agent and stopping a hand-started instance first — so
# there is only ever ONE owner of the service process (which also ends the
# restart lock race, since two competing starts can no longer happen).
#
# Usage:  ops/install-supervisor.sh
# Undo:   launchctl bootout gui/$(id -u)/dev.crossband.server
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_DIR="$(pwd -P)"
LABEL="dev.crossband.server"
# v0.2 rename: v0.1 installs ran under this label. Booting it out (and
# removing its plist so it cannot re-bootstrap at login) makes this installer
# the migration tool for an existing install; drop once v0.2 has settled.
OLD_LABEL="dev.sideband.server"
TEMPLATE="ops/${LABEL}.plist.template"
DEST="$HOME/Library/LaunchAgents/${LABEL}.plist"
PORT="${CROSSBAND_PORT:-${MMC_PORT:-8902}}"  # MMC_ fallback ends in v0.3
DOMAIN="gui/$(id -u)"

[ -f "$TEMPLATE" ] || { echo "✗ template not found: $TEMPLATE" >&2; exit 1; }

# Bake this machine's real node/npm dir onto the agent's PATH so start.sh's
# frontend build works whether node came from nvm or Homebrew; fall back to a
# clean system PATH when npm isn't found.
AGENT_PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin"
if NPM="$(command -v npm 2>/dev/null)"; then
  AGENT_PATH="$(dirname "$NPM"):$AGENT_PATH"
fi

mkdir -p "$HOME/Library/LaunchAgents"
sed -e "s#{{REPO_DIR}}#${REPO_DIR}#g" \
    -e "s#{{HOME}}#${HOME}#g" \
    -e "s#{{PATH}}#${AGENT_PATH}#g" \
    "$TEMPLATE" > "$DEST"

# The generated plist must be valid and fully substituted before we load it.
plutil -lint "$DEST" >/dev/null
if grep -q '{{' "$DEST"; then
  echo "✗ unsubstituted placeholder left in $DEST" >&2; exit 1
fi

# Take over as sole owner: drop any prior agent, then stop a hand-started
# instance still holding the port, before bootstrapping the agent (RunAtLoad
# starts the one real instance).
launchctl bootout "$DOMAIN/$OLD_LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/${OLD_LABEL}.plist"
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
sleep 1
if PID="$(lsof -tnP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null)"; then
  echo "· stopping hand-started instance (pid $PID) so the agent owns the port"
  kill "$PID" 2>/dev/null || true
  for _ in $(seq 1 15); do kill -0 "$PID" 2>/dev/null || break; sleep 1; done
  kill -9 "$PID" 2>/dev/null || true
fi
launchctl bootstrap "$DOMAIN" "$DEST"
launchctl enable "$DOMAIN/$LABEL"

echo "✓ supervisor installed. Crossband will self-restart and survive reboots."
echo "  status:  launchctl print $DOMAIN/$LABEL | grep -iE 'state|pid|program'"
echo "  logs:    tail -f $REPO_DIR/data/service.log"
echo "  remove:  launchctl bootout $DOMAIN/$LABEL"
