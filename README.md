# Tooth-key

A Linux (Kubuntu / Ubuntu) system-tray app that turns your computer into a
Bluetooth HID keyboard so you can type into an iPhone, iPad, or any other
Bluetooth-enabled host using your real keyboard.

Originally forked from [Bullshitooth](https://github.com/Alkaid-Benetnash/EmuBTHID).
Now keyboard-only, more reliable persisted pairing, and a proper tray UI.

## Features

- **Pairs as a real HID keyboard** (Class of Device = Peripheral/Keyboard,
  Numeric Comparison Secure Simple Pairing). iOS accepts it without fuss.
- **Persistent bonds** — pair once, and subsequent app restarts just
  reconnect. No re-pairing dance.
- **Auto-reconnect** — if the iPhone drops off (out of range, sleep,
  etc.), Tooth-key pages it back automatically when it's reachable again.
- **System-tray UI** — runs quietly in the tray with a tooth icon; a red
  X overlay appears when disconnected, a blue pause overlay appears
  when paused, and the tooth turns green while grab mode is on.
- **Tray menu** with Disconnect / Pause / Unpause / Grab / Ungrab /
  Open log folder / Restart / Exit. Left-click toggles grab while
  connected; otherwise it opens the menu.
- **Pause mode** is "Disconnect, but for real" — like Disconnect, it
  drops the current Bluetooth link, but it _also_ suppresses
  auto-reconnect (both our outgoing pages and incoming iOS-initiated
  reconnects are refused) until you explicitly **Unpause**. Use it
  when you want to stop your iPhone from picking up keystrokes for
  longer than the ~60 s window Disconnect gives you.
- **Grab mode** suppresses keys from the host while forwarding them to
  the Bluetooth peer. A floating tooth appears at the top-right of the
  screen so you always know grab is active — click it to ungrab.
  The grab is a kernel-level `EVIOCGRAB` on the keyboard's
  `/dev/input` node, so while it is held your keystrokes genuinely do
  not reach any local application — not the focused window, not the
  compositor. **Ctrl+Alt+Shift+Esc releases it** from anywhere, which
  is the escape hatch if the tray or the Bluetooth link ever wedges.
- **Ctrl+V pastes the Linux clipboard into the iPhone.** While grab
  mode is on, Ctrl+V is intercepted: instead of forwarding the chord
  to iOS (which doesn't bind Ctrl+V to paste anyway — it expects
  Cmd+V on hardware keyboards), Tooth-key reads the desktop clipboard
  via `xclip` / `wl-paste` and types its contents into the BT peer
  one HID keystroke at a time. The only practical way to ferry text
  out of a Linux app and into an iOS text field over Bluetooth.
- **Clean disconnect on exit** so iOS doesn't hold a stale link.

## Requirements

- Linux with BlueZ 5.x (tested on Kubuntu/Ubuntu 24.04 and 26.04)
- A working Bluetooth controller (BR/EDR — classic Bluetooth)
- Python 3.10+
- X11 or Wayland session. Keyboard capture is display-server
  independent (it reads `/dev/input` directly), so grab works the same
  on both. The tray itself asks Qt for the **xcb** platform even in a
  Wayland session — see [Why the tray runs on
  XWayland](#why-the-tray-runs-on-xwayland).
- `XWayland` present in a Wayland session (it is, by default, on
  Kubuntu/KDE)
- `python3-xlib`, for the one window-manager hint Qt can't set (keeping
  the floating tooth and the toast out of the taskbar)
- `xclip` and/or `wl-clipboard` on the user's `$PATH` (auto-installed;
  required for the Ctrl+V "paste desktop clipboard into iPhone"
  feature — without them Ctrl+V silently does nothing)

All Python / apt dependencies are installed automatically the first
time you run either `./install.sh` or `./start.sh`. Both delegate to
the same `install_dependencies` / `init_bluez` routines in `start.sh`,
so there's one source of truth for the package list and the BlueZ
configuration.

## Install

