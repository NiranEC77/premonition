#!/usr/bin/env bash
# Starts IB Gateway under IBC, read-only, against the paper trading account.
#
# Credentials are never passed as process arguments — `ps`/`pgrep -af` show
# full command lines to any local shell user on this box, so --user/--pw
# flags would leak the password in plaintext to anyone who runs `ps aux`
# while the gateway is up. Instead, IBC reads IbLoginId/IbPassword from a
# generated ini file that exists only under a 700 directory, mode 600,
# regenerated fresh from /etc/premonition/env on every start, and never
# committed to git (ibkr/config.ini, the tracked template, always has both
# fields blank).
#
# Xvfb is started manually (not via xvfb-run) so the keyboard layout can be
# forced to US before IBC types the password into the login form — IBC uses
# synthetic key events (java.awt.Robot) to fill the login dialog, and if the
# X server's default layout doesn't match, shifted characters (like the `!`
# and `?` in this password) can silently fail to type correctly, submitting
# a corrupted password even though the stored credential is right.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IBC_PATH="/home/claude-orch/.local/opt/ibc"
# IBC expects the stock installer layout <tws-path>/ibgateway/<version>/jars.
# We installed flat, so ibgw/ibgateway/1045 is a symlink back to the real install.
GATEWAY_PATH="/home/claude-orch/.local/opt/ibgw"
GATEWAY_SETTINGS_PATH="/home/claude-orch/.local/state/ibgateway"
RUNTIME_DIR="/home/claude-orch/.local/state/ibkr-runtime"
RUNTIME_INI="${RUNTIME_DIR}/config.ini"
TWS_VERSION=1045
LOG_DIR="/srv/premonition/logs"
LOG_FILE="${LOG_DIR}/ibgateway-$(date +%Y-%m-%d).log"
XVFB_DISPLAY=":50"

set -a
source /etc/premonition/env
set +a

if [[ -z "${IBKR_LOGIN_ID:-}" || -z "${IBKR_PASSWORD:-}" ]]; then
    echo "FATAL: IBKR_LOGIN_ID / IBKR_PASSWORD not set in /etc/premonition/env — refusing to start." >&2
    exit 1
fi

mkdir -p "$GATEWAY_SETTINGS_PATH"
mkdir -p -m 700 "$RUNTIME_DIR"

umask 077
awk -v uid="$IBKR_LOGIN_ID" -v pw="$IBKR_PASSWORD" '
  /^IbLoginId=/ { print "IbLoginId=" uid; next }
  /^IbPassword=/ { print "IbPassword=" pw; next }
  { print }
' "${REPO_DIR}/ibkr/config.ini" > "$RUNTIME_INI"
chmod 600 "$RUNTIME_INI"

pkill -9 -f "Xvfb ${XVFB_DISPLAY} " 2>/dev/null || true

Xvfb "$XVFB_DISPLAY" -screen 0 1024x768x24 -nolisten tcp -ac &
XVFB_PID=$!
trap 'kill -9 "$XVFB_PID" 2>/dev/null || true' EXIT

for _ in $(seq 1 20); do
    DISPLAY="$XVFB_DISPLAY" xdpyinfo >/dev/null 2>&1 && break
    sleep 0.25
done

DISPLAY="$XVFB_DISPLAY" setxkbmap us >> "$LOG_FILE" 2>&1

DISPLAY="$XVFB_DISPLAY" "${IBC_PATH}/scripts/ibcstart.sh" "$TWS_VERSION" \
    --gateway \
    --tws-path="$GATEWAY_PATH" \
    --tws-settings-path="$GATEWAY_SETTINGS_PATH" \
    --ibc-path="$IBC_PATH" \
    --ibc-ini="$RUNTIME_INI" \
    --mode=paper \
    >> "$LOG_FILE" 2>&1
