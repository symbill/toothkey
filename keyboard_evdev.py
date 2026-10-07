"""Keyboard capture via Linux evdev, with a kernel-level exclusive grab.

Why evdev and not pynput
------------------------
pynput picks its backend from the environment: with DISPLAY set it
loads `pynput.keyboard._xorg` and talks to the X server. In a Wayland
session DISPLAY points at XWayland, and XWayland only ever receives key
events while an *X11* window holds focus — so a listener there sees
nothing at all when the focused window is a native Wayland client
(which, on Plasma 6, is nearly everything). `suppress=True` has the
same blind spot: it grabs the X keyboard, which cannot withhold input
from the Wayland compositor.

Reading `/dev/input/event*` sidesteps the display server completely.
It behaves identically on Wayland, X11 and the bare console, and
EVIOCGRAB (evdev's `grab()`) is a real exclusive grab: while it is
held the kernel routes that device's events to us and to nobody else,
so keystrokes genuinely stop reaching local apps instead of merely
being swallowed late.

It also avoids a translation step. pynput reports characters the
active layout has already produced, which would then have to be mapped
*back* onto HID usage IDs. evdev reports physical keycodes, and a HID
boot keyboard report wants exactly that — physical usage IDs plus a
modifier bitmask, with the host owning the layout. So KEYCODE_TO_HID
below is a direct relabelling of what the kernel hands us, and there is
no layout round-trip to get wrong: a non-US layout works because the
phone applies its own, exactly as it does for a real USB keyboard.

Requirements
------------
`/dev/input/event*` is root:input 0660, so this only works in the
(root) worker process. Nothing here touches a display, an X authority
file or the tray handshake, so capture is available from the moment the
worker starts — it does not wait on a graphical session.

Safety
------
An exclusive grab can lock the user out of their own machine, so:

  - The kernel drops a grab when the file descriptor closes, which
    includes the process dying for any reason. A crashed or killed
    worker therefore cannot leave the keyboard captured.
  - `stop()` is idempotent and runs from the reader thread's `finally`,
    so any error path inside the loop still releases every device.
  - The caller releases the grab when the Bluetooth link drops and
    when the tray's IPC connection goes away (see worker.py), so a
    grab can't outlive the thing it was forwarding to.
  - PANIC_CHORD (Ctrl+Alt+Shift+Esc) releases the grab from inside the
    reader loop itself. This is the one escape hatch that does not
    depend on the tray, the Bluetooth link or the compositor still
    working, and it is the reason the chord is not forwarded to the
    peer.
"""

import errno
import os
import select
import threading
import time

import evdev
from evdev import ecodes as E


