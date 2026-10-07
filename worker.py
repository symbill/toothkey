"""Root-side BT/HID worker process.

Spawned by start.sh under sudo (so the sudo password prompt happens
on the user's actual tty, not in a detached session where sudo -n
has no way to reach a cached timestamp). Wraps the existing
ToothkeyHandler / ToothkeyKeyboardHandler loops (same ones main.py
uses) and exposes them to the tray over a Unix-domain socket.

Why this split exists: see tray.py's module docstring. TL;DR — the Qt
tray has to run as the user so Plasma's systray accepts its SNI
registration; this process keeps the parts that need CAP_NET_RAW.

Server/client: the worker is the SERVER. It creates the UDS, chowns
it to the invoking user (SUDO_UID/SUDO_GID in the env), and blocks
in accept() until the tray connects. Having the privileged process
own the socket means a dead/restarted tray can reconnect without
the worker going away, and it also means sudo doesn't need to be
re-invoked per tray restart.

Protocol (one JSON object per line in both directions):
    worker -> tray:
        {"type":"hello","pid":<int>}
        {"type":"state","connected":bool,"name":str|null,"mac":str|null,
                       "grab":bool,"paused":bool}
        {"type":"shutdown_ack"}
    tray -> worker:
        {"type":"disconnect"}
        {"type":"pause"}
        {"type":"unpause"}
        {"type":"set_grab","on":bool}
        {"type":"shutdown"}

The worker prints diagnostic lines via regular print() as usual —
logging_setup redirects those into logs/toothkey.log alongside the
tray's own log lines (both processes open the file in O_APPEND mode,
so interleaving is safe).
"""

import argparse
import json
import os
import socket
import sys
import threading
import time
import traceback

# ---------------------------------------------------------------------------
# EARLY-BOOT DIAGNOSTICS
#
# Same pattern as tray.py: an unbuffered append-only file that bypasses
# logging_setup entirely, so we can tell what the worker is doing even if
# the logging_setup pipe gets wedged or the reader thread dies. This
# helped diagnose a case where logging_setup's output silently stopped
# mid-initialize even though the BT loop kept running.
# ---------------------------------------------------------------------------
_WORKER_DIAG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'logs', 'worker-diag.log')
try:
    os.makedirs(os.path.dirname(_WORKER_DIAG_PATH), mode=0o775, exist_ok=True)
except Exception:
    pass

# Size cap for this file. It is append-only across every run and
# nothing else rotates it, so without a cap it grows for as long as the
# service keeps restarting — in practice to hundreds of megabytes.
# Deliberately implemented inline rather than reusing
# logging_setup.rotate_if_large: this whole block exists to diagnose
# failures in the imports below it, so it must not depend on them.
_DIAG_MAX_BYTES = 16 * 1024 * 1024
_DIAG_KEEP = 2
_wdiag_written = 0


def _diag_rotate(path):
    """Roll `path` to .1/.2 if it has grown past the cap. Never raises."""
    try:
        if os.path.getsize(path) < _DIAG_MAX_BYTES:
            return False
        oldest = f'{path}.{_DIAG_KEEP}'
        if os.path.exists(oldest):
            os.unlink(oldest)
        for n in range(_DIAG_KEEP - 1, 0, -1):
            src = f'{path}.{n}'
            if os.path.exists(src):
                os.replace(src, f'{path}.{n + 1}')
        os.replace(path, f'{path}.1')
        return True
    except Exception:
        return False

_diag_rotate(_WORKER_DIAG_PATH)
try:
    _worker_diag = open(_WORKER_DIAG_PATH, 'ab', buffering=0)
except Exception:
    _worker_diag = None

