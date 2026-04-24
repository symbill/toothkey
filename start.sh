#!/bin/bash

# Re-exec ourselves with argv[0] set to "toothkey-start" so that any
# short-lived invocation is discoverable via `ps aux | grep toothkey`
# / `pgrep toothkey`. The env guard prevents an exec loop. `$0` still
# resolves to the real script path inside the child so sourcing
# sibling files keeps working.
if [ "${TOOTHKEY_START_EXECD:-}" != "1" ]; then
    # Exec via an explicit bash invocation so the kernel doesn't
    # discard our argv[0] the way it does for shebang-dispatched
    # scripts (execve of a script re-execs the interpreter with a
    # fresh argv, losing the name we set here).
    export TOOTHKEY_START_EXECD=1
    exec -a toothkey-start /bin/bash "$0" "$@"
fi

# Absolute path of this script's directory, used everywhere we
# reference sibling files (tray.py, main.py, worker.py, logs/, etc.).
# Resolving it once up-front means the script works no matter where
# it's invoked from (e.g. the .desktop launcher's cwd is $HOME).
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

function install_dependencies() {
    echo "Installing dependencies..."
    sudo apt update
    sudo apt install -y \
        bluez bluez-firmware bluez-obexd bluez-tools \
        python3 python3-pip \
        python3-pynput python3-dbus python3-gi python3-bluez \
        python3-pyqt5 python3-pyqt5.qtsvg \
        python3-setproctitle
}

function ensure_main_conf_class() {
    # BlueZ recomputes the adapter's Class of Device on every profile /
    # UUID change via the mgmt API, using `Class = ...` from main.conf for
    # major/minor. If main.conf doesn't force major=0x05 (Peripheral) and
    # minor=0x10 (Keyboard), the iPhone sees us as a generic device and
    # refuses HID pairing — regardless of what we btmgmt-set at runtime.
    local conf=/etc/bluetooth/main.conf
    local desired='Class = 0x000540'
    if [ ! -f "$conf" ]; then
        echo "  ! $conf missing; skipping Class enforcement"
        return
    fi
    if grep -qE '^[[:space:]]*Class[[:space:]]*=[[:space:]]*0x000540' "$conf"; then
        echo "  = $conf already has '$desired'"
        return
    fi
    if grep -qE '^[[:space:]]*#?[[:space:]]*Class[[:space:]]*=' "$conf"; then
        sudo sed -i.bak -E "s|^[[:space:]]*#?[[:space:]]*Class[[:space:]]*=.*|$desired|" "$conf"
    else
        sudo sed -i.bak "/^\[General\]/a $desired" "$conf"
    fi
    echo "  + wrote '$desired' into $conf"
}