# ---------------------------------------------------------------------------
# Linux keycode -> HID usage ID (HID Usage Page 0x07, "Keyboard/Keypad")
# ---------------------------------------------------------------------------
# A boot-protocol keyboard reports physical keys, so this is a plain
# relabelling of the kernel's keycode onto the USB HID usage for the
# same physical key. Deliberately NOT layout-aware: the host decides
# that KEY_Q means "q" on QWERTY and "a" on AZERTY, exactly as it does
# for a real USB keyboard. Keys with no HID equivalent are absent and
# are dropped on the floor.
KEYCODE_TO_HID = {
    # letters
    E.KEY_A: 4,  E.KEY_B: 5,  E.KEY_C: 6,  E.KEY_D: 7,  E.KEY_E: 8,
    E.KEY_F: 9,  E.KEY_G: 10, E.KEY_H: 11, E.KEY_I: 12, E.KEY_J: 13,
    E.KEY_K: 14, E.KEY_L: 15, E.KEY_M: 16, E.KEY_N: 17, E.KEY_O: 18,
    E.KEY_P: 19, E.KEY_Q: 20, E.KEY_R: 21, E.KEY_S: 22, E.KEY_T: 23,
    E.KEY_U: 24, E.KEY_V: 25, E.KEY_W: 26, E.KEY_X: 27, E.KEY_Y: 28,
    E.KEY_Z: 29,

    # digit row
    E.KEY_1: 30, E.KEY_2: 31, E.KEY_3: 32, E.KEY_4: 33, E.KEY_5: 34,
    E.KEY_6: 35, E.KEY_7: 36, E.KEY_8: 37, E.KEY_9: 38, E.KEY_0: 39,

    # typing controls + punctuation
    E.KEY_ENTER: 40,      E.KEY_ESC: 41,        E.KEY_BACKSPACE: 42,
    E.KEY_TAB: 43,        E.KEY_SPACE: 44,      E.KEY_MINUS: 45,
    E.KEY_EQUAL: 46,      E.KEY_LEFTBRACE: 47,  E.KEY_RIGHTBRACE: 48,
    E.KEY_BACKSLASH: 49,  E.KEY_SEMICOLON: 51,  E.KEY_APOSTROPHE: 52,
    E.KEY_GRAVE: 53,      E.KEY_COMMA: 54,      E.KEY_DOT: 55,
    E.KEY_SLASH: 56,      E.KEY_CAPSLOCK: 57,

    # function row
    E.KEY_F1: 58,  E.KEY_F2: 59,  E.KEY_F3: 60,  E.KEY_F4: 61,
    E.KEY_F5: 62,  E.KEY_F6: 63,  E.KEY_F7: 64,  E.KEY_F8: 65,
    E.KEY_F9: 66,  E.KEY_F10: 67, E.KEY_F11: 68, E.KEY_F12: 69,

    # navigation cluster
    E.KEY_SYSRQ: 70,      E.KEY_SCROLLLOCK: 71, E.KEY_PAUSE: 72,
    E.KEY_INSERT: 73,     E.KEY_HOME: 74,       E.KEY_PAGEUP: 75,
    E.KEY_DELETE: 76,     E.KEY_END: 77,        E.KEY_PAGEDOWN: 78,
    E.KEY_RIGHT: 79,      E.KEY_LEFT: 80,       E.KEY_DOWN: 81,
    E.KEY_UP: 82,

    # keypad
    E.KEY_NUMLOCK: 83,    E.KEY_KPSLASH: 84,    E.KEY_KPASTERISK: 85,
    E.KEY_KPMINUS: 86,    E.KEY_KPPLUS: 87,     E.KEY_KPENTER: 88,
    E.KEY_KP1: 89, E.KEY_KP2: 90, E.KEY_KP3: 91, E.KEY_KP4: 92,
    E.KEY_KP5: 93, E.KEY_KP6: 94, E.KEY_KP7: 95, E.KEY_KP8: 96,
    E.KEY_KP9: 97, E.KEY_KP0: 98, E.KEY_KPDOT: 99,

    # the extra key ISO layouts have next to left-shift, and the
    # context-menu key; both are standard HID usages.
    E.KEY_102ND: 100,     E.KEY_COMPOSE: 101,   E.KEY_KPEQUAL: 103,

    # F13..F24 — rare, but they are contiguous HID usages so there is
    # no reason to drop them.
    E.KEY_F13: 104, E.KEY_F14: 105, E.KEY_F15: 106, E.KEY_F16: 107,
    E.KEY_F17: 108, E.KEY_F18: 109, E.KEY_F19: 110, E.KEY_F20: 111,
    E.KEY_F21: 112, E.KEY_F22: 113, E.KEY_F23: 114, E.KEY_F24: 115,

    # Editing / media usages that live on the keyboard page. Order
    # matches the kernel's own hid_keyboard[] table so this stays a
    # true inverse of what Linux does with an incoming HID keyboard.
    E.KEY_OPEN: 116,  E.KEY_HELP: 117,  E.KEY_PROPS: 118,
    E.KEY_FRONT: 119, E.KEY_STOP: 120,  E.KEY_AGAIN: 121,
    E.KEY_UNDO: 122,  E.KEY_CUT: 123,   E.KEY_COPY: 124,
    E.KEY_PASTE: 125, E.KEY_FIND: 126,  E.KEY_MUTE: 127,
    E.KEY_VOLUMEUP: 128, E.KEY_VOLUMEDOWN: 129,

    # locale keys (JP/KR keyboards)
    E.KEY_RO: 135, E.KEY_KATAKANAHIRAGANA: 136, E.KEY_YEN: 137,
    E.KEY_HENKAN: 138, E.KEY_MUHENKAN: 139, E.KEY_KPJPCOMMA: 140,
    E.KEY_HANGEUL: 144, E.KEY_HANJA: 145, E.KEY_KATAKANA: 146,
    E.KEY_HIRAGANA: 147, E.KEY_ZENKAKUHANKAKU: 148,
}