def _wdiag(msg: str) -> None:
    """Write one timestamped line to worker-diag.log. Unbuffered, bypasses
    logging_setup. Must not raise."""
    global _worker_diag, _wdiag_written
    if _worker_diag is None:
        return
    try:
        from datetime import datetime as _dt
        ts = _dt.now().astimezone().isoformat(timespec='milliseconds')
        line = f'{ts} [worker-diag pid={os.getpid()}] {msg}\n'.encode('utf-8', 'replace')
        _worker_diag.write(line)
        _wdiag_written += len(line)
        if _wdiag_written >= 1024 * 1024:
            _wdiag_written = 0
            # We hold the file open, so a rename would leave this handle
            # writing to the rotated-away inode. Reopen onto the new file.
            if _diag_rotate(_WORKER_DIAG_PATH):
                try: _worker_diag.close()
                except Exception: pass
                _worker_diag = open(_WORKER_DIAG_PATH, 'ab', buffering=0)
    except Exception:
        pass

_wdiag(f'enter: argv={sys.argv!r} uid={os.getuid()} euid={os.geteuid()}')

from proc_title import set_title
set_title('toothkey-worker')
_wdiag('set_title ok')

import logging_setup
logging_setup.install()
_wdiag('logging_setup.install() ok')

import keyboard_evdev
from bluetooth_handler import ToothkeyHandler
from common import GlobalContext
from keyboard_handler import ToothkeyKeyboardHandler
_wdiag('bt/common/kbd imports ok')


_shutdown = threading.Event()
_out_lock = threading.Lock()
_out_fh = None

# Set to True when the tray sent us an explicit `shutdown` command
# (i.e. user clicked Exit or Restart in the tray menu). Used to
# choose our process exit code: 42 in that case, mirroring the
# tray's exit-code contract. See the comment in the `shutdown`
# branch of _command_reader and the return statement in main().
_user_initiated_shutdown = False


def _send(obj: dict) -> None:
    """Write one NDJSON line to the tray. No-op if not connected yet
    or if the socket has gone away — we treat the tray's departure
    as a signal to keep the BT loop running but stop emitting events.
    """
    global _out_fh
    fh = _out_fh
    if fh is None:
        return
    try:
        with _out_lock:
            fh.write(json.dumps(obj, separators=(',', ':')) + '\n')
            fh.flush()
    except (BrokenPipeError, OSError) as e:
        # Tray disappeared; clear the handle so future sends are no-ops.
        print(f'[worker] ipc send failed, dropping link: {e}')
        _out_fh = None


def _current_state() -> dict:
    paused = bool(ToothkeyHandler.is_paused())
    # When paused-but-disconnected, ToothkeyHandler.client_display_name
    # has been nulled by _drop_client. Fall back to the snapshot taken
    # at pause time so the tray's "Unpause <name>" menu label keeps
    # rendering with a real name instead of just "Unpause".
    name = ToothkeyHandler.client_display_name
    if name is None and paused:
        name = ToothkeyHandler.paused_device_label()
    return {
        'connected': bool(ToothkeyHandler.is_connected()),
        'name': name,
        'mac': ToothkeyHandler.client_mac_address,
        'grab': bool(GlobalContext.grab_mode),
        'paused': paused,
    }


def _state_poller():
    """Emit {"type":"state", ...} whenever the tuple actually changes.

    Polling is fine here — BT connection events are human-scale (multiple
    hundreds of ms minimum), and this keeps ToothkeyHandler free of Qt /
    signal machinery. 300ms feels instantaneous in the tray menu.
    """
    _wdiag('state_poller: started')
    last = None
    loops = 0
    while not _shutdown.is_set():
        try:
            s = _current_state()
        except Exception as e:
            _wdiag(f'state_poller: _current_state raised: {type(e).__name__}: {e}')
            s = {'connected': False, 'name': None, 'mac': None, 'grab': False}
        if s != last:
            _wdiag(f'state_poller: state change: {last} -> {s}')
            last = s
            _send({'type': 'state', **s})
        loops += 1
        # Every ~30s emit a heartbeat so we can tell the thread is alive.
        if loops % 100 == 0:
            _wdiag(f'state_poller: alive, loop #{loops}, last={last}')
        _shutdown.wait(0.3)
    _wdiag('state_poller: exiting')


