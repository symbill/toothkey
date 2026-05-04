#!/bin/bash
# install.sh — make Toothkey start automatically on every boot + login,
# without ever asking the user for a password.
#
# What it sets up:
#
#   1. /etc/systemd/system/toothkey-worker.service
#      System-level service, starts at boot, runs worker.py as root.
#      Needs root for L2CAP raw sockets. The unit file embeds
#      TOOTHKEY_SOCKET_OWNER=UID:GID so the worker chowns its UDS to
#      the logged-in user; TOOTHKEY_ACCEPT_TIMEOUT=0 so the worker
#      waits indefinitely for a tray instead of churning pre-login.
#
#   2. ~/.config/systemd/user/toothkey-tray.service
#      User-level service, starts when a graphical session begins.
#      Runs tray.py as the invoking user. Auto-restarts on crash.
#
#   3. /etc/sudoers.d/toothkey
#      Lets the user restart / start / stop the toothkey-worker system
#      service without a password, so the tray's Restart menu item
#      stays silent on click.
#
#   4. ~/.local/share/applications/toothkey.desktop + matching SVG icon
#      Application-menu entry so "Tooth-key" shows up in the KDE /
#      GNOME launcher / Activities. Delegates to
#      `start.sh --install-launcher` so the .desktop format has a
#      single source of truth.
#
# The script is idempotent — re-running it overwrites existing files
# with fresh content. Use ./uninstall.sh to revert.
#
# Compatibility:
#   - `./start.sh` still works if the system service isn't active
#     (same semantics as before the install).
#   - If the system service IS active, `./start.sh` detects that and
#     refuses cleanly, pointing the user at systemctl / the tray.
#   - The tray's Restart menu item detects installed mode and uses
#     `sudo -n systemctl restart` instead of re-exec'ing start.sh.

set -euo pipefail

# Re-exec as toothkey-install so `ps aux | grep toothkey` still works.
if [ "${TOOTHKEY_INSTALL_EXECD:-}" != "1" ]; then
    export TOOTHKEY_INSTALL_EXECD=1
    exec -a toothkey-install /bin/bash "$0" "$@"
fi

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# ---------------------------------------------------------------------------
# Privilege escalation. install.sh is invoked by the end user, then
# re-execs itself under sudo -E to write /etc/{systemd,sudoers.d}/...
# We preserve SUDO_USER / original UID via env vars so the privileged
# half still knows whose home directory to drop the user service file
# into.
# ---------------------------------------------------------------------------
if [ "$(id -u)" -ne 0 ]; then
    TOOTHKEY_INSTALL_INVOKER="$USER"
    TOOTHKEY_INSTALL_INVOKER_UID=$(id -u)
    TOOTHKEY_INSTALL_INVOKER_GID=$(id -g)
    TOOTHKEY_INSTALL_INVOKER_HOME="$HOME"
    export TOOTHKEY_INSTALL_INVOKER TOOTHKEY_INSTALL_INVOKER_UID \
           TOOTHKEY_INSTALL_INVOKER_GID TOOTHKEY_INSTALL_INVOKER_HOME
    echo "Elevating to root (you may be prompted for your password)..."
    exec sudo -E bash "$0" "$@"
fi

# At this point we are root.
REAL_USER="${TOOTHKEY_INSTALL_INVOKER:-${SUDO_USER:-}}"
if [ -z "$REAL_USER" ] || [ "$REAL_USER" = "root" ]; then
    echo "ERROR: could not determine the end user. Don't run install.sh"
    echo "       directly as root — run it as yourself (it'll sudo for you)."
    exit 1
fi

REAL_UID="${TOOTHKEY_INSTALL_INVOKER_UID:-$(id -u "$REAL_USER")}"
REAL_GID="${TOOTHKEY_INSTALL_INVOKER_GID:-$(id -g "$REAL_USER")}"
REAL_HOME="${TOOTHKEY_INSTALL_INVOKER_HOME:-$(getent passwd "$REAL_USER" | cut -d: -f6)}"

if [ -z "$REAL_HOME" ] || [ ! -d "$REAL_HOME" ]; then
    echo "ERROR: couldn't locate home directory for $REAL_USER."
    exit 1
fi

echo "Installing Toothkey autostart for user: $REAL_USER"
echo "  uid=$REAL_UID gid=$REAL_GID home=$REAL_HOME"
echo "  source dir: $SCRIPT_DIR"
echo

