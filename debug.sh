#!/bin/bash
# debug.sh — one-shot debug-pairing session.
#
# What it does:
#   1. Makes sure bluetoothd is running with `-d` (debug logging) + our plugin
#      overrides. Calls `./start.sh --debug-on` under the hood.
#   2. Clears all Linux-side BT bonds so we start from a clean slate.
#   3. Starts `journalctl -u bluetooth -f` in the background, writing to
#      bluetoothd.log so we capture every HCI/SSP event bluetoothd emits.
#   4. Launches main.py in the foreground (no xterm wrapper) so you see
#      output live AND it goes into toothkey.log.
#   5. On exit (Ctrl-C, crash, or normal quit), stops the journalctl tail
#      and prints a short post-mortem summary of the most useful bits.
#
# Flags:
#   --keep-pairings   skip the Linux-side bond wipe (default is to wipe).
#   --keep-debug-on   don't `--debug-off` bluetoothd on exit (default keeps it on
#                     too, so this is currently a no-op, kept for forward compat).
#   --debug-off-after flip bluetoothd out of debug mode after the run.
#   --isolate         temporarily stop other BT consumers (kded5/bluedevil,
#                     obexd, pipewire's BT module) so nothing else can steal
#                     the default BT agent or inject profile registrations.
#                     Automatically restarts them on cleanup. Use this when
#                     pairing silently fails without any SSP events \u2014 it
#                     guarantees we have exclusive control of bluez.
#
# Typical usage:
#   ./debug.sh              # wipe bonds, run, capture logs, exit
#   ./debug.sh --keep-pairings
#   ./debug.sh --isolate    # stop KDE/obex/pipewire BT stuff during test
#
# After a run, share toothkey.log + bluetoothd.log for analysis.

set -u

# Rename ourselves so `ps aux | grep toothkey` finds this long-lived
# debug session. See start.sh for the rationale behind the /bin/bash
# exec dance.
if [ "${TOOTHKEY_DEBUG_EXECD:-}" != "1" ]; then
    export TOOTHKEY_DEBUG_EXECD=1
    exec -a toothkey-debug /bin/bash "$0" "$@"
fi

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$SCRIPT_DIR"

KEEP_PAIRINGS=0
DEBUG_OFF_AFTER=0
ISOLATE=0
for arg in "$@"; do
    case "$arg" in
        --keep-pairings)   KEEP_PAIRINGS=1 ;;
        --keep-debug-on)   DEBUG_OFF_AFTER=0 ;;
        --debug-off-after) DEBUG_OFF_AFTER=1 ;;
        --isolate)         ISOLATE=1 ;;
        -h|--help)
            sed -n '1,/^set -u/p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) echo "unknown flag: $arg"; exit 1 ;;
    esac
done

# All runtime log files live in ./logs/ (matches logging_setup.LOG_DIR).
# Created up-front so step [0/5]'s truncation doesn't race the dir's
# first-time creation when the Python app hasn't run here yet.
LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"
BLUETOOTHD_LOG="$LOG_DIR/bluetoothd.log"
TOOTHKEY_LOG="$LOG_DIR/toothkey.log"
HCI_LOG="$LOG_DIR/hci_monitor.log"
TAIL_PID=""
HCI_PID=""
SUDO_PID=""
# Tracks which things --isolate stopped, so cleanup can put them back.
ISOLATE_RESTORE=()