function write_toothkey_override() {
    # Any stray drop-in in /etc/systemd/system/bluetooth.service.d/ will
    # take precedence over our edits to the packaged unit file. Rather
    # than edit the packaged unit (which can also get overwritten on
    # `apt upgrade`), we own a single drop-in file and delete any other
    # stray drop-ins we find.
    #
    # $1 = "debug" to add `-d` (bluetoothd debug logging), empty otherwise.
    local mode="${1:-}"
    local extra_flags=""
    if [ "$mode" = "debug" ]; then
        extra_flags=" -d"
    fi
    local dir=/etc/systemd/system/bluetooth.service.d
    local ours="$dir/toothkey.conf"
    sudo mkdir -p "$dir"
    # Nuke stray drop-ins (e.g. old debug.conf from a prior debugging session)
    # but keep ours.
    if ls "$dir"/*.conf >/dev/null 2>&1; then
        for f in "$dir"/*.conf; do
            if [ "$f" != "$ours" ]; then
                echo "  - removing stray drop-in: $f"
                sudo rm -f "$f"
            fi
        done
    fi
    sudo tee "$ours" >/dev/null <<EOF
# Managed by start.sh. Disables every bluez plugin that registers SDP
# UUIDs, because each such UUID sets a service-class bit in the adapter's
# CoD. When the adapter ends up advertising Audio / Telephony / Object-
# Transfer service bits on top of Peripheral/Keyboard, iOS decides we're
# not a "real" HID device and rejects pairing with HCI 0x0E "Connection
# Rejected due to Security Reasons" — or accepts the ACL but never opens
# the HID L2CAP channels, which leaves iOS showing "Connected" while we
# show disconnected (because PSM 0x11/0x13 accept() is still blocked).
#
# Gotcha: -P is last-one-wins; must be a single comma-separated list.
#
#   input     : would bind PSM 0x0011/0x0013 (our HID channels) AND claim
#               the HID UUID (RegisterProfile -> "UUID already registered").
#   hostname  : would read /etc/machine-info / DMI chassis and force adapter
#               class to Computer/Desktop, overriding main.conf Class.
#   sink / source / a2dp       : A2DP audio   -> Audio service-class bit.
#   target / control / avrcp   : AVRCP        -> Audio service-class bit.
#   media                      : loads a2dp/avrcp en-bloc -> Audio bit.
#   handsfree / hfp            : HFP          -> Telephony service bit.
#                                (This was the missing one responsible for
#                                 0x400 still being set even after a2dp
#                                 was disabled — bluez ships Telephony
#                                 separately from Audio plugins.)
#   network                    : BNEP / NAP / PANU / GN -> Networking bit.
#   hog                        : HID over GATT — we do classic HID, HoG
#                                would confuse iOS's service cache.
#   bap / csip / vcp           : LE audio; they register GATT services
#                                that iOS picks up as "weird extras".
#   autopair                   : auto-accepts pairings silently; we want
#                                our own agent to handle SSP instead.
#   neard                      : NFC / neard integration.
#   micp / sap / suspend       : LE audio / SIM access / suspend helpers.
#
# NOT disabled here: pnp, gatt, gap, battery, device-info — these are
# either required for basic discovery/pairing or benign for HID.
[Service]
ExecStart=
ExecStart=/usr/libexec/bluetooth/bluetoothd -P input,hostname,sink,source,a2dp,target,control,avrcp,media,handsfree,hfp,network,hog,bap,csip,vcp,autopair,neard,micp,sap,suspend${extra_flags}
EOF
    echo "  + wrote $ours${extra_flags:+ (debug mode)}"
}

function quiet_obex_services() {
    # BlueZ ships a separate obex daemon (bluez-obexd / obexd) that
    # registers OPP / FTP / PBAP / MAP over SDP via bluetoothd's D-Bus
    # API. Even with the a2dp/hfp plugins disabled, if obexd is running
    # those UUIDs set the Object-Transfer (0x100) bit in the adapter's
    # CoD. Result: an adapter with Class=0x100540 instead of 0x000540,
    # and iOS refuses to treat us as a classic HID peripheral.
    #
    # We only stop currently-running instances — we don't `disable` or
    # `mask`, because that would affect all user sessions system-wide
    # and break things like "Send file via Bluetooth" in Dolphin /
    # Nautilus. Start.sh is a foreground session; user can reboot or
    # `systemctl --user start obex` afterwards to get OBEX back.
    local stopped_any=0
    for svc in bluez-obexd obex; do
        if systemctl --user is-active --quiet "$svc" 2>/dev/null; then
            echo "  - stopping user service: $svc"
            systemctl --user stop "$svc" 2>/dev/null || true
            stopped_any=1
        fi
        if sudo systemctl is-active --quiet "$svc" 2>/dev/null; then
            echo "  - stopping system service: $svc"
            sudo systemctl stop "$svc" 2>/dev/null || true
            stopped_any=1
        fi
    done
    # The OBEX service can also live under different names in various
    # distros / desktops. Best-effort — sweep anything that looks like
    # an obex process that's still lingering.
    local obex_pids
    obex_pids=$(pgrep -a obexd 2>/dev/null | awk '{print $1}' || true)
    if [ -n "$obex_pids" ]; then
        echo "  - killing lingering obexd pids: $obex_pids"
        # shellcheck disable=SC2086
        sudo kill -TERM $obex_pids 2>/dev/null || true
        stopped_any=1
    fi
    if [ "$stopped_any" -eq 0 ]; then
        echo "  = no obex daemon running (good)"
    fi
}

function init_bluez() {
    echo "Initializing BlueZ..."
    write_toothkey_override "${1:-}"
    ensure_main_conf_class
    quiet_obex_services
    echo "  . systemctl daemon-reload + restart bluetooth"
    sudo systemctl daemon-reload
    sudo systemctl restart bluetooth

    echo "BlueZ reinitialised. Effective config:"
    # systemctl show is the authoritative source: it merges the packaged
    # unit + every drop-in in /etc/systemd/system/bluetooth.service.d/
    local effective_exec
    effective_exec=$(systemctl show bluetooth -p ExecStart --value 2>/dev/null \
        | grep -oE 'path=[^ ;]+ ; argv\[\]=[^;]+' \
        | head -1 \
        | sed -E 's/.*argv\[\]=//; s/[[:space:]]+$//')
    local effective_class
    effective_class=$(grep -E '^[[:space:]]*Class[[:space:]]*=' /etc/bluetooth/main.conf 2>/dev/null | head -1)
    local running_cmd
    running_cmd=$(ps -o cmd= -p "$(pidof bluetoothd | awk '{print $1}')" 2>/dev/null)
    echo "  effective ExecStart: ${effective_exec:-<missing>}"
    echo "  main.conf:           ${effective_class:-<not set>}"
    echo "  running proc:        ${running_cmd:-<not running?>}"
}

function reset_pairings() {
    echo "Removing all Linux-side BT pairings (bonds on iPhone are unaffected)..."
    local macs
    macs=$(sudo bluetoothctl devices | awk '{print $2}')
    for mac in $macs; do
        echo "  bluetoothctl remove $mac"
        sudo bluetoothctl remove "$mac" >/dev/null
    done
    rm -f client_mac_address.cache
    echo "Also forget the device on your iPhone (Settings > Bluetooth > (i) > Forget This Device)."
}

function install_launcher() {
    # Install the .desktop file + icon into the user's application menu
    # so Tooth-key shows up in Kubuntu's launcher / Ubuntu's Activities.
    # Idempotent: running multiple times just overwrites.
    local repo
    repo=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

    local apps_dir="$HOME/.local/share/applications"
    local icons_dir="$HOME/.local/share/icons/hicolor/scalable/apps"
    mkdir -p "$apps_dir" "$icons_dir"

    # Materialise the .desktop from the template, substituting the real repo path.
    sed "s|%REPO%|$repo|g" "$repo/toothkey.desktop.template" \
        > "$apps_dir/toothkey.desktop"
    chmod +x "$apps_dir/toothkey.desktop"
    cp -f "$repo/toothkey.svg" "$icons_dir/toothkey.svg"

    # Refresh the icon cache so the freshly-installed icon is actually
    # picked up by the desktop. gtk-update-icon-cache is best-effort;
    # on some KDE-only installs it's not present and we no-op.
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -q -t "$HOME/.local/share/icons/hicolor" || true
    fi
    # KDE reads .desktop files from ~/.local/share/applications live,
    # but kbuildsycoca5/6 speeds that up when it exists.
    if command -v kbuildsycoca6 >/dev/null 2>&1; then
        kbuildsycoca6 --noincremental >/dev/null 2>&1 || true
    elif command -v kbuildsycoca5 >/dev/null 2>&1; then
        kbuildsycoca5 --noincremental >/dev/null 2>&1 || true
    fi

    echo "Installed:"
    echo "  $apps_dir/toothkey.desktop"
    echo "  $icons_dir/toothkey.svg"
    echo
    echo "Tooth-key should now appear in your application launcher."
    echo "To uninstall, run: $0 --uninstall-launcher"
}

function uninstall_launcher() {
    rm -f "$HOME/.local/share/applications/toothkey.desktop"
    rm -f "$HOME/.local/share/icons/hicolor/scalable/apps/toothkey.svg"
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -q -t "$HOME/.local/share/icons/hicolor" || true
    fi
    echo "Uninstalled launcher + icon from ~/.local/share."
}

function print_usage() {
    cat <<EOF
usage: $0 [flag]

    (no flag)            launch the tray app (first time: install deps + init bluez)
    --cli                launch in terminal-only mode (main.py, no tray)
    --reset-all          reinstall deps, reinit bluez, then launch the app
    --reset-bluez        reinit bluez (systemd service + main.conf) and exit
    --reset-pairings     drop all linux-side BT pairings and exit
    --debug-on           enable bluetoothd debug (-d) and restart it; exit
    --debug-off          disable bluetoothd debug and restart it; exit
    --install-launcher   install Tooth-key into the application menu; exit
    --uninstall-launcher remove the application-menu entry; exit
    -h, --help           show this message and exit
EOF
}

case "$1" in
    -h|--help)
        print_usage
        exit 0
        ;;
    --reset-bluez)
        # One-shot: reconfigure bluez (systemd unit + main.conf), restart
        # the service, and exit. Use this when you want to apply config
        # changes without immediately starting the app so you can toggle
        # Bluetooth on the iPhone to flush its cached EIR first.
        init_bluez
        echo "Done. Re-run $0 (no flags) to start the app."
        exit 0
        ;;
    --debug-on)
        init_bluez debug
        echo "Done. bluetoothd is now running with -d. Tail it with:"
        echo "  sudo journalctl -u bluetooth -f --output=short-iso-precise | tee logs/bluetoothd.log"
        exit 0
        ;;
    --debug-off)
        init_bluez
        echo "Done. bluetoothd debug output disabled."
        exit 0
        ;;
    --reset-all)
        echo "initiating (reset-all)..."
        install_dependencies
        init_bluez
        touch .initiated
        ;;
    --reset-pairings)
        # One-shot: drop bonds and exit (matches --reset-bluez semantics).
        reset_pairings
        exit 0
        ;;
    --install-launcher)
        install_launcher
        exit 0
        ;;
    --uninstall-launcher)
        uninstall_launcher
        exit 0
        ;;
    --cli)
        # Fall through to the launch phase; the second-level if below
        # picks main.py instead of tray.py when $1 == --cli.
        if [ ! -f ".initiated" ]; then
            echo "initiating (first run)..."
            install_dependencies
            init_bluez
            touch .initiated
        fi
        ;;
    "")
        if [ ! -f ".initiated" ]; then
            echo "initiating (first run)..."
            install_dependencies
            init_bluez
            touch .initiated
        fi
        ;;
    *)
        echo "unknown flag: $1"
        print_usage
        exit 1
        ;;
esac

# Idempotent: make sure main.conf has the right Class every launch. If the
# sed actually changes something, restart bluetooth so bluez re-reads it.
before=$(sha256sum /etc/bluetooth/main.conf 2>/dev/null || echo "")
ensure_main_conf_class
after=$(sha256sum /etc/bluetooth/main.conf 2>/dev/null || echo "")
if [ "$before" != "$after" ]; then
    echo "main.conf changed; restarting bluetooth.service"
    sudo systemctl restart bluetooth
fi

# pynput + QSystemTrayIcon both need access to the X/Wayland display. As
# we run python as root (L2CAP raw sockets require CAP_NET_RAW), grant
# root permission to use the current user's X display. xhost is a no-op
# on pure Wayland sessions; that's fine \u2014 Qt will fall back to the
# Wayland socket directly.
xhost +SI:localuser:root >/dev/null 2>&1 || true

# Re-run install_dependencies if a required Python module is missing.
# This covers the case where the dep list has grown since this machine
# was first initialised (.initiated is a marker, not a manifest). We
# check as the invoking user, not root, because Python sees the same
# system packages either way and we want to avoid an avoidable sudo
# prompt when everything is already installed.
function ensure_python_deps() {
    local missing=()
    local mod
    for mod in pynput dbus gi bluetooth PyQt5.QtSvg PyQt5.QtWidgets setproctitle; do
        if ! python3 -c "import $mod" >/dev/null 2>&1; then
            missing+=("$mod")
        fi
    done
    if [ "${#missing[@]}" -gt 0 ]; then
        echo "Missing python modules: ${missing[*]}"
        echo "Re-running install_dependencies to catch up..."
        install_dependencies
    fi
}
ensure_python_deps

# Detect and fix BlueZ config drift. The .initiated file is written on
# first install, so an old machine can easily be stuck with a stale
# systemd drop-in / old -P plugin list from whenever start.sh was last
# run. We re-initialise whenever the effective bluetoothd ExecStart
# doesn't match what write_toothkey_override would produce NOW.
#
# This replaces the previous UX where a user had to remember to run
# `./start.sh --reset-bluez` manually after pulling an update that
# changed the plugin blacklist — and silently got the old CoD (with
# Audio/Telephony/OBEX service bits) until they did.
function ensure_bluez_config_current() {
    local desired_drop_in="/etc/systemd/system/bluetooth.service.d/toothkey.conf"

    # No drop-in at all -> definitely need to init.
    # (File is 0644 world-readable, so no sudo needed just to probe it.
    # Using sudo here would pop an unnecessary GUI askpass dialog in
    # the no-tty Restart flow.)
    if [ ! -f "$desired_drop_in" ]; then
        echo "No BlueZ drop-in found -> initialising BlueZ..."
        init_bluez
        return
    fi

    # Extract the -P plugin list our drop-in would WANT bluetoothd to
    # run with (the ExecStart line embedded in write_toothkey_override).
    # If the file on disk doesn't match, regenerate. We grep out the
    # -P argument and compare as a simple string match — any drift in
    # the blacklist forces a regen.
    local desired_plugins="input,hostname,sink,source,a2dp,target,control,avrcp,media,handsfree,hfp,network,hog,bap,csip,vcp,autopair,neard,micp,sap,suspend"
    local actual_plugins
    actual_plugins=$(grep -oE '\-P [^[:space:]]+' "$desired_drop_in" 2>/dev/null | head -1 | awk '{print $2}' || true)
    if [ "$actual_plugins" != "$desired_plugins" ]; then
        echo "BlueZ drop-in plugin list is out of date"
        echo "  on disk: ${actual_plugins:-<missing>}"
        echo "  wanted : $desired_plugins"
        echo "Re-initialising BlueZ..."
        init_bluez
        return
    fi

    # Drop-in is correct on disk — but bluetoothd may still be running
    # with an older copy if nobody restarted it since the last edit.
    # Compare the running process's cmdline against what we want.
    local running_plugins
    running_plugins=$(ps -ww -o cmd= -p "$(pidof bluetoothd | awk '{print $1}')" 2>/dev/null \
                      | grep -oE '\-P [^[:space:]]+' | head -1 | awk '{print $2}' || true)
    if [ "$running_plugins" != "$desired_plugins" ]; then
        echo "bluetoothd is running with an outdated -P list"
        echo "  running: ${running_plugins:-<not running?>}"
        echo "  wanted : $desired_plugins"
        echo "Restarting bluetoothd + quieting obex..."
        quiet_obex_services
        sudo systemctl daemon-reload
        sudo systemctl restart bluetooth
    else
        # Even if bluetoothd matches, obex daemons may have started
        # since last boot and be contributing OBEX UUIDs to our
        # adapter's CoD. Stop them — cheap and idempotent.
        quiet_obex_services
    fi
}
ensure_bluez_config_current

# ---------------------------------------------------------------------------
# If install.sh has wired up toothkey as system + user systemd units,
# we shouldn't double-launch. Detect an active worker service and
# bail with a friendly message. Still allow `./start.sh --cli` to
# override, since that's a developer debug flow and the user opted in.
# ---------------------------------------------------------------------------
if [ "${1:-}" != "--cli" ] \
   && command -v systemctl >/dev/null 2>&1 \
   && systemctl is-active --quiet toothkey-worker.service 2>/dev/null; then
    echo
    echo "Toothkey is already running as a systemd service"
    echo "  (installed via ./install.sh). Nothing for ./start.sh to do."
    echo
    echo "Useful commands:"
    echo "  systemctl status toothkey-worker          # worker status"
    echo "  systemctl --user status toothkey-tray     # tray status"
    echo "  systemctl --user restart toothkey-tray    # reload the tray UI"
    echo "  sudo systemctl restart toothkey-worker    # reload the BT worker"
    echo "  ./uninstall.sh                            # disable autostart,"
    echo "                                            # then ./start.sh works normally"
    exit 0
fi

# --cli still runs main.py as root in the foreground (no tray, no
# split). Useful for debugging and for headless setups that don't
# have a graphical session.
if [ "${1:-}" = "--cli" ]; then
    sudo -E python3 "$SCRIPT_DIR/main.py"
    exit $?
fi

# Tray flow (default): two detached processes — a root-side worker
# (L2CAP raw sockets, pynput) and a user-side Qt tray — that talk
# over a Unix-domain socket. The worker is the server; the tray is
# the client. Privileged process is the stable one, so a tray
# restart doesn't cost us a sudo prompt or a BT re-init.
#
# The subtle bit is getting sudo to (a) authenticate without a
# prompt once we've already prompted once, and (b) keep the worker
# alive past the user's shell exit. What does NOT work:
#
#   setsid sudo -E python3 worker.py ... &
#     setsid creates a new session with no controlling tty, so sudo
#     has nowhere to prompt AND its default timestamp_type=tty cache
#     can't find a matching (user,tty) row either. Result: sudo
#     dies with "a terminal is required", worker never starts.
#
#   sudo -E python3 worker.py ... &
#     When the shell later exits, SIGHUP propagates to the job and
#     kills the worker. Also `$!` is sudo's pid, which is fine to
#     signal but dies with the shell absent nohup.
#
# What works (and is what we do below):
#
#   1. `sudo -v` in the foreground to prompt the user once and
#      prime the tty-scoped credential cache.
#   2. `nohup sudo -n -E python3 worker.py ... & disown`. nohup
#      makes the process tree ignore SIGHUP (survives shell exit)
#      without severing the tty, so `sudo -n` still sees the cached
#      (user,tty) credential and goes straight through. The worker
#      runs as root, the socket gets chowned back to us.

TOOTHKEY_LOG="$SCRIPT_DIR/logs/toothkey.log"
mkdir -p "$SCRIPT_DIR/logs"

# Critical ownership step: if a previous run created files in logs/
# while worker (root) was the first writer, the files/directory end
# up root-owned and the user-side tray can't append to them. That
# failure mode was especially nasty because the tray's logging_setup
# would bail with a PermissionError on toothkey.log, before it ever
# got a chance to log anything — so the tray died silently with an
# empty bootstrap log. Fix up anything root-owned to us now.
NEEDS_LOGS_CHOWN=0
if [ -n "$(find "$SCRIPT_DIR/logs" -maxdepth 1 -not -user "$USER" -print -quit 2>/dev/null)" ]; then
    NEEDS_LOGS_CHOWN=1
fi

# ---------------------------------------------------------------------------
# Prime sudo credentials ONCE, up front.
#
# The BT worker has to run as root (raw L2CAP sockets), and several
# smaller steps below also need root: fixing ownership of logs/ files
# left root-owned by a previous session, killing a root-owned worker
# from a previous run that won't respond to graceful shutdown, etc.
# If we authenticated lazily at each of those points we could end up
# popping the GUI password dialog two or three times in a row when
# the tray's Restart menu item relaunches us. Instead we authenticate
# once here and then use `sudo -n` everywhere downstream so the cache
# is guaranteed to be warm.
#
# Two prompt styles depending on how we were invoked:
#
#   A. Interactive (`./start.sh` from a terminal):
#      Use `sudo -v`. sudo prints its standard "[sudo] password:"
#      prompt to the tty. Same as every other shell invocation.
#
#   B. Launched by the tray's Restart menu item:
#      TOOTHKEY_GUI_SUDO=1 is set and SUDO_ASKPASS points at a
#      GUI helper (ksshaskpass / ssh-askpass / gnome-ssh-askpass).
#      Use `sudo -A -v`, which tells sudo to exec $SUDO_ASKPASS for
#      the password — a native password dialog pops up. Requires
#      no tty, no terminal emulator, no sudoers tweaks.
#
# If TOOTHKEY_GUI_SUDO is set but SUDO_ASKPASS is empty, we still try
# sudo -v (which will fail gracefully if there's genuinely no way to
# read a password) and fall through to the error branch below.
echo "Caching sudo credentials (needed for the BT worker)..."
if [ -n "${TOOTHKEY_GUI_SUDO:-}" ] && [ -n "${SUDO_ASKPASS:-}" ]; then
    echo "  (using GUI askpass: $SUDO_ASKPASS)"
    if ! sudo -A -v; then
        echo "ERROR: sudo auth failed via askpass; aborting."
        exit 1
    fi
else
    if ! sudo -v; then
        echo "ERROR: sudo auth failed; aborting."
        exit 1
    fi
fi

# Now that we hold the sudo cache, fix any root-owned logs/ files. Has
# to happen before the `: > "$TOOTHKEY_LOG"` truncation below — root-
# owned files would EACCES on truncate as the invoking user.
if [ "$NEEDS_LOGS_CHOWN" = "1" ]; then
    echo "Fixing ownership of logs/ files that were root-owned from a prior run..."
    sudo -n chown -R "$USER:$USER" "$SCRIPT_DIR/logs"
fi

# Short-lived bootstrap logs for the two processes. Each gets its
# own file so a crash on import / before logging_setup.install()
# runs is distinguishable by process. Once install() takes over
# these stop filling and everything goes to toothkey.log instead.
WORKER_BOOT_LOG="$SCRIPT_DIR/logs/worker-bootstrap.log"
TRAY_BOOT_LOG="$SCRIPT_DIR/logs/tray-bootstrap.log"

# Pre-create every log file we're about to write as the invoking
# user. Root can always append to a user-owned file, but the
# reverse isn't true — if we let the worker create toothkey.log
# first, it'd be mode 0600 root:root and the tray would EACCES on
# open. Pre-touching fixes the race by planting the right
# ownership + mode before either process starts.
WORKER_DIAG_LOG="$SCRIPT_DIR/logs/worker-diag.log"
: > "$WORKER_BOOT_LOG"
: > "$TRAY_BOOT_LOG"
: > "$WORKER_DIAG_LOG"
# Truncate toothkey.log at session start too. Previous versions just
# touched it, which meant output from older sessions kept accumulating
# and made it genuinely impossible to distinguish "this session's
# events" from "last week's events" when diagnosing a reconnect issue.
# worker-diag.log already gets truncated each session — align them.
: > "$TOOTHKEY_LOG"
chmod u+rw,g+r "$TOOTHKEY_LOG" "$WORKER_BOOT_LOG" "$TRAY_BOOT_LOG" "$WORKER_DIAG_LOG" 2>/dev/null || true

# Socket path: prefer XDG_RUNTIME_DIR (ramdisk, per-user, wiped on
# logout) over /tmp. Put it in a toothkey/ subdir so we own the
# directory and can `rm -rf` it on next start without touching
# unrelated files.
RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp}"
TOOTHKEY_RUN="$RUNTIME_DIR/toothkey"
mkdir -p "$TOOTHKEY_RUN"
chmod 700 "$TOOTHKEY_RUN"
SOCKET_PATH="$TOOTHKEY_RUN/ipc.sock"

# ---------------------------------------------------------------------------
# Kill any previously-running toothkey. Relies on the sudo credential
# cache having already been primed above — any SIGTERM/SIGKILL fallback
# here uses `sudo -n` only. Using non-`-n` fallbacks would pop a second
# GUI password dialog on tray-Restart (the first being from the priming
# `sudo -A -v` call), which is exactly the bug we're avoiding.
# ---------------------------------------------------------------------------
function kill_previous_instances() {
    local my_pid=$$

    # 1. Catch every process named by proc_title: toothkey-worker,
    #    toothkey-tray, toothkey-start, toothkey-debug, toothkey-cli,
    #    toothkey-hcimon. The old toothkey-start from a previous launch
    #    is already gone (start.sh exits after spawning worker+tray),
    #    but the others may linger.
    # 2. Also catch any sudo wrapper that's holding the worker python
    #    as a child — pgrep on proc titles won't match the sudo parent.
    # 3. Exclude ourselves ($$) and our own parent chain so we don't
    #    SIGTERM the shell that invoked us.
    local titled_pids
    titled_pids=$(pgrep -x 'toothkey-worker|toothkey-tray|toothkey-cli|toothkey-hcimon' 2>/dev/null || true)
    local sudo_pids
    sudo_pids=$(pgrep -f "sudo.* python3 .*$SCRIPT_DIR/worker.py" 2>/dev/null || true)
    # Prior start.sh / debug.sh sessions that are still in-flight.
    # Exclude our own pid and our direct parent.
    local my_ppid="${PPID:-0}"
    local script_pids
    script_pids=$(pgrep -x 'toothkey-start|toothkey-debug' 2>/dev/null \
        | grep -vE "^(${my_pid}|${my_ppid})$" || true)

    local all_pids
    all_pids=$(printf '%s\n%s\n%s\n' "$titled_pids" "$sudo_pids" "$script_pids" \
               | tr ' ' '\n' | sort -u | grep -vE "^(${my_pid}|${my_ppid}|)$" || true)

    if [ -z "$all_pids" ]; then
        return 0
    fi

    echo "Stopping previously-running toothkey instance(s)..."
    echo "  pids: $(echo $all_pids | tr '\n' ' ')"

    # Try a clean shutdown via the UDS first, if the old worker's
    # socket is still around. One graceful shutdown round-trip gives
    # us clean L2CAP teardown (ACL disconnect to the iPhone, etc)
    # instead of leaving those to time out on the remote end.
    if [ -S "$SOCKET_PATH" ]; then
        echo "  - attempting graceful shutdown via UDS"
        python3 - "$SOCKET_PATH" <<'PY' 2>/dev/null || true
import socket, sys, time
path = sys.argv[1]
try:
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(1.0)
    s.connect(path)
    s.sendall(b'{"type":"shutdown"}\n')
    # Read until EOF (the worker closes the socket when it exits)
    # but cap at 3s so a stuck worker doesn't stall us.
    s.settimeout(3.0)
    while True:
        try:
            if not s.recv(4096):
                break
        except socket.timeout:
            break
    s.close()
except Exception:
    pass
PY
        # Give the graceful path up to ~2s to wind down before we
        # escalate. Typical teardown is under 500ms.
        for _ in 1 2 3 4; do
            sleep 0.5
            local any_alive=0
            for p in $all_pids; do
                if kill -0 "$p" 2>/dev/null; then any_alive=1; break; fi
            done
            [ "$any_alive" -eq 0 ] && break
        done
    fi

    # Anything still alive gets SIGTERM (needs sudo since the worker
    # runs as root; use -n so we don't prompt here — if there's no
    # cached credential, sudo fails silently and we fall through to
    # SIGKILL below, which will also need credentials anyway).
    local survivors
    survivors=$(for p in $all_pids; do kill -0 "$p" 2>/dev/null && echo "$p"; done)
    if [ -n "$survivors" ]; then
        echo "  - SIGTERM: $(echo $survivors | tr '\n' ' ')"
        # Non-root first (cheap), then sudo for root-owned processes.
        # shellcheck disable=SC2086
        kill -TERM $survivors 2>/dev/null || true
        # shellcheck disable=SC2086
        sudo -n kill -TERM $survivors 2>/dev/null || true
        for _ in 1 2 3 4 5 6; do
            sleep 0.5
            local any_alive=0
            for p in $survivors; do
                if kill -0 "$p" 2>/dev/null; then any_alive=1; break; fi
            done
            [ "$any_alive" -eq 0 ] && break
        done
    fi

    # Final hammer. If anything's still alive here it's because it
    # ignored SIGTERM (pynput listeners stuck in a syscall, an L2CAP
    # accept() that close() couldn't wake, etc). SIGKILL is not
    # ignorable — this always succeeds.
    local stubborn
    stubborn=$(for p in $all_pids; do kill -0 "$p" 2>/dev/null && echo "$p"; done)
    if [ -n "$stubborn" ]; then
        echo "  - SIGKILL: $(echo $stubborn | tr '\n' ' ')"
        # shellcheck disable=SC2086
        kill -KILL $stubborn 2>/dev/null || true
        # Cache is warm (we primed sudo before calling this function),
        # so -n is sufficient. No non-`-n` fallback here on purpose:
        # that would pop a second GUI password prompt on Restart.
        # shellcheck disable=SC2086
        sudo -n kill -KILL $stubborn 2>/dev/null || true
    fi

    # Clean up the stale socket path. Root-owned socket from the
    # previous worker needs sudo to unlink. Same "-n only" rule:
    # the credential cache is already primed by the caller.
    if [ -e "$SOCKET_PATH" ]; then
        rm -f "$SOCKET_PATH" 2>/dev/null \
            || sudo -n rm -f "$SOCKET_PATH" 2>/dev/null \
            || true
    fi

    echo "  done."
}

kill_previous_instances

# Nuke any stale socket from a previous run that didn't clean up.
# The worker also unlinks before bind, but we'd rather fail fast
# here with a clear error than get a confusing bind() EADDRINUSE.
rm -f "$SOCKET_PATH"

echo "Starting BT worker (as root, via sudo -n)..."
# -E preserves SUDO_UID/SUDO_GID/DISPLAY/XAUTHORITY/XDG_RUNTIME_DIR
# so (a) the worker can chown the socket back to us and (b) pynput
# can still reach the X server. nohup (not setsid!) is what keeps
# it alive past shell exit — see the big comment above.
# python3 -u forces unbuffered stdout/stderr so our logging_setup
# pipe can't silently swallow a buffer's worth of output on crashes
# or on fast producer / slow reader races.
nohup sudo -n -E python3 -u "$SCRIPT_DIR/worker.py" --socket "$SOCKET_PATH" \
    </dev/null >"$WORKER_BOOT_LOG" 2>&1 &
WORKER_PID=$!
disown "$WORKER_PID" 2>/dev/null || true

# Wait up to ~10s for the worker's socket to appear (it binds right
# after startup, before opening the BT adapter which is the slow
# bit). If we time out or the process has already died, dump its
# bootstrap log so the user sees why.
for i in $(seq 1 100); do
    if [ -S "$SOCKET_PATH" ]; then break; fi
    if ! kill -0 "$WORKER_PID" 2>/dev/null; then break; fi
    sleep 0.1
done
if [ ! -S "$SOCKET_PATH" ]; then
    echo "ERROR: worker never created $SOCKET_PATH"
    if kill -0 "$WORKER_PID" 2>/dev/null; then
        echo "  worker pid=$WORKER_PID is alive but not serving."
    else
        echo "  worker pid=$WORKER_PID died."
    fi
    echo "---- worker bootstrap log ----"
    sed 's/^/  /' "$WORKER_BOOT_LOG"
    echo "---- toothkey.log (last 40 lines) ----"
    tail -n 40 "$TOOTHKEY_LOG" 2>/dev/null | sed 's/^/  /'
    exit 1
fi
echo "  worker pid=$WORKER_PID, socket=$SOCKET_PATH"

echo "Starting tray UI..."
# We deliberately DON'T use setsid here. Two reasons:
#   1. `setsid` without --fork from a pgrp leader forks internally,
#      so $! ends up being the short-lived setsid parent that exits
#      right after the fork — and then our `kill -0 $TRAY_PID`
#      health check one second later falsely reports "tray died"
#      even though the real Qt process is alive and well.
#   2. nohup alone is sufficient to keep the tray alive past shell
#      exit (it sets SIGHUP ignored, which carries across execve),
#      and nohup does NOT fork, so $! really is the Python PID.
TRAY_DIAG_LOG="$SCRIPT_DIR/logs/tray-diag.log"
TRAY_EXIT_LOG="$SCRIPT_DIR/logs/tray-exit.log"
: > "$TRAY_DIAG_LOG"
: > "$TRAY_EXIT_LOG"

# Wrap the tray launch in a subshell that records the exit status
# right as it happens, completely independent of Python's stdio or
# logging_setup. Even if the tray dies during `exec python3`
# (before Python ever runs), the subshell will write "exited with
# <code>" to tray-exit.log, which gives us a definitive signal.
(
    nohup python3 -u "$SCRIPT_DIR/tray.py" --socket "$SOCKET_PATH" \
        </dev/null >"$TRAY_BOOT_LOG" 2>&1
    _rc=$?
    echo "tray subprocess exited rc=$_rc at $(date -Is)" >> "$TRAY_EXIT_LOG"
) &
TRAY_WRAPPER_PID=$!
disown "$TRAY_WRAPPER_PID" 2>/dev/null || true

# Give the tray ~2s to either stabilise or fail. Longer than before
# because some KDE boxes take that long for isSystemTrayAvailable()
# to resolve (SNI watcher registration is async).
sleep 2

# Find the actual python3 tray PID — we set proc title to
# toothkey-tray right at import time, so pgrep finds it whether or
# not the subshell wrapper is still around.
TRAY_PID=$(pgrep -n -x toothkey-tray 2>/dev/null | head -1)
if [ -z "$TRAY_PID" ]; then
    # Fallback: pgrep by cmdline. This catches the case where
    # setproctitle hasn't taken effect yet (e.g. Python still importing).
    TRAY_PID=$(pgrep -n -f 'python3 .*tray\.py --socket' 2>/dev/null | head -1)
fi

if [ -n "$TRAY_PID" ] && kill -0 "$TRAY_PID" 2>/dev/null; then
    echo "Tooth-key tray launched (pid=$TRAY_PID)."
    echo "  logs: $TOOTHKEY_LOG"
    echo "  diag: $TRAY_DIAG_LOG"
    echo "  quit: right-click the tray icon -> Exit"
else
    echo "ERROR: no running toothkey-tray process found after 2s."
    echo "---- tray wrapper (exit status) ----"
    sed 's/^/  /' "$TRAY_EXIT_LOG"
    echo "---- tray bootstrap log (raw stdout/stderr) ----"
    sed 's/^/  /' "$TRAY_BOOT_LOG"
    echo "---- tray-diag.log (synchronous, bypasses logging_setup) ----"
    sed 's/^/  /' "$TRAY_DIAG_LOG"
    echo "---- toothkey.log (last 60 lines) ----"
    tail -n 60 "$TOOTHKEY_LOG" 2>/dev/null | sed 's/^/  /'
    echo "---- environment snapshot (what the tray would have inherited) ----"
    echo "  DISPLAY=${DISPLAY:-}"
    echo "  WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-}"
    echo "  XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-}"
    echo "  XDG_CURRENT_DESKTOP=${XDG_CURRENT_DESKTOP:-}"
    echo "  KDE_FULL_SESSION=${KDE_FULL_SESSION:-}"
    echo "  DBUS_SESSION_BUS_ADDRESS=${DBUS_SESSION_BUS_ADDRESS:-}"
    echo "  USER=${USER:-} HOME=${HOME:-}"
    echo "---- currently-running toothkey processes ----"
    pgrep -fa toothkey 2>/dev/null | sed 's/^/  /' || echo "  (none)"
    echo
    echo "Worker (pid=$WORKER_PID) has been left running so you can"
    echo "inspect / retry. Kill it manually when done:"
    echo "    sudo kill -TERM $WORKER_PID"
    exit 1
fi