# Modifier keys live in the report's bitmask byte, not in the six key
# slots. Bit positions are fixed by the HID boot-keyboard spec, one bit
# per physical modifier so the host can tell left from right.
KEYCODE_TO_MOD_BIT = {
    E.KEY_LEFTCTRL:   1 << 0,
    E.KEY_LEFTSHIFT:  1 << 1,
    E.KEY_LEFTALT:    1 << 2,
    E.KEY_LEFTMETA:   1 << 3,
    E.KEY_RIGHTCTRL:  1 << 4,
    E.KEY_RIGHTSHIFT: 1 << 5,
    E.KEY_RIGHTALT:   1 << 6,
    E.KEY_RIGHTMETA:  1 << 7,
}

_CTRL_KEYS = (E.KEY_LEFTCTRL, E.KEY_RIGHTCTRL)
_SHIFT_KEYS = (E.KEY_LEFTSHIFT, E.KEY_RIGHTSHIFT)
_ALT_KEYS = (E.KEY_LEFTALT, E.KEY_RIGHTALT)
_META_KEYS = (E.KEY_LEFTMETA, E.KEY_RIGHTMETA)

# How many non-modifier keys a boot-protocol report can carry.
MAX_KEYS = 6
# Usage 0x01 is ErrorRollOver: what a real boot keyboard puts in every
# slot when more keys are held than the report can describe.
HID_ERROR_ROLLOVER = 0x01

# Keys the device must have before we are willing to grab it, so that
# "keyboard" means "something you can type prose on" rather than any
# device that happens to emit key codes (power buttons, lid switches,
# audio-jack volume keys, and the extra HID interfaces that gaming mice
# and headsets expose all report EV_KEY).
_REQUIRED_KEYS = (E.KEY_A, E.KEY_Z, E.KEY_SPACE, E.KEY_ENTER)
# Having any of these means the device also drives the pointer. Grabbing
# it would take the user's mouse away along with their keyboard.
_POINTER_KEYS = (E.BTN_LEFT, E.BTN_RIGHT, E.BTN_TOUCH, E.BTN_STYLUS)

# Re-scan interval for hotplugged keyboards while a grab is active.
# Cheap (a readdir plus an ioctl per new node) and avoids a pyudev
# dependency for something that only has to feel instant to a human.
_RESCAN_INTERVAL_S = 2.0


def is_text_keyboard(dev) -> bool:
    """True if `dev` looks like a keyboard a person types on.

    Requires the full alphabetic core (A, Z, space, enter) and rejects
    anything that also reports pointer axes or mouse buttons.
    """
    try:
        caps = dev.capabilities()
    except OSError:
        return False

    keys = set(caps.get(E.EV_KEY, ()))
    if not keys:
        return False
    if not all(k in keys for k in _REQUIRED_KEYS):
        return False
    if any(k in keys for k in _POINTER_KEYS):
        return False
    # Relative axes mean a pointing device (mouse, trackball); absolute
    # axes mean a touchpad, touchscreen or tablet. Neither is a keyboard
    # even when it also carries key codes.
    if caps.get(E.EV_REL) or caps.get(E.EV_ABS):
        return False
    return True


def discover_keyboards(log=None) -> list:
    """Open every eligible keyboard under /dev/input.

    Returns a list of open evdev.InputDevice. Nodes we can't open
    (permissions, or a device that vanished mid-scan) are skipped; the
    caller decides whether an empty list is fatal.
    """
    found = []
    try:
        paths = sorted(evdev.list_devices())
    except OSError as exc:
        if log:
            log(f'[kbd] cannot list /dev/input: {exc}')
        return found

    for path in paths:
        try:
            dev = evdev.InputDevice(path)
        except OSError as exc:
            # EACCES is the interesting one: it means we are not root.
            if log and exc.errno == errno.EACCES:
                log(f'[kbd] {path}: permission denied (worker must run as root)')
            continue
        if is_text_keyboard(dev):
            found.append(dev)
        else:
            try:
                dev.close()
            except OSError:
                pass
    return found