isolate_stop() {
    # Best-effort stop of anything that registers a BT agent or profiles
    # against bluez, so our app has exclusive control. We record what we
    # touched in ISOLATE_RESTORE so cleanup can put things back.
    echo
    echo "[isolate] stopping competing bluetooth consumers"

    # 1. KDE's bluedevil agent lives inside kded5. Unload just that module
    #    (preferred) or fall back to killing kded5 entirely.
    if pgrep -x kded5 >/dev/null 2>&1; then
        if qdbus org.kde.kded5 /kded unloadModule bluedevil >/dev/null 2>&1; then
            echo "  - unloaded kded5 module 'bluedevil'"
            ISOLATE_RESTORE+=("kded5_reload_bluedevil")
        else
            echo "  - qdbus unload failed; killing kded5 entirely"
            kded5_cmd=$(ps -o cmd= -p "$(pgrep -x kded5 | head -1)" 2>/dev/null)
            pkill -9 -x kded5 2>/dev/null || true
            if [ -n "$kded5_cmd" ]; then
                ISOLATE_RESTORE+=("kded5_cmd:$kded5_cmd")
            fi
        fi
    else
        echo "  - kded5 not running"
    fi

    # 2. obexd registers OBEX UUIDs. It's D-Bus-activated so it'll respawn
    #    when something pings it; for a short test run we just kill it.
    if pgrep -x obexd >/dev/null 2>&1; then
        sudo pkill -9 -x obexd 2>/dev/null || true
        echo "  - killed obexd"
        ISOLATE_RESTORE+=("obexd_was_running")
    else
        echo "  - obexd not running"
    fi

    # 3. pipewire's libspa-bluez5 module claims HFP/A2DP on the adapter.
    #    Stopping pipewire is heavy-handed but reversible. We only stop the
    #    user-scope units; the desktop brings them back on cleanup.
    if systemctl --user is-active --quiet pipewire.service 2>/dev/null; then
        systemctl --user stop pipewire.service pipewire-pulse.service wireplumber.service 2>/dev/null || true
        echo "  - stopped user pipewire/wireplumber"
        ISOLATE_RESTORE+=("pipewire_was_running")
    else
        echo "  - pipewire user service not active"
    fi

    # Give bluez a moment to drop the unregistered agents/profiles.
    sleep 0.5
}

isolate_restore() {
    local entry cmd
    [ "${#ISOLATE_RESTORE[@]}" -eq 0 ] && return
    echo
    echo "[isolate] restoring competing bluetooth consumers"
    for entry in "${ISOLATE_RESTORE[@]}"; do
        case "$entry" in
            kded5_reload_bluedevil)
                qdbus org.kde.kded5 /kded loadModule bluedevil >/dev/null 2>&1 \
                    && echo "  + reloaded kded5 module 'bluedevil'" \
                    || echo "  + could not reload bluedevil (will come back next kded5 start)"
                ;;
            kded5_cmd:*)
                cmd="${entry#kded5_cmd:}"
                nohup $cmd >/dev/null 2>&1 &
                disown 2>/dev/null || true
                echo "  + restarted kded5"
                ;;
            obexd_was_running)
                # obexd is D-Bus-activated; it'll come back on demand. No-op.
                echo "  + obexd will respawn on demand"
                ;;
            pipewire_was_running)
                systemctl --user start pipewire.service pipewire-pulse.service wireplumber.service 2>/dev/null || true
                echo "  + restarted user pipewire/wireplumber"
                ;;
        esac
    done
}

