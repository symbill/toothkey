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
- **Ctrl+V pastes the Linux clipboard into the iPhone.** While grab
  mode is on, Ctrl+V is intercepted: instead of forwarding the chord
  to iOS (which doesn't bind Ctrl+V to paste anyway — it expects
  Cmd+V on hardware keyboards), Tooth-key reads the desktop clipboard
  via `xclip` / `wl-paste` and types its contents into the BT peer
  one HID keystroke at a time. The only practical way to ferry text
  out of a Linux app and into an iOS text field over Bluetooth.
- **Clean disconnect on exit** so iOS doesn't hold a stale link.

## Requirements

- Linux with BlueZ 5.x (tested on Kubuntu/Ubuntu 24.04)
- A working Bluetooth controller (BR/EDR — classic Bluetooth)
- Python 3.10+
- X11 or Wayland session (the tray needs a display)
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

Everything else — including `Ctrl+C`, `Cmd+V`, `Ctrl+Shift+V`,
function keys, etc. — is forwarded as-is. If you want to send a
literal Ctrl+V to the iPhone for some reason, ungrab first, focus
the target app on iOS, then re-grab.

## Log files

All logs live in the `logs/` subdirectory of the repo. They are
appended to across runs (not rotated) — delete them manually if they
get large, or run `./debug.sh` which truncates them before capture.

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
  - On X11, `pynput`'s key capture needs access to the display. The
    tray runs `xhost +SI:localuser:root` at startup so the (root)
    worker can open the display. Verify with `xhost` that the
    `localuser:root` entry is present.
  - On pure Wayland sessions some keys may not be capturable; fall
    back to an X11 session.

- **"Worker unreachable: … No such file or directory" (installed mode)**
  - The worker service isn't running. Check
    `systemctl status toothkey-worker` and
    `journalctl -u toothkey-worker -n 50` for the failure reason.

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