def describe_input_devices(log=print) -> int:
    """Log which /dev/input nodes we would and wouldn't grab.

    Read-only: opens each node, reads its capabilities and closes it
    again, without ever taking a grab. Called once at worker startup so
    the log answers "can this machine capture the keyboard at all, and
    which device would it capture" before anyone tries to type —
    permission problems and a misidentified device look identical from
    the outside otherwise.

    Returns the number of devices that qualify.
    """
    try:
        paths = sorted(evdev.list_devices())
    except OSError as exc:
        log(f'[kbd] probe: cannot list /dev/input: {exc}')
        return 0

    if not paths:
        log('[kbd] probe: no /dev/input/event* nodes are readable '
            f'(euid={os.geteuid()}); keyboard capture will not work')
        return 0

    eligible = []
    for path in paths:
        try:
            dev = evdev.InputDevice(path)
        except OSError as exc:
            log(f'[kbd] probe: {path}: {exc.strerror or exc}')
            continue
        try:
            if is_text_keyboard(dev):
                eligible.append(dev.name)
                log(f'[kbd] probe: WILL GRAB {os.path.basename(path)} '
                    f'{dev.name!r}')
            else:
                log(f'[kbd] probe: skip     {os.path.basename(path)} '
                    f'{dev.name!r} — {_ineligible_reason(dev)}')
        finally:
            try:
                dev.close()
            except OSError:
                pass

    if eligible:
        log(f'[kbd] probe: {len(eligible)} keyboard(s) available for '
            f'capture: {", ".join(repr(n) for n in eligible)}')
    else:
        log('[kbd] probe: no text keyboard found — Grab will be refused. '
            'The worker must run as root to read /dev/input.')
    return len(eligible)


def _ineligible_reason(dev) -> str:
    """Short human-readable reason is_text_keyboard rejected a device."""
    try:
        caps = dev.capabilities()
    except OSError as exc:
        return f'cannot read capabilities: {exc}'
    keys = set(caps.get(E.EV_KEY, ()))
    if not keys:
        return 'no key events'
    if caps.get(E.EV_REL):
        return 'has relative axes (pointing device)'
    if caps.get(E.EV_ABS):
        return 'has absolute axes (touchpad/tablet)'
    if any(k in keys for k in _POINTER_KEYS):
        return 'has mouse/stylus buttons'
    missing = [name for name, code in (
        ('A', E.KEY_A), ('Z', E.KEY_Z),
        ('space', E.KEY_SPACE), ('enter', E.KEY_ENTER))
        if code not in keys]
    if missing:
        return f'not a text keyboard (no {", ".join(missing)})'
    return 'ineligible'