# Sanity-check the source tree. We're writing absolute paths into unit
# files — better to fail now than to generate a broken service.
for f in worker.py tray.py start.sh; do
    if [ ! -f "$SCRIPT_DIR/$f" ]; then
        echo "ERROR: $SCRIPT_DIR/$f missing; can't reference it from systemd unit."
        exit 1
    fi
done

# ---------------------------------------------------------------------------
# Prepare the system: apt packages + BlueZ configuration. This was
# previously skipped by install.sh, which silently produced a worker
# service that crash-looped on `import dbus` / `import gi` / etc. on a
# machine that hadn't been bootstrapped via `./start.sh` first.
#
# We delegate to `start.sh --prepare-system` so the apt package list
# and the bluez init logic have exactly one source of truth (in
# start.sh's install_dependencies / init_bluez functions). That entry
# point installs deps, writes /etc/bluetooth/main.conf Class, drops in
# the bluetooth.service plugin override, and exits — it does NOT
# launch the tray or worker (we'll do that ourselves below via
# systemctl).
#
# Skip this if the caller passed --no-prepare (useful when iterating
# on install.sh and you know the system is already prepared).
# ---------------------------------------------------------------------------
SKIP_PREPARE=0
for arg in "$@"; do
    if [ "$arg" = "--no-prepare" ]; then
        SKIP_PREPARE=1
    fi
done

if [ "$SKIP_PREPARE" -eq 0 ]; then
    echo "Preparing system (apt deps + bluez config)..."
    # We're already root from the sudo -E re-exec above, so start.sh's
    # internal `sudo` calls are no-ops and won't prompt.
    if ! bash "$SCRIPT_DIR/start.sh" --prepare-system; then
        echo "ERROR: ./start.sh --prepare-system failed; not writing systemd units."
        echo "       Fix the failure above (usually apt failing to install a"
        echo "       package, or BlueZ not restarting) and re-run ./install.sh."
        exit 1
    fi
    echo
fi

PYTHON3=$(command -v python3 || true)
if [ -z "$PYTHON3" ]; then
    echo "ERROR: python3 not found on PATH."
    exit 1
fi

# ---------------------------------------------------------------------------
# Logs directory. Both the root worker and the user tray will write
# here. Ensure the dir exists, is owned by the user, and is group/
# world-writable so root (who always wins) and the user can both
# append without permission dances.
# ---------------------------------------------------------------------------
mkdir -p "$SCRIPT_DIR/logs"
chown "$REAL_USER:$REAL_GID" "$SCRIPT_DIR/logs"
chmod 0775 "$SCRIPT_DIR/logs"

# ---------------------------------------------------------------------------
# 1. System service for the worker (runs as root, starts at boot).
# ---------------------------------------------------------------------------
SERVICE_PATH=/etc/systemd/system/toothkey-worker.service
cat > "$SERVICE_PATH" <<EOF
# Managed by toothkey install.sh. Edit install.sh and re-run, do not
# hand-edit this file — a re-install will overwrite your changes.

[Unit]
Description=Toothkey BlueZ HID keyboard worker (root)
Documentation=https://github.com/bklein/toothkey
After=bluetooth.service
Requires=bluetooth.service

[Service]
Type=simple

# Socket path lives in /run/toothkey/ so it survives across user
# sessions (unlike /run/user/\$UID/toothkey/ which gets torn down on
# logout). ExecStartPre ensures the dir exists and has the right
# owner before the worker tries to bind inside it.
ExecStartPre=/bin/mkdir -p /run/toothkey
ExecStartPre=/bin/chmod 0755 /run/toothkey

# Keep the shared logs dir writable by the user too: the tray (which
# runs unprivileged) has to be able to append to the same files as
# the worker. Without this a prior root-only run would cause the
# user tray to EACCES on its first open().
ExecStartPre=/bin/chown -R $REAL_UID:$REAL_GID $SCRIPT_DIR/logs