def _command_reader(rfh):
    """Read NDJSON commands from the tray and dispatch. Returns when
    the socket closes (rfh yields EOF) or when a shutdown command is
    received.
    """
    try:
        for line in rfh:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                print(f'[worker] bad json from tray: {line!r}')
                continue
            t = msg.get('type')
            if t == 'client_hello':
                # Tray handshake: forwards its graphical-session env
                # vars (DISPLAY, WAYLAND_DISPLAY, XAUTHORITY,
                # XDG_RUNTIME_DIR) so this root process can reach the
                # user's clipboard via xclip / wl-paste for Ctrl+V.
                # Keyboard capture needs none of them — it reads
                # /dev/input directly.
                env = msg.get('env') or {}
                applied = []
                for k in ('DISPLAY', 'WAYLAND_DISPLAY',
                          'XAUTHORITY', 'XDG_RUNTIME_DIR',
                          'XDG_SESSION_TYPE'):
                    v = env.get(k)
                    if v:
                        os.environ[k] = str(v)
                        applied.append(f'{k}={v}')
                print(f'[worker] client_hello env: {", ".join(applied) or "(empty)"}')
            elif t == 'disconnect':
                print('[worker] command: disconnect')
                ToothkeyHandler.disconnect_client()
            elif t == 'pause':
                print('[worker] command: pause')
                ToothkeyHandler.pause_client()
            elif t == 'unpause':
                print('[worker] command: unpause')
                ToothkeyHandler.unpause_client()
            elif t == 'set_grab':
                on = bool(msg.get('on'))
                print(f'[worker] command: set_grab({on})')
                reached = ToothkeyKeyboardHandler.set_grab_mode(on)
                if reached != on:
                    print(f'[worker] set_grab({on}) could not be honoured; '
                          f'grab is {reached}')
                # The state poller picks the new value up either way,
                # but say so immediately: a refused grab should flip
                # the tray back without waiting for the next poll.
                _send({'type': 'state', **_current_state()})
            elif t == 'shutdown':
                print('[worker] command: shutdown')
                _send({'type': 'shutdown_ack'})
                _shutdown.set()
                # Mark this as a user-initiated shutdown so the
                # process exits with status 42. Mirrors tray.py's
                # contract: the worker's systemd unit lists 42 in
                # RestartPreventExitStatus so the user clicking
                # Exit doesn't get the worker auto-respawned
                # underneath them. (Restart from the tray menu
                # still works because the tray follows up with an
                # explicit `sudo systemctl restart toothkey-worker`,
                # which ignores RestartPreventExitStatus.)
                global _user_initiated_shutdown
                _user_initiated_shutdown = True
                # Kick both subsystems so wait_for_client and the
                # keyboard reader unblock and the BT main loop drops out.
                ToothkeyKeyboardHandler.shutdown()
                ToothkeyHandler.stop()

                # Hard-exit watchdog. ToothkeyHandler.stop() closes our
                # L2CAP server sockets, but on Linux, close() on an
                # AF_BLUETOOTH SOCK_SEQPACKET listening socket does NOT
                # reliably unblock a concurrent accept() in another
                # thread — the kernel doesn't deliver a wake-up, so the
                # main thread stays parked forever and the process
                # lives on after the tray disappears. Give the normal
                # path 1.5s to tear down cleanly, then hard-exit so
                # `pgrep toothkey` comes up empty the moment the tray
                # goes away.
                def _force_exit():
                    time.sleep(1.5)
                    _wdiag('watchdog: forcing os._exit(42)')
                    print('[worker] forcing exit (main thread stuck in '
                          'accept)')
                    os._exit(42)
                threading.Thread(target=_force_exit, daemon=True,
                                 name='shutdown-watchdog').start()
                return
            else:
                print(f'[worker] unknown command: {msg}')
    except Exception as e:
        print(f'[worker] command reader crashed: {type(e).__name__}: {e}')
        traceback.print_exc()
    finally:
        # The tray is the only thing that can ask for a grab, and the
        # only UI that shows one is held. Once its socket is gone an
        # exclusive keyboard grab would be invisible and unreleasable
        # from the desktop, so drop it here no matter how we left the
        # loop.
        if GlobalContext.grab_mode or ToothkeyKeyboardHandler.is_running():
            print('[worker] tray link closed while grabbed; releasing '
                  'keyboard')
            ToothkeyKeyboardHandler.set_grab_mode(False)