cleanup() {
    local rc=$?
    echo
    echo "===== debug.sh cleanup ====="
    if [ -n "$TAIL_PID" ] && kill -0 "$TAIL_PID" 2>/dev/null; then
        echo "  stopping journalctl tail (pid=$TAIL_PID)"
        sudo kill "$TAIL_PID" 2>/dev/null || true
        wait "$TAIL_PID" 2>/dev/null || true
    fi
    if [ -n "${HCI_PID:-}" ] && sudo kill -0 "$HCI_PID" 2>/dev/null; then
        echo "  stopping hci_monitor python (pid=$HCI_PID)"
        # Signal the python process directly so its handler flushes the log.
        sudo kill -TERM "$HCI_PID" 2>/dev/null || true
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            sudo kill -0 "$HCI_PID" 2>/dev/null || break
            sleep 0.1
        done
        sudo kill -9 "$HCI_PID" 2>/dev/null || true
    fi
    if [ -n "${SUDO_PID:-}" ] && kill -0 "$SUDO_PID" 2>/dev/null; then
        # Reap the sudo wrapper if it's still hanging around.
        sudo kill -TERM "$SUDO_PID" 2>/dev/null || true
        wait "$SUDO_PID" 2>/dev/null || true
    fi
    if [ "$ISOLATE" = "1" ]; then
        isolate_restore
    fi
    if [ "$DEBUG_OFF_AFTER" = "1" ]; then
        echo "  disabling bluetoothd debug"
        "$SCRIPT_DIR/start.sh" --debug-off >/dev/null
    else
        echo "  leaving bluetoothd in debug mode (re-run debug.sh fast-path)"
    fi
    echo
    echo "===== post-mortem: interesting lines from bluetoothd.log ====="
    if [ -s "$BLUETOOTHD_LOG" ]; then
        # Grep for the handful of events that matter most for a pairing
        # failure diagnosis. Keep it short so we don't drown ourselves.
        grep -iE \
            'disconnect(ion)? complete|reason=|link[_ ]?key|io[_ ]?cap|authentication|bonding|pair|l2cap.*connect|psm.*0x00?1[13]|encryption|mic failure|access denied|refused|not permitted|authorize' \
            "$BLUETOOTHD_LOG" \
            | tail -60 \
            || echo "  (no matches)"
    else
        echo "  (bluetoothd.log is empty)"
    fi
    echo
    echo "===== post-mortem: last 20 lines of toothkey.log ====="
    tail -20 "$TOOTHKEY_LOG" 2>/dev/null || echo "  (no toothkey.log)"
    echo
    echo "===== post-mortem: HCI events (connection / auth / ssp) ====="
    if [ -s "$HCI_LOG" ]; then
        grep -iE \
            'connection complete|connection request|disconnection|authentication|link key|io capability|user confirm|user passkey|simple pairing complete|encryption change|le connection|le ltk' \
            "$HCI_LOG" \
            | tail -40 \
            || echo "  (no matches)"
    else
        echo "  (no hci_monitor.log)"
    fi
    echo
    echo "Logs saved:"
    echo "  $BLUETOOTHD_LOG"
    echo "  $TOOTHKEY_LOG"
    echo "  $HCI_LOG"
    exit "$rc"
}
trap cleanup EXIT INT TERM

echo "===== debug.sh: preparing pairing session ====="

# 0. Clear both log files so the capture only contains this run. Truncate
#    (not rm) because logging_setup opens toothkey.log in append mode at
#    process start — truncating a file the app will open preserves the
#    inode and still yields an empty file.
echo
echo "[0/5] clearing log files"
: > "$BLUETOOTHD_LOG"
: > "$TOOTHKEY_LOG"
: > "$HCI_LOG"

# 1. Make sure bluetoothd is running with -d (our drop-in manages this).
#    start.sh --debug-on is idempotent; always run it so stray drop-ins get
#    cleaned and the daemon is guaranteed in the right state.
echo
echo "[1/5] enabling bluetoothd debug + validating plugin overrides"
"$SCRIPT_DIR/start.sh" --debug-on

# 2. Wipe Linux-side bonds unless told not to.
if [ "$KEEP_PAIRINGS" = "0" ]; then
    echo
    echo "[2/5] clearing Linux-side BT bonds"
    "$SCRIPT_DIR/start.sh" --reset-pairings
else
    echo
    echo "[2/5] skipping bond wipe (--keep-pairings)"
fi

if [ "$ISOLATE" = "1" ]; then
    isolate_stop
fi

echo
echo "REMINDER: on your iPhone, if \"Tooth-key (*)\" is under \"My Devices\","
echo "          tap (i) -> Forget This Device. Otherwise just toggle Bluetooth"
echo "          off, wait ~3s, toggle it back on to flush the EIR cache."
read -r -p "press Enter when ready to start the capture... "

