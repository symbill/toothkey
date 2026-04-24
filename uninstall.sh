#!/bin/bash
# uninstall.sh — undo everything install.sh did.
#
# Specifically:
#   1. Stops + disables the toothkey-tray.service user unit.
#   2. Removes ~/.config/systemd/user/toothkey-tray.service.
#   3. Stops + disables the toothkey-worker.service system unit.
#   4. Removes /etc/systemd/system/toothkey-worker.service.
#   5. Removes /etc/sudoers.d/toothkey.
#   6. Runs `systemctl daemon-reload` so systemd forgets the units.
#
# We intentionally do NOT touch:
#   - /run/toothkey/       (ephemeral, gone on reboot anyway)
#   - logs/                (the user probably still wants their logs)
#   - /etc/bluetooth/       (main.conf + drop-ins from start.sh's
#                           `ensure_bluez_config_current` — those are
#                           needed for start.sh to work. If the user
#                           wants a clean BlueZ they can run
#                           ./start.sh --reset-bluez.)

set -euo pipefail

if [ "${TOOTHKEY_UNINSTALL_EXECD:-}" != "1" ]; then
    export TOOTHKEY_UNINSTALL_EXECD=1
    exec -a toothkey-uninstall /bin/bash "$0" "$@"
fi

# Mirror install.sh's privilege-escalation dance so we can still
# locate the invoking user after re-exec under sudo.
if [ "$(id -u)" -ne 0 ]; then
    TOOTHKEY_UNINSTALL_INVOKER="$USER"
    TOOTHKEY_UNINSTALL_INVOKER_UID=$(id -u)
    TOOTHKEY_UNINSTALL_INVOKER_GID=$(id -g)
    TOOTHKEY_UNINSTALL_INVOKER_HOME="$HOME"
    export TOOTHKEY_UNINSTALL_INVOKER TOOTHKEY_UNINSTALL_INVOKER_UID \
           TOOTHKEY_UNINSTALL_INVOKER_GID TOOTHKEY_UNINSTALL_INVOKER_HOME
    echo "Elevating to root (you may be prompted for your password)..."
    exec sudo -E bash "$0" "$@"
fi

REAL_USER="${TOOTHKEY_UNINSTALL_INVOKER:-${SUDO_USER:-}}"
if [ -z "$REAL_USER" ] || [ "$REAL_USER" = "root" ]; then
    echo "ERROR: could not determine the end user. Don't run uninstall.sh"
    echo "       directly as root."
    exit 1
fi
REAL_UID="${TOOTHKEY_UNINSTALL_INVOKER_UID:-$(id -u "$REAL_USER")}"
REAL_HOME="${TOOTHKEY_UNINSTALL_INVOKER_HOME:-$(getent passwd "$REAL_USER" | cut -d: -f6)}"

echo "Uninstalling Toothkey autostart for user: $REAL_USER"
echo

# ---------------------------------------------------------------------------
# 1 + 2. Tear down the per-user tray service.
# ---------------------------------------------------------------------------
USER_RUNTIME_DIR="/run/user/$REAL_UID"
USER_SERVICE_PATH="$REAL_HOME/.config/systemd/user/toothkey-tray.service"

if [ -f "$USER_SERVICE_PATH" ]; then
    if [ -d "$USER_RUNTIME_DIR" ]; then
        # Best-effort: failures here are non-fatal (user bus may have
        # already shut down, service may already be stopped, etc.).
        sudo -u "$REAL_USER" XDG_RUNTIME_DIR="$USER_RUNTIME_DIR" \
            systemctl --user stop toothkey-tray.service 2>/dev/null || true
        sudo -u "$REAL_USER" XDG_RUNTIME_DIR="$USER_RUNTIME_DIR" \
            systemctl --user disable toothkey-tray.service 2>/dev/null || true
        sudo -u "$REAL_USER" XDG_RUNTIME_DIR="$USER_RUNTIME_DIR" \
            systemctl --user daemon-reload 2>/dev/null || true
    fi
    rm -f "$USER_SERVICE_PATH"
    echo "  removed $USER_SERVICE_PATH"
else
    echo "  (no $USER_SERVICE_PATH to remove)"
fi

# ---------------------------------------------------------------------------
# 3 + 4. Tear down the system worker service.
# ---------------------------------------------------------------------------
SERVICE_PATH=/etc/systemd/system/toothkey-worker.service
if [ -f "$SERVICE_PATH" ]; then
    systemctl stop toothkey-worker.service 2>/dev/null || true
    systemctl disable toothkey-worker.service 2>/dev/null || true
    rm -f "$SERVICE_PATH"
    echo "  removed $SERVICE_PATH"
else
    echo "  (no $SERVICE_PATH to remove)"
fi

# ---------------------------------------------------------------------------
# 5. Remove sudoers drop-in.
# ---------------------------------------------------------------------------
SUDOERS_PATH=/etc/sudoers.d/toothkey
if [ -f "$SUDOERS_PATH" ]; then
    rm -f "$SUDOERS_PATH"
    echo "  removed $SUDOERS_PATH"
else
    echo "  (no $SUDOERS_PATH to remove)"
fi

# ---------------------------------------------------------------------------
# 6. Daemon reload so systemd forgets the units.
# ---------------------------------------------------------------------------
systemctl daemon-reload

# Clean up the (ephemeral) runtime dir if present and empty. /run/
# is tmpfs so it'll be gone on reboot anyway — this just reclaims it
# immediately for the non-reboot case.
if [ -d /run/toothkey ]; then
    rm -f /run/toothkey/ipc.sock
    rmdir /run/toothkey 2>/dev/null || true
fi

echo
echo "Uninstall complete."
echo "  - logs/ kept; delete manually if you don't want them."
echo "  - ./start.sh still works as a manual launcher."