def _serve_tray(conn) -> None:
    """Serve one tray over `conn`, returning when it goes away.

    Sets up the NDJSON streams, says hello, pushes the current state so
    a freshly connected tray renders correctly without waiting for
    something to change, then blocks in _command_reader until EOF or a
    shutdown command. Always tears the streams down on the way out, so
    the next tray starts from a clean slate.
    """
    global _out_fh
    conn.settimeout(None)
    print('[worker] tray connected')

    # One file object per direction so the poller (writer) and command
    # reader don't contend on internal buffer state.
    rfh = conn.makefile('r', encoding='utf-8')
    _out_fh = conn.makefile('w', encoding='utf-8')
    wfh = _out_fh

    try:
        _send({'type': 'hello', 'pid': os.getpid()})
        # The state poller only emits on change, and the link state it
        # last saw may be minutes old — without this a tray that
        # reconnects to a long-running worker would sit on its startup
        # "disconnected" icon until the iPhone next did something.
        _send({'type': 'state', **_current_state()})
        _command_reader(rfh)
    finally:
        _out_fh = None
        for fh in (rfh, wfh):
            try:
                fh.close()
            except OSError:
                pass
        try:
            conn.close()
        except OSError:
            pass
        print('[worker] tray link closed')


def _tray_link_loop(server, first_conn) -> None:
    """Accept and serve trays, one at a time, for the life of the worker.

    `first_conn` is the connection main() already accepted, so the
    first tray isn't made to wait for this thread to start.
    """
    conn = first_conn
    while not _shutdown.is_set():
        _serve_tray(conn)
        if _shutdown.is_set():
            break
        conn = None
        # A timeout rather than a blocking accept, so a shutdown that
        # arrives while no tray is connected still ends this thread.
        server.settimeout(1.0)
        while conn is None and not _shutdown.is_set():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            except OSError as e:
                # The listening socket went away (shutdown closed it).
                _wdiag(f'tray-link: accept failed: {e}')
                return
        if conn is None:
            return
        _wdiag('tray-link: accepted a new tray')
    _wdiag('tray-link: exiting')


def _run_connection_session():
    """Block until the current BT client disconnects.

    Capture is deliberately not driven from here. This function must
    return promptly when the peer goes away, so it only ever waits on
    the disconnect event; blocking on a keyboard read would strand us
    past the iPhone's reconnect window, leaving the phone showing
    "connected" while the tray still showed "disconnected".

    The capture lifecycle belongs to the keyboard handler
    itself (see set_grab_mode): it's only alive while grab_mode is
    on, runs in its own daemon thread, and doesn't block our BT
    state machine.
    """
    # Auto-resume grab if it was on from a previous session — matches
    # the behaviour of a real BT keyboard that just happens to be in
    # the same "grabbing input" state across reconnects.
    if GlobalContext.grab_mode and not ToothkeyKeyboardHandler.is_running():
        ToothkeyKeyboardHandler.start_listener()

    ToothkeyHandler.wait_until_disconnected()

    # Drop the grab on our way out: with no peer there is nowhere to
    # forward keystrokes, and holding the user's keyboard exclusively
    # while disconnected would be indistinguishable from a hang.
    # grab_mode survives, so the grab is re-taken when the peer
    # returns (see start_listener).
    ToothkeyKeyboardHandler.stop_listener()