# Worker proper. --socket is in /run/toothkey/ (persistent across
# user sessions); the env vars below tell the worker to chown the
# socket to the logged-in user and to wait forever for a tray
# connection (installed mode).
#
# WorkingDirectory is set to the project root so any code path that
# still opens a file by relative name (e.g. hid_sdp_record.xml, log
# files during debugging) finds it. The code already prefers absolute
# paths, but this is cheap insurance against a future regression.
WorkingDirectory=$SCRIPT_DIR
Environment=TOOTHKEY_SOCKET_OWNER=$REAL_UID:$REAL_GID
Environment=TOOTHKEY_ACCEPT_TIMEOUT=0
Environment=TOOTHKEY_INSTALLED=1
ExecStart=$PYTHON3 -u $SCRIPT_DIR/worker.py --socket /run/toothkey/ipc.sock

# The worker exits when the tray disconnects (clean shutdown via UDS
# or an uncaught error). Auto-restart so a tray re-launch — e.g. the
# user clicking Restart, or the user logging out and back in —
# transparently gets a fresh worker. RestartSec=2 avoids a tight
# loop if the worker is crashing immediately.
#
# RestartPreventExitStatus=42 carves out the "user clicked Exit in
# the tray menu" path: tray sends `shutdown` over the UDS, worker
# exits with 42, systemd leaves us dead. Mirrors the tray unit's own
# RestartPreventExitStatus=42. Restart-via-tray still works because
# tray.py follows up with an explicit `sudo systemctl restart
# toothkey-worker.service`, which ignores RestartPreventExitStatus.
Restart=always
RestartPreventExitStatus=42
RestartSec=2

# journald + the worker's own logs/ files both get the output. This
# is useful because \`systemctl status\` / \`journalctl -u\` give a
# convenient at-a-glance view, but logs/toothkey.log is still the
# source of truth for debugging cross-process interactions.
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
chmod 0644 "$SERVICE_PATH"
echo "  wrote $SERVICE_PATH"

# ---------------------------------------------------------------------------
# 2. User service for the tray (starts at login, runs as the user).
# ---------------------------------------------------------------------------
USER_SVC_DIR="$REAL_HOME/.config/systemd/user"
install -d -m 0755 -o "$REAL_USER" -g "$REAL_GID" "$USER_SVC_DIR"

USER_SERVICE_PATH="$USER_SVC_DIR/toothkey-tray.service"
cat > "$USER_SERVICE_PATH" <<EOF
# Managed by toothkey install.sh. Hand-edits will be overwritten by
# a re-install. Runs the unprivileged tray half of Toothkey.

[Unit]
Description=Toothkey system tray
# graphical-session.target is fired by the DE (KDE, GNOME, XFCE)
# once DISPLAY/WAYLAND_DISPLAY/XDG_* are all set. Tying ourselves
# to it guarantees Qt can reach the compositor on start.
After=graphical-session.target
PartOf=graphical-session.target
# If the worker service hasn't come up yet (e.g. we're logging in
# within seconds of boot) the tray will just wait on connect() and
# systemd's Restart=on-failure covers the rest.

[Service]
Type=simple
WorkingDirectory=$SCRIPT_DIR
Environment=TOOTHKEY_INSTALLED=1
ExecStart=$PYTHON3 -u $SCRIPT_DIR/tray.py --socket /run/toothkey/ipc.sock

# Restart policy:
#   Restart=always               => tray comes back on any exit
#                                   except the explicitly-excluded
#                                   code below. Covers crashes,
#                                   `pkill toothkey`, SIGTERM from
#                                   session logout/login, and the
#                                   tray menu's Restart click.
#   RestartPreventExitStatus=42  => carved-out code the tray uses
#                                   when the user picks "Exit" from
#                                   the menu. We do NOT want to
#                                   respawn in that case.
# Keeping these two in lockstep with tray.py's _quit_now()'s exit
# code contract is what makes Exit/Restart/pkill all behave sanely.
Restart=always
RestartPreventExitStatus=42
RestartSec=2

[Install]
WantedBy=graphical-session.target
EOF
chown "$REAL_USER:$REAL_GID" "$USER_SERVICE_PATH"
chmod 0644 "$USER_SERVICE_PATH"
echo "  wrote $USER_SERVICE_PATH"