The recommended way to use Tooth-key is to install it as a pair of
systemd services — a system service for the BlueZ worker (starts at
boot, runs as root, no password prompt) and a user service for the
tray UI (starts when you log in).

```bash
git clone <this-repo> ~/toothkey
cd ~/toothkey
./install.sh
```

`install.sh`:

- runs `./start.sh --prepare-system` to install the apt + Python
  dependencies and configure BlueZ (`main.conf` Class, plugin
  blocklist drop-in). Pass `--no-prepare` if you've already done this
  step manually and want to skip it.
- writes `/etc/systemd/system/toothkey-worker.service`
  (BlueZ/HID worker, runs as root at boot)
- writes `~/.config/systemd/user/toothkey-tray.service`
  (system-tray UI, starts with your graphical session)
- writes `/etc/sudoers.d/toothkey` so your user can
  `systemctl {start,stop,restart} toothkey-worker.service` without a
  password (needed by the tray's Restart menu item)
- drops a `.desktop` entry + SVG icon into `~/.local/share` so
  Tooth-key shows up in your KDE / GNOME / XFCE application menu
  (delegates to `start.sh --install-launcher`)
- enables and starts both services

The repo directory you cloned into becomes the permanent install
location — systemd points `ExecStart=` at `worker.py` / `tray.py`
inside it, so don't delete or move the directory after installing. To
pick up code changes, either re-run `./install.sh` (safe to do
repeatedly) or click Restart in the tray menu.

To undo: `./uninstall.sh`. It removes the two services, the sudoers
drop-in, the application-menu entry, and disables everything
`install.sh` enabled. Your pairings and BlueZ config are left alone.

Useful commands after `./install.sh`:

```bash
systemctl status toothkey-worker          # is the worker up?
systemctl --user status toothkey-tray     # is the tray up?
journalctl -u toothkey-worker -f          # follow worker logs
./uninstall.sh                            # undo the install
```

## Running without installing

If you'd rather not use systemd — e.g. for one-off use, development,
or headless debugging — run it directly via `start.sh`:

```bash
git clone <this-repo> ~/toothkey
cd ~/toothkey
./start.sh
```

On first run, `start.sh` installs apt + Python dependencies, configures
BlueZ, and launches the tray. On subsequent runs it just launches. It
caches sudo credentials up front (needed to spawn the BT worker as
root) and launches both the worker and the tray as detached processes.

If Tooth-key has been installed via `./install.sh`, `./start.sh`
detects the running systemd worker service and exits gracefully rather
than spawning a second one. Use `--cli` to force terminal mode.

The application-menu entry is installed automatically by `./install.sh`.
If you're running `start.sh` directly (without installing) and want
the same menu shortcut, manage it explicitly:

```bash
./start.sh --install-launcher    # drops .desktop + icon into ~/.local/share
./start.sh --uninstall-launcher  # remove it
```

## Usage

1. **Launch**: just log in (installed mode) or run `./start.sh`. A
   tooth icon appears in the system tray with a red X overlay while
   disconnected.
2. **Pair** (first time only): on your iPhone, go to
   Settings → Bluetooth → tap `Tooth-key (<hostname>)`. Confirm the
   numeric code on the phone. The Linux side auto-confirms. After the
   first pair, the bond is remembered on both sides.
3. **Reconnect** (subsequent runs): happens automatically, either from
   the iPhone or from Tooth-key paging the iPhone.
4. **Type**: left-click the tray icon (or use the menu's Grab
   keyboard) to start forwarding keys to the iPhone. The tooth turns
   green, a floating tooth appears top-right, and a toast confirms.
   Left-click again (or click the floating tooth) to ungrab.
5. **Exit**: tray menu → Exit. Cleanly drops the Bluetooth link so the
   iPhone shows "Not Connected" immediately.

### Tray menu

| Item                       | When shown         | Effect |
|---------------------------|--------------------|--------|
| Disconnect _device-name_  | Connected, not paused | Drops the current link; app stays listening for reconnects (auto-reconnect resumes after a ~60 s suppression window). |
| Pause _device-name_       | Connected, not paused | Same teardown as Disconnect, but auto-reconnect stays off (no outbound paging, inbound iOS reconnects are refused) until Unpause. Tray icon flips to blue pause overlay. |
| Unpause _device-name_     | While paused       | Clears the pause flag and pages the saved peer immediately to bring the link back up. Hidden in every other state. |
| Grab keyboard             | Connected + ungrabbed | Start forwarding key events to the Bluetooth peer. |
| Ungrab keyboard           | Connected + grabbed   | Stop forwarding; keys reach local apps again. |
| Open log folder           | Always             | Opens `logs/` in your file manager. |
| Restart                   | Always             | Clean stop + start. Uses `systemctl` in installed mode, re-execs `start.sh` otherwise. (Pause state does NOT survive a Restart — the new process starts un-paused and auto-reconnects.) |
| Exit                      | Always             | Clean disconnect + quit. |

The tray icon reflects link state at a glance:

| Icon                       | Meaning |
|---------------------------|---------|
| Plain tooth               | Connected, grab off — keys go to local apps as usual. |
| Green tooth               | Connected, grab on — keys are being forwarded to the iPhone. |
| Tooth + red X             | Disconnected, auto-reconnect is trying to bring the link back up. |
| Tooth + blue pause bars   | Paused — disconnected on purpose, auto-reconnect suppressed until you click Unpause. |
| Faded tooth               | Shutting down (Exit / Restart in progress). |

### Keyboard shortcuts

Almost none. Grab/ungrab and quitting are controlled exclusively
through the tray menu (and the floating grab indicator), so every
key you press while grab mode is on is forwarded verbatim to the
Bluetooth peer — with one exception:

| Chord (while grabbed) | What Tooth-key does |
|------------------------|----------------------|
| `Ctrl+V`              | **Special case.** Reads your Linux desktop clipboard (`xclip` on X11, `wl-paste` on Wayland) and types its contents into the iPhone as simulated keystrokes. The Ctrl+V chord itself is _not_ forwarded — iOS doesn't bind Ctrl+V to paste anyway (Cmd+V is the iOS hardware-keyboard chord), so the only practical effect is "the text I just copied on Linux now appears in the iOS text field". Non-typeable characters (most non-ASCII / control codes) are skipped. |
| `Ctrl+Alt+Shift+Esc`  | **Panic release.** Drops the keyboard grab immediately. Handled inside the capture loop itself, so it works even if the tray has died or the Bluetooth link is wedged — the one way out that depends on nothing else. Not forwarded to the peer. |

Everything else — including `Ctrl+C`, `Cmd+V`, `Ctrl+Shift+V`,
function keys, etc. — is forwarded as-is. If you want to send a
literal Ctrl+V to the iPhone for some reason, ungrab first, focus
the target app on iOS, then re-grab.

Because capture reads *physical* keycodes rather than characters your
layout has already produced, your Linux keyboard layout does not
matter: the iPhone applies its own, exactly as it would for a real USB
keyboard.

## Log files

All logs live in the `logs/` subdirectory of the repo. They are
appended to across runs and rotated by size: each file is rolled to
`<name>.1`, `<name>.2`, ... once it passes 16 MB, keeping 3
generations of `toothkey.log` and 2 of the diag files. `./debug.sh`
truncates them before capture.

Rotation matters here because these files are in the repo, not
`/var/log`, so nothing else caps them — and a BlueZ adapter that is
scanning generates events for every device in radio range.

| File                       | Written by | Purpose |
|---------------------------|------------|---------|
| `toothkey.log`            | worker + tray | Main combined log. Timestamped stdout/stderr of both processes plus uncaught exceptions. This is the file you want 99% of the time. |
| `worker-diag.log`         | worker     | Low-level, unbuffered diagnostic trace written directly by the worker (bypasses normal logging). Used to catch crashes that happen before `toothkey.log` is even open. |
| `tray-diag.log`           | tray       | Same idea, but for the tray process — captures early-startup failures (missing imports, no DISPLAY, Qt init errors). |
| `worker-bootstrap.log`    | start.sh   | Captures stdout/stderr of the `sudo …python3 worker.py` subprocess during `start.sh`'s launch sequence. Useful when the worker dies before the socket is created. |
| `tray-bootstrap.log`      | start.sh   | Same, for the tray subprocess. |
| `tray-exit.log`           | start.sh   | Timestamped record of the tray subprocess's exit status. Useful to tell "tray never ran" from "tray ran and quit". |
| `bluetoothd.log`          | debug.sh   | `journalctl -u bluetooth` capture. Only present after a `./debug.sh` run. |
| `hci_monitor.log`         | debug.sh   | `btmon` HCI-level capture. Only present after a `./debug.sh` run. |
| `toast-last.png`          | tray       | Diagnostic dump of the most recent toast notification (used to debug Wayland compositing of the grab toast). |

In installed (systemd) mode, the worker's output is also captured in
`journalctl -u toothkey-worker` and the tray's in
`journalctl --user -u toothkey-tray`, in addition to `toothkey.log`.

## Command-line reference

`./start.sh` accepts exactly one flag at a time:

| Flag                   | Purpose |
|------------------------|---------|
| _(none)_               | Launch the tray app; on first run install deps + configure BlueZ. |
| `--cli`                | Launch in terminal mode (no tray). Useful for headless debugging or when installed mode is active but you want a one-off terminal run. |
| `--reset-all`          | Reinstall apt dependencies, reinitialise BlueZ, then launch. |
| `--reset-bluez`        | Reinitialise BlueZ (systemd unit, plugin blocklist, `main.conf` Class) and exit. |
| `--reset-pairings`     | Drop every BlueZ bond on this machine (useful when a pair is stuck); exit. Remember to "Forget This Device" on the iPhone too. |
| `--debug-on`           | Enable `bluetoothd -d` debug logging and restart the service. |
| `--debug-off`          | Disable `bluetoothd` debug. |
| `--install-launcher`   | Install the app into `~/.local/share/applications` + icon. (`./install.sh` does this for you automatically.) |
| `--uninstall-launcher` | Remove the launcher + icon. |
| `-h`, `--help`         | Help. |

## Troubleshooting

- **Pairing fails with "Pairing Unsuccessful" on iPhone**
  - Run `./start.sh --reset-pairings` _and_ "Forget This Device" on the
    iPhone, then retry. Stale bonds on either side are the most common
    cause.
  - If it still fails, run `./debug.sh --isolate`. This captures a
    full `bluetoothd` + HCI trace into `logs/bluetoothd.log`,
    `logs/hci_monitor.log`, and `logs/toothkey.log`.

- **App doesn't appear in system tray on Ubuntu GNOME**
  - GNOME hides legacy tray icons. Install and enable the
    [AppIndicator extension](https://extensions.gnome.org/extension/615/appindicator-support/);
    log out and back in.

- **Red X stays on the icon forever**
  - The iPhone hasn't reconnected. Tap `Tooth-key (…)` in iOS
    Settings → Bluetooth. If nothing happens, toggle Bluetooth off/on
    on the iPhone.

- **Grab doesn't capture my keys**
  - Capture needs root, because `/dev/input/event*` is `root:input`
    mode 0660. In installed mode the worker runs as root already; if
    you launched by hand, check that the worker is not running as your
    user.
  - The worker logs exactly which devices it would capture, every
    start. Look for `[kbd] probe:` in `logs/toothkey.log` — one line
    per input device, with `WILL GRAB` on the ones it picked and a
    reason on the ones it skipped. If nothing says `WILL GRAB`, that
    is the problem, and the reasons say why.
  - Prove it independently of Bluetooth:

    ```bash
    sudo python3 tools/kbd_selftest.py --grab
    ```

    This takes the same exclusive grab and prints the HID report for
    every key you press. If keys show up there, capture works and
    anything still wrong is on the Bluetooth side.
  - `EBUSY` on a device means another process already holds an
    exclusive grab on it. The log names the device.
  - The grab is released when the Bluetooth link drops, when the tray
    disconnects, on `Ctrl+Alt+Shift+Esc`, and whenever the worker
    exits for any reason — the kernel drops a grab when the file
    descriptor closes, so a crash cannot leave your keyboard captured.

- **Tray says "waiting for the worker" / "reconnecting to the worker"**
  - That is the tray dialling the worker in the background; it retries
    forever, backing off to once every 5 s, and recovers on its own as
    soon as the worker is there. You do not need to restart anything.
  - If it never clears, the worker really isn't running:
    `systemctl status toothkey-worker` and
    `journalctl -u toothkey-worker -n 50` for the failure reason.
  - Restarting the **tray** alone is always safe. The worker keeps its
    listening socket open for its whole life and accepts whatever tray
    turns up, so the Bluetooth link survives a tray restart, a
    logout/login, or the tray crashing — and the tray re-syncs its
    state on connect.

- **Ctrl+V doesn't paste anything into the iPhone**
  - Check `logs/toothkey.log` for a `[kbd] clipboard read failed: …`
    line. The most common cause is `xclip` / `wl-paste` not being on
    `$PATH`; install with `sudo apt install xclip wl-clipboard` and
    grab again. Tooth-key tries `wl-paste` first when
    `WAYLAND_DISPLAY` is set, then `xclip`, so installing both is the
    safest option for mixed X11/Wayland setups.
  - The clipboard read happens inside the (root) worker process,
    using the `DISPLAY` / `WAYLAND_DISPLAY` / `XAUTHORITY` env vars
    the tray forwarded over the IPC handshake. If the tray hasn't
    connected yet (e.g. you triggered Ctrl+V immediately on launch
    while the icon is still red-X) the worker logs
    `clipboard read: no DISPLAY or WAYLAND_DISPLAY set` and bails.
  - Non-ASCII characters (emoji, accented letters, CJK, etc.) aren't
    typeable on a US-layout HID keyboard and are skipped with a
    `paste: skipped N unmappable char(s)` log line. Plain ASCII
    pastes verbatim.

## How it works (briefly)

- Registers a **BlueZ HID profile** (UUID `0x1124`) over D-Bus, with the
  kernel handling L2CAP channels 0x0011 (control) and 0x0013 (interrupt).
- Serves the HID **SDP record** (`hid_sdp_record.xml`) describing a
  standard boot-protocol keyboard.
- Locks the adapter's **Class of Device** to `0x002540`
  (Peripheral + Keyboard) and blocks the BlueZ plugins (`input`,
  `hostname`, `a2dp`, …) that would otherwise pollute the adapter's
  advertised service classes and make iOS refuse HID pairing.
- Implements a D-Bus **pairing agent** with `DisplayYesNo` capability
  so SSP resolves to Numeric Comparison, which iOS requires for HID.
- **Initiates pairing from our side** via `Device1.Pair()` the instant
  the iPhone's ACL comes up — iOS waits for the peripheral to kick
  off authentication on classic HID, without which it silently times
  out.
- **Auto-reconnects** via `Device1.Connect()` whenever a known-bonded
  peer goes away, on a slow exponential backoff so it's ready the
  moment the iPhone is reachable again.
- **Cleanly disconnects** via `Device1.Disconnect()` on shutdown so
  subsequent launches reconnect without waiting for iOS's 40-second
  supervision timeout.
- **Captures the keyboard through evdev**, not the display server:
  the root worker opens the keyboard's `/dev/input/event*` node and
  takes an exclusive `EVIOCGRAB`. Linux keycodes map straight onto HID
  usage IDs, which is exactly what a boot-protocol report carries, so
  there is no layout round-trip. See `keyboard_evdev.py` for the full
  rationale.
- **Ignores every peer that isn't a classic HID host.** A scanning
  adapter reports every LE advertiser in range; those use rotating
  random addresses and can't host a classic HID session, so they are
  neither paired with nor logged at full volume. Pairing attempts are
  also rate-limited per peer.

### Why the tray runs on XWayland

The tray asks Qt for the `xcb` platform even inside a Wayland session.
The floating grab indicator has three requirements that Wayland's
xdg-shell deliberately does not give a client: placing its own window
at a chosen position, keeping it above other windows, and keeping it
out of the taskbar. Under Wayland, `move()` is silently ignored, a
parentless `Qt.Tool` becomes an ordinary toplevel (so it gets a
taskbar button), and there is no protocol for "keep above" — so the
indicator ends up wherever the compositor feels like putting it,
listed in the taskbar, and dropping behind whatever you click next.

Under xcb, Qt maps it as `_NET_WM_WINDOW_TYPE_UTILITY` with
`_NET_WM_STATE_ABOVE`, and KWin honours the position. Nothing else in
the tray needs Wayland — the tray icon is the StatusNotifierItem D-Bus
protocol, which is display-server agnostic — and keyboard capture
never touches the display server at all.

Keeping those two windows out of the taskbar takes one more step, and
it is not the window type. On KWin 6, `skipTaskbar` is false for both
`_NET_WM_WINDOW_TYPE_UTILITY` and `_NET_WM_WINDOW_TYPE_NOTIFICATION`,
so a utility window and a notification window each get a taskbar
button. Qt has no API for the state that actually decides it, so
`x11_window_hints.py` sets `_NET_WM_STATE_SKIP_TASKBAR`,
`_NET_WM_STATE_SKIP_PAGER` and `_KDE_NET_WM_STATE_SKIP_SWITCHER`
directly, keeping the windows out of the taskbar, the pager and
Alt+Tab.

Making them override-redirect instead (`Qt.ToolTip`, or
`Qt.X11BypassWindowManagerHint`) would also hide them from the
taskbar, by taking them out of the window manager's hands entirely.
That is the wrong trade: with the bypass hint KWin stops delivering
mouse input, which breaks clicking the tooth to ungrab. Both windows
stay managed.

Override with `TOOTHKEY_QT_PLATFORM=wayland` if you want the native
platform anyway; the tray logs which one it got, and warns when
placement won't work.

## Tests

```bash
python3 tests/run_all.py                # everything (127 tests)
```

| Suite                       | Covers |
|-----------------------------|--------|
| `test_keyboard_evdev.py`    | HID report assembly, modifier bitmask, 6-key rollover, chord handling, which input devices qualify, and that the keycode table is a true inverse of the kernel's. |
| `test_grab_lifecycle.py`    | That every exit path releases the grab — including a crashing reader — that an all-keys-up report goes out, and that a lost grab is never reported as held. |
| `test_grab_policy.py`       | That a grab which could not be taken is reported as *off*, that the grab stays armed across a Bluetooth drop without being held, and that a panic release reaches the tray. |
| `test_pairing_scope.py`     | Which peers get a `Pair()` call, per-peer rate limiting, and log throttling. |
| `test_x11_hints.py`         | That the taskbar-hint helper refuses bad input and degrades quietly when python-xlib or the X server is missing, rather than breaking the toast path. |
| `test_tray_link.py`         | That the worker keeps accepting trays — serving a second one after the first disconnects, never closing its listening socket, and pushing state to a reconnecting tray. |
| `test_hid_reconnect.py`     | The peripheral-initiated HID retry: that a near-miss is retried, that the attempt cap and time budget hold, and that it stops when the ACL goes or a session is adopted. |

None of them needs root, Bluetooth, or a display. The one thing they
can't cover is `EVIOCGRAB` against real hardware — `tools/kbd_selftest.py`
does that:

```bash
sudo python3 tools/kbd_selftest.py           # probe devices only
sudo python3 tools/kbd_selftest.py --grab    # grab 10s, decode keystrokes
```