def _bt_main():
    _wdiag('bt_main: entering')
    try:
        _wdiag('bt_main: calling ToothkeyHandler.initialize()')
        ToothkeyHandler.initialize()
        _wdiag('bt_main: initialize() returned OK')
        while ToothkeyKeyboardHandler.active:
            _wdiag('bt_main: calling wait_for_client()')
            if not ToothkeyHandler.wait_for_client():
                _wdiag('bt_main: wait_for_client returned False')
                if not ToothkeyHandler._running:
                    break
                continue
            _wdiag(f'bt_main: wait_for_client connected '
                   f'name={ToothkeyHandler.client_display_name!r} '
                   f'mac={ToothkeyHandler.client_mac_address!r}')
            _run_connection_session()
            _wdiag('bt_main: session ended')
            print('session ended; waiting for reconnect...')
    except Exception as e:
        _wdiag(f'bt_main: crashed: {type(e).__name__}: {e}\n'
               + traceback.format_exc())
        print(f'[worker] BT loop crashed: {type(e).__name__}: {e}')
        traceback.print_exc()
    finally:
        _wdiag('bt_main: finally - calling stop()')
        ToothkeyHandler.stop()
        _shutdown.set()
        _wdiag('bt_main: exiting')


def _setup_server_socket(sock_path: str) -> socket.socket:
    """Bind + listen on sock_path, transferring ownership to the user
    who invoked sudo so a non-root tray can connect.

    Using SUDO_UID/SUDO_GID (preserved by `sudo -E`) instead of
    guessing via stat() on a parent dir keeps this robust to running
    from any cwd — including the .desktop launcher case where cwd
    is the user's $HOME, not the repo.
    """
    # Clean up any stale socket from a previous run that crashed
    # before cleanup_socket() ran. UDS bind() fails with EADDRINUSE
    # if the path already exists, even if nothing is listening.
    try:
        os.unlink(sock_path)
    except FileNotFoundError:
        pass

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(sock_path)
    sock.listen(1)

    # Default socket file mode is umask-dependent, typically 0o755.
    # Lock it down: owner rw only (which, after chown below, is the
    # invoking user; root bypasses DAC so we can still connect too).
    os.chmod(sock_path, 0o600)

    # Socket ownership: we want the invoking user (not root) to own
    # the socket so the unprivileged tray can connect() it. Two
    # sources, in order of preference:
    #
    #   SUDO_UID / SUDO_GID  — set by `sudo -E` (the start.sh flow).
    #
    #   TOOTHKEY_SOCKET_OWNER=UID:GID — set by install.sh-generated
    #     systemd unit file. systemd services are spawned by PID 1,
    #     not sudo, so they have no SUDO_*. The unit file instead
    #     sets this env var explicitly.
    sudo_uid = os.environ.get('SUDO_UID')
    sudo_gid = os.environ.get('SUDO_GID')
    if not (sudo_uid and sudo_gid):
        owner = os.environ.get('TOOTHKEY_SOCKET_OWNER', '')
        if ':' in owner:
            uid_s, gid_s = owner.split(':', 1)
            sudo_uid = uid_s.strip() or None
            sudo_gid = gid_s.strip() or None
    if sudo_uid and sudo_gid:
        try:
            os.chown(sock_path, int(sudo_uid), int(sudo_gid))
        except (ValueError, PermissionError) as e:
            # Fall back to world-readable so the tray can still reach
            # it. Noisy, but better than a silent UX failure.
            print(f'[worker] chown({sock_path}) failed: {e}; '
                  f'relaxing mode to 0o666')
            os.chmod(sock_path, 0o666)
    else:
        # No UID hints at all; probably invoked directly as root for
        # a dev test. Keep the tight mode, caller is responsible.
        print('[worker] no SUDO_UID/SUDO_GID or TOOTHKEY_SOCKET_OWNER; '
              'socket stays root-owned')

    return sock


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--socket', required=True,
                        help='Path of the Unix domain socket to create and '
                             'listen on. Tray connects to this.')
    args = parser.parse_args()

    print(f'[worker] start pid={os.getpid()} euid={os.geteuid()} '
          f'socket={args.socket}')

    # Report up front which keyboard we would capture. Read-only; it
    # takes no grab. Doing this at startup means the log already
    # answers "could it have captured anything?" by the time anyone
    # reports that typing didn't reach the phone.
    try:
        keyboard_evdev.describe_input_devices()
    except Exception as e:
        print(f'[worker] keyboard probe failed: {type(e).__name__}: {e}')

    try:
        server = _setup_server_socket(args.socket)
    except OSError as e:
        print(f'[worker] failed to bind {args.socket}: {e}')
        return 2

    print(f'[worker] listening on {args.socket}; waiting for tray')

    # Accept-timeout behaviour is configurable so the same binary can
    # be used in two deployment modes:
    #
    #   start.sh mode (default, timeout=120s): start.sh spawned us and
    #     will spawn the tray within a second or two. If the tray
    #     never arrives something's wrong — bail out rather than hang
    #     forever on a stale worker.
    #
    #   systemd/installed mode (timeout=0): install.sh starts us at
    #     boot with TOOTHKEY_ACCEPT_TIMEOUT=0. We wait forever for
    #     the user's tray to connect, because "tray" only exists
    #     once someone logs in graphically. With a 2-minute cap we'd
    #     cycle forever pre-login, churning BT state.
    try:
        timeout_s = float(os.environ.get('TOOTHKEY_ACCEPT_TIMEOUT', '120'))
    except ValueError:
        timeout_s = 120.0
    if timeout_s > 0:
        server.settimeout(timeout_s)
        print(f'[worker] will wait up to {timeout_s:.0f}s for tray')
    else:
        server.settimeout(None)  # block indefinitely
        print('[worker] will wait indefinitely for tray (systemd mode)')

    try:
        conn, _ = server.accept()
    except socket.timeout:
        print(f'[worker] no tray connected within {timeout_s:.0f}s; exiting')
        server.close()
        try: os.unlink(args.socket)
        except OSError: pass
        return 3

    # A grab can end without the tray asking: the panic chord, or the
    # last grabbed keyboard being unplugged. Push the new state the
    # moment that happens so the tray icon never shows a grab that
    # isn't held.
    def _on_grab_lost(_state):
        _send({'type': 'state', **_current_state()})
    ToothkeyKeyboardHandler.on_grab_lost = staticmethod(_on_grab_lost)

    _wdiag('main: starting state-poller thread')
    threading.Thread(target=_state_poller, daemon=True,
                     name='state-poller').start()

    # The listening socket stays open for the life of the worker, and
    # one thread accepts trays from it in series.
    #
    # The worker outlives any individual tray: it owns the Bluetooth
    # link, which should survive the tray being restarted, the user
    # logging out and back in, or the tray crashing. If the listening
    # socket were closed after the first accept, the next tray could
    # never connect — the worker would keep the iPhone happily paired
    # while every tray reported "Worker unreachable" until someone
    # restarted the worker by hand.
    _wdiag('main: starting tray-link thread')
    threading.Thread(target=_tray_link_loop, args=(server, conn),
                     daemon=True, name='tray-link').start()

    _wdiag('main: calling _bt_main()')
    _bt_main()
    _wdiag('main: _bt_main() returned')

    _shutdown.set()
    # Closing the listening socket also unblocks the tray-link thread
    # if it is parked in accept(); _serve_tray owns closing whichever
    # tray connection it is holding.
    try: server.close()
    except OSError: pass
    try: os.unlink(args.socket)
    except OSError: pass
    print('[worker] exit')
    # See comment on _user_initiated_shutdown above. If we got
    # here because the tray asked us to stop, exit 42 so systemd
    # leaves us dead (the tray will systemctl-restart us on a
    # subsequent launcher click or menu Restart).
    return 42 if _user_initiated_shutdown else 0


if __name__ == '__main__':
    sys.exit(main())