# ---------------------------------------------------------------------------
# 3. sudoers drop-in: allow the user to {start,stop,restart} the
# toothkey-worker system service without a password. The Restart menu
# item in the tray needs this to avoid popping a GUI askpass dialog on
# every click.
#
# We whitelist the EXACT systemctl invocations, not "all of systemctl"
# — keeps the blast radius small if someone else ever gets command
# injection into tray.py.
# ---------------------------------------------------------------------------
SYSTEMCTL_PATH=$(command -v systemctl)
SUDOERS_PATH=/etc/sudoers.d/toothkey
cat > "$SUDOERS_PATH" <<EOF
# Managed by toothkey install.sh. Allows $REAL_USER to restart/start/
# stop the toothkey-worker system service without re-authenticating.
$REAL_USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH restart toothkey-worker.service
$REAL_USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH start toothkey-worker.service
$REAL_USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH stop toothkey-worker.service
EOF
chmod 0440 "$SUDOERS_PATH"
# Validate before anyone else reads it — a syntax error in sudoers
# locks EVERYONE out of sudo.
if ! visudo -cf "$SUDOERS_PATH" >/dev/null; then
    echo "ERROR: visudo rejected $SUDOERS_PATH; removing it."
    rm -f "$SUDOERS_PATH"
    exit 1
fi
echo "  wrote $SUDOERS_PATH"

# ---------------------------------------------------------------------------
# 4. Application-menu entry. Drops a .desktop + SVG icon into
# ~/.local/share so "Tooth-key" shows up in KDE / GNOME / XFCE
# launchers next to every other GUI app on the box.
#
# We delegate to `start.sh --install-launcher`, run as the real user
# with HOME pointed at their home dir, so the .desktop format / icon
# layout has exactly one source of truth (start.sh's install_launcher
# function). Failures here are non-fatal: a missing menu entry is a
# UX regression, not a functional one — the autostarted tray will
# still come up on next login.
# ---------------------------------------------------------------------------
echo "Installing application-menu entry..."
USER_RUNTIME_DIR="/run/user/$REAL_UID"
LAUNCHER_ENV=( "HOME=$REAL_HOME" )
if [ -d "$USER_RUNTIME_DIR" ]; then
    # kbuildsycoca6 (KDE) writes into XDG_RUNTIME_DIR — pass it
    # through when it exists so the new .desktop is picked up
    # without a re-login.
    LAUNCHER_ENV+=( "XDG_RUNTIME_DIR=$USER_RUNTIME_DIR" )
fi
if sudo -u "$REAL_USER" "${LAUNCHER_ENV[@]}" \
        bash "$SCRIPT_DIR/start.sh" --install-launcher; then
    echo "  application-menu : Tooth-key entry installed"
else
    echo "  WARN: launcher install failed; tray will still autostart on login."
fi

# ---------------------------------------------------------------------------
# 5. Enable + (re)start the services.
# ---------------------------------------------------------------------------
echo
echo "Enabling services..."

systemctl daemon-reload
systemctl enable --now toothkey-worker.service
echo "  system-wide  : toothkey-worker.service (enabled, started)"

# User service: enable via the user's own systemd --user instance.
# That requires the user's XDG_RUNTIME_DIR to exist — normally it
# does whenever the user has an active login, but it doesn't from
# e.g. an SSH session without lingering. Attempt enable and fall
# back to a warning message if the user bus isn't reachable.
if [ -d "$USER_RUNTIME_DIR" ] \
   && sudo -u "$REAL_USER" XDG_RUNTIME_DIR="$USER_RUNTIME_DIR" \
        systemctl --user daemon-reload 2>/dev/null; then
    sudo -u "$REAL_USER" XDG_RUNTIME_DIR="$USER_RUNTIME_DIR" \
        systemctl --user enable toothkey-tray.service
    # Try to start it now too, so the user sees the tray icon appear
    # immediately without having to log out and back in. If the
    # user's graphical-session.target isn't active yet (e.g. they're
    # installing over SSH) the start will fail harmlessly.
    if sudo -u "$REAL_USER" XDG_RUNTIME_DIR="$USER_RUNTIME_DIR" \
        systemctl --user start toothkey-tray.service 2>/dev/null; then
        echo "  per-user     : toothkey-tray.service (enabled, started)"
    else
        echo "  per-user     : toothkey-tray.service (enabled; will start on next graphical login)"
    fi
else
    echo "  per-user     : toothkey-tray.service (enabled-on-next-login; user bus not reachable from here)"
fi

echo
echo "Install complete."
echo
echo "Commands you might want:"
echo "  systemctl status toothkey-worker          # is the worker up?"
echo "  systemctl --user status toothkey-tray     # is the tray up?"
echo "  journalctl -u toothkey-worker -f          # live worker logs"
echo "  ./uninstall.sh                            # undo this install"