# 3. Start tailing journalctl into bluetoothd.log.
#    Using sudo + --output=short-iso-precise gives us ISO timestamps that
#    line up with toothkey.log's entries. File was already truncated in
#    step 0.
echo
echo "[3/5] starting journalctl tail -> $BLUETOOTHD_LOG"
sudo journalctl -u bluetooth -f --output=short-iso-precise --since=now \
    >> "$BLUETOOTHD_LOG" 2>&1 &
TAIL_PID=$!
# Give journalctl a moment to attach before we generate events.
sleep 0.3

# 4. Start our own HCI monitor. Uses CAP_NET_RAW via sudo; writes decoded
#    pairing-relevant events to hci_monitor.log. This is the ground-truth
#    view the iPhone actually interacts with \u2014 bluetoothd's logs can lie
#    about what's happening at the controller level, but this cannot.
#
#    Two gotchas we're guarding against:
#      a) `sudo ... &` backgrounds the sudo wrapper, so $! is sudo's PID
#         not python's. When cleanup kills sudo, sudo DOES NOT forward the
#         signal to its child by default, so python is orphaned and the log
#         never gets its final flush. We ask sudo to exec python (no wrapper)
#         by preceding the command with `exec`, which replaces sudo's own
#         process image \u2014 but sudo still fork/execs. Instead, we use
#         `sudo --preserve-status` so exit codes line up, and fish out the
#         actual python PID via pgrep after the start.
#      b) suppressing stderr with 2>/dev/null previously hid fatal errors
#         (EPERM, ENODEV). Now hci_monitor.py writes errors to the log file
#         itself, and we also tee stderr to hci_monitor.stderr.
echo
echo "[4/5] starting hci_monitor -> $HCI_LOG"
HCI_STDERR="$LOG_DIR/hci_monitor.stderr"
: > "$HCI_STDERR"
sudo python3 "$SCRIPT_DIR/hci_monitor.py" "$HCI_LOG" \
    >/dev/null 2>"$HCI_STDERR" &
SUDO_PID=$!
# Wait up to ~2s for hci_monitor.log to grow past zero bytes OR for the
# sudo wrapper to die. Whichever comes first tells us whether it worked.
for i in 1 2 3 4 5 6 7 8 9 10; do
    if [ -s "$HCI_LOG" ]; then break; fi
    if ! kill -0 "$SUDO_PID" 2>/dev/null; then break; fi
    sleep 0.2
done
# Grab the actual python child of sudo (more reliable than $!).
HCI_PID=$(pgrep -P "$SUDO_PID" -f "hci_monitor.py" | head -1)
if [ -z "$HCI_PID" ]; then
    # Maybe sudo already exec'd python (child = SUDO_PID itself)?
    if ps -p "$SUDO_PID" -o comm= 2>/dev/null | grep -q python; then
        HCI_PID="$SUDO_PID"
    fi
fi
if [ ! -s "$HCI_LOG" ]; then
    echo "  WARNING: hci_monitor produced no output. Details:"
    echo "    sudo pid: $SUDO_PID  python pid: ${HCI_PID:-<none>}"
    echo "    --- hci_monitor.stderr ---"
    sed 's/^/    /' "$HCI_STDERR" 2>/dev/null || true
    echo "    --- hci_monitor.log ---"
    sed 's/^/    /' "$HCI_LOG" 2>/dev/null || true
    echo "    --------------------------"
    echo "  Continuing without HCI capture; bluetoothd.log only."
    HCI_PID=""
    SUDO_PID=""
else
    echo "  hci_monitor running (sudo=$SUDO_PID, python=${HCI_PID:-?}), log=$HCI_LOG"
fi

# 5. Launch the app in the foreground. main.py sets up logging_setup so
#    toothkey.log is written as a side effect. When python exits, the trap
#    runs and prints the post-mortem.
echo
echo "[5/5] launching main.py (Ctrl-C to stop)"
echo
sudo python3 "$SCRIPT_DIR/main.py"