class KeyboardGrabber:
    """Exclusively grabs every text keyboard and emits HID reports.

    `on_report(bytes)` is called with a complete 10-byte boot-keyboard
    input report every time the key state changes. `on_chord(name)` is
    called for a chord this class handles itself instead of forwarding
    ('paste' for Ctrl+V, 'panic' for the emergency release).
    `on_lost()` is called if capture stops on its own.

    One instance owns one reader thread. start()/stop() are idempotent
    and safe to call from any thread.
    """

    def __init__(self, on_report, on_chord=None, log=print, on_lost=None):
        self._on_report = on_report
        self._on_chord = on_chord or (lambda name: None)
        # Called if capture ends without anyone asking it to — every
        # grabbed keyboard unplugged, or the reader hitting an error it
        # can't continue past. Without it the caller would go on
        # believing it holds a grab that is gone.
        self._on_lost = on_lost or (lambda: None)
        self._log = log

        self._devices = {}           # fd -> InputDevice
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

        # Physical keys currently held. `_mods` is a set of modifier
        # keycodes; `_keys` is an ordered list of non-modifier keycodes
        # (oldest first) so the six report slots stay stable as keys
        # come and go, the way a real keyboard's do.
        self._mods = set()
        self._keys = []

        # True while we are synthesising keystrokes for a clipboard
        # paste. The reader keeps consuming events (we must not lose
        # track of which keys are physically down) but forwards none of
        # them, so the user's own typing cannot interleave with the
        # text being pasted.
        self.suppress_forwarding = False

    # ----------------------------- lifecycle ----------------------------

    def start(self) -> bool:
        """Grab the keyboards and start the reader thread.

        Returns True if at least one device was grabbed. On failure
        nothing is left grabbed.
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True

            self._stop.clear()
            self._mods.clear()
            del self._keys[:]

            devices = discover_keyboards(log=self._log)
            if not devices:
                self._log('[kbd] no grabbable keyboard found under '
                          '/dev/input — is the worker running as root?')
                return False

            grabbed = []
            for dev in devices:
                if self._grab_one(dev):
                    grabbed.append(dev)
                else:
                    try:
                        dev.close()
                    except OSError:
                        pass

            if not grabbed:
                self._log('[kbd] found keyboards but could not grab any')
                return False

            self._devices = {dev.fd: dev for dev in grabbed}
            names = ', '.join(f'{d.name!r} ({os.path.basename(d.path)})'
                              for d in grabbed)
            self._log(f'[kbd] grabbed {len(grabbed)} keyboard(s): {names}')

            self._thread = threading.Thread(
                target=self._run, daemon=True, name='toothkey-evdev')
            self._thread.start()
            return True

    def stop(self) -> None:
        """Release every grab and stop the reader thread.

        Idempotent. Sends one all-keys-up report on the way out so the
        peer is never left believing a key is still held.

        The join timeout is load-bearing, not politeness. The caller
        holds ToothkeyKeyboardHandler._lock, and the reader thread takes
        that same lock when it starts a clipboard paste — so an ungrab
        racing a Ctrl+V has each side waiting on the other. Giving up
        after 2s breaks that: _release_all() then ungrabs and closes the
        descriptors underneath the reader, which wakes it with EBADF and
        it exits on its own.
        """
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and \
                thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._release_all()

    def is_running(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    # ----------------------------- internals ----------------------------

    def _grab_one(self, dev) -> bool:
        try:
            dev.grab()
            return True
        except OSError as exc:
            # EBUSY means somebody else already holds an exclusive grab
            # on this device. Report it and carry on with the rest:
            # a second keyboard we can't grab shouldn't stop us using
            # the one we can.
            self._log(f'[kbd] cannot grab {dev.name!r} '
                      f'({os.path.basename(dev.path)}): {exc}')
            return False

    def _release_all(self) -> None:
        with self._lock:
            devices = list(self._devices.values())
            self._devices = {}
            self._thread = None

        if devices:
            # Tell the peer everything is up before we stop being able
            # to observe releases, otherwise a key held at ungrab time
            # repeats on the phone forever.
            self._mods.clear()
            del self._keys[:]
            try:
                self._on_report(self._build_report())
            except Exception as exc:
                self._log(f'[kbd] final all-up report failed: {exc}')

        for dev in devices:
            try:
                dev.ungrab()
            except OSError:
                # Already gone (unplugged); closing the fd is enough.
                pass
            try:
                dev.close()
            except OSError:
                pass
        if devices:
            self._log(f'[kbd] released {len(devices)} keyboard grab(s)')

    def _rescan(self) -> None:
        """Pick up keyboards plugged in after the grab started."""
        with self._lock:
            if not self._devices:
                return
            known = {dev.path for dev in self._devices.values()}

        for dev in discover_keyboards(log=None):
            if dev.path in known:
                try:
                    dev.close()
                except OSError:
                    pass
                continue
            if self._grab_one(dev):
                with self._lock:
                    self._devices[dev.fd] = dev
                self._log(f'[kbd] grabbed hotplugged keyboard {dev.name!r} '
                          f'({os.path.basename(dev.path)})')
            else:
                try:
                    dev.close()
                except OSError:
                    pass

    def _drop_device(self, fd) -> None:
        with self._lock:
            dev = self._devices.pop(fd, None)
        if dev is None:
            return
        self._log(f'[kbd] keyboard went away: {dev.name!r} '
                  f'({os.path.basename(dev.path)})')
        try:
            dev.close()
        except OSError:
            pass

    def _run(self) -> None:
        next_rescan = time.monotonic() + _RESCAN_INTERVAL_S
        try:
            while not self._stop.is_set():
                with self._lock:
                    fds = list(self._devices)
                if not fds:
                    self._log('[kbd] every grabbed keyboard disappeared; '
                              'stopping capture')
                    break

                try:
                    ready, _, _ = select.select(fds, [], [], 0.25)
                except OSError as exc:
                    # A device vanished between building `fds` and the
                    # select; EBADF is expected, re-loop and rebuild.
                    if exc.errno in (errno.EBADF, errno.EINTR):
                        continue
                    raise

                for fd in ready:
                    with self._lock:
                        dev = self._devices.get(fd)
                    if dev is None:
                        continue
                    try:
                        for event in dev.read():
                            if event.type == E.EV_KEY:
                                self._on_key_event(event)
                    except OSError as exc:
                        if exc.errno in (errno.ENODEV, errno.EBADF):
                            self._drop_device(fd)
                        else:
                            self._log(f'[kbd] read error on {dev.path}: {exc}')
                            self._drop_device(fd)

                now = time.monotonic()
                if now >= next_rescan:
                    next_rescan = now + _RESCAN_INTERVAL_S
                    self._rescan()
        except Exception as exc:
            import traceback
            self._log(f'[kbd] reader thread crashed: '
                      f'{type(exc).__name__}: {exc}\n{traceback.format_exc()}')
        finally:
            # Whatever happened, do not leave the user's keyboard captured.
            self._release_all()
            # Distinguish "we were asked to stop" from "capture died".
            # stop() and the panic chord both set the event first, so
            # reaching here without it means grab_mode is now a lie and
            # somebody has to be told.
            if not self._stop.is_set():
                self._stop.set()
                try:
                    self._on_lost()
                except Exception as exc:
                    self._log(f'[kbd] on_lost callback failed: {exc}')

    # --------------------------- event handling -------------------------

    def _held(self, keycodes) -> bool:
        return any(k in self._mods for k in keycodes)

    def _on_key_event(self, event) -> None:
        code, value = event.code, event.value

        # value 2 is the kernel's autorepeat. A boot-protocol keyboard
        # reports only edges and lets the host do typematic repeat, so
        # repeats must not go on the wire — forwarding them would make
        # every held key send a fresh press iOS treats as a new keystroke.
        if value == 2:
            return

        pressed = (value == 1)

        if code in KEYCODE_TO_MOD_BIT:
            if pressed:
                self._mods.add(code)
            else:
                self._mods.discard(code)
        else:
            if pressed:
                # The panic release and the clipboard paste are the only
                # two chords this app consumes rather than forwards.
                # Both are checked before the key is recorded, so the
                # peer never sees the triggering key.
                if code == E.KEY_ESC and self._held(_CTRL_KEYS) \
                        and self._held(_ALT_KEYS) and self._held(_SHIFT_KEYS):
                    self._log('[kbd] panic chord (Ctrl+Alt+Shift+Esc) — '
                              'releasing keyboard grab')
                    self._stop.set()
                    self._on_chord('panic')
                    return
                if code == E.KEY_V and self._held(_CTRL_KEYS) \
                        and not self._held(_ALT_KEYS) \
                        and not self._held(_META_KEYS):
                    self._on_chord('paste')
                    return
                if code not in self._keys:
                    self._keys.append(code)
            else:
                try:
                    self._keys.remove(code)
                except ValueError:
                    # Release with no matching press: the key was down
                    # before we grabbed the device, or it was consumed
                    # as part of a chord. Nothing to undo.
                    pass

        if self.suppress_forwarding:
            return
        try:
            self._on_report(self._build_report())
        except Exception as exc:
            self._log(f'[kbd] send failed: {type(exc).__name__}: {exc}')

    def _build_report(self) -> bytes:
        """Assemble a HID boot-keyboard input report from current state.

        Layout: [0] 0xA1 DATA, [1] report id 0x01, [2] modifier bitmask,
        [3] reserved, [4..9] up to six held non-modifier usage IDs.
        """
        mod = 0
        for code in self._mods:
            mod |= KEYCODE_TO_MOD_BIT[code]

        usages = []
        for code in self._keys:
            usage = KEYCODE_TO_HID.get(code)
            if usage is not None:
                usages.append(usage)

        report = bytearray(10)
        report[0] = 0xA1
        report[1] = 0x01
        report[2] = mod

        if len(usages) > MAX_KEYS:
            # More keys held than the report can describe. A real boot
            # keyboard answers with ErrorRollOver in every slot rather
            # than silently reporting an arbitrary six.
            for i in range(MAX_KEYS):
                report[4 + i] = HID_ERROR_ROLLOVER
        else:
            for i, usage in enumerate(usages):
                report[4 + i] = usage
        return bytes(report)

    def send_current_state(self) -> None:
        """Re-emit a report for the current key state.

        Used by the paste path to restore the real modifier state after
        it has finished driving the peer itself.
        """
        try:
            self._on_report(self._build_report())
        except Exception as exc:
            self._log(f'[kbd] state resync failed: {exc}')
