"""Keyboard capture + HID report generation.

All user-facing control (grab/ungrab, shutdown) is driven by the tray
menu. With TWO exceptions every key pressed while grab_mode is on is
forwarded verbatim to the connected Bluetooth host:

    Ctrl+V              pastes the Linux desktop clipboard into the
                        peer as synthesised keystrokes, because iOS
                        doesn't bind Ctrl+V to paste anyway (it expects
                        Cmd+V on hardware keyboards) and there is
                        otherwise no way to ferry text from a Linux app
                        into an iPhone over a BT-HID link. See
                        `_do_clipboard_paste`.
    Ctrl+Alt+Shift+Esc  releases the keyboard grab. Capture is a kernel
                        EVIOCGRAB (see keyboard_evdev), so unlike the
                        old X11 grab it cannot be escaped by switching
                        windows or killing the compositor — this chord
                        is the escape hatch that works even if the tray
                        or the Bluetooth link is wedged.

Capture lives in keyboard_evdev.KeyboardGrabber, which reads
/dev/input/event* directly. This module is the policy layer around it:
when to hold a grab, what the two consumed chords do, and how to type
out a clipboard paste.

Lifecycle:
    - `set_grab_mode(on)` takes or releases the grab. It returns the
      state actually reached, so a grab that could not be taken is
      reported as off rather than silently pretending.
    - `stop_listener()` drops the grab but remembers grab_mode, for
      when the Bluetooth link blips; `start_listener()` re-takes it
      when the peer comes back.
    - `shutdown()` flips `active=False` and releases the grab so the
      worker's main loop exits.

Why not pynput:
    pynput loads its X11 backend whenever DISPLAY is set, and in a
    Wayland session DISPLAY is XWayland, which only sees key events
    while an X11 window holds focus. On Plasma 6 almost every window
    is a native Wayland client, so such a listener observes nothing
    and `suppress=True` cannot withhold input from the compositor.
    keyboard_evdev's module docstring has the full rationale.
"""

import os
import string
import subprocess
import threading
import time

import keyboard_evdev
from common import GlobalContext


# US-QWERTY printable characters that require Shift to produce. Used
# by the clipboard-paste synth path to decide whether to set the Shift
# bit in the modifier byte before each per-character report. Keep this
# in sync with `keyboard_hid_usage_id_map.json` — every shifted symbol
# in the JSON map must have its base character listed here.
_SHIFTED_PRINTABLE_CHARS = set('!@#$%^&*()_+{}|:"<>?~') | set(string.ascii_uppercase)

# HID modifier-byte bit for left-shift, per the boot-keyboard spec.
# Same bit as keyboard_evdev.KEYCODE_TO_MOD_BIT[KEY_LEFTSHIFT]; the
# clipboard-paste path needs it as a literal because it builds reports
# character-by-character rather than from held physical keys.
_HID_MOD_SHIFT_L = 1 << 1

# Minimum gap between back-to-back HID reports during clipboard paste.
# iOS's HID input driver de-duplicates reports that arrive within the
# same Bluetooth transmission slot (~1.25 ms), so two distinct chars
# sent too quickly merge into one keypress on the phone. Empirically,
# 4 ms between EVERY edge (press AND release) is the floor where every
# char in a 1k-char paste lands reliably; anything tighter loses chars
# unpredictably mid-string. 4 ms × 2 edges/char ≈ 125 chars/sec,
# which feels instant for typical password / URL pastes.
_PASTE_INTERCHAR_DELAY_S = 0.004


def _read_desktop_clipboard():
    """Read text from the user's desktop clipboard via xclip / wl-paste.

    The worker process inherits DISPLAY / WAYLAND_DISPLAY / XAUTHORITY
    from the tray's `client_hello` handshake, which is what lets a root
    process reach the logged-in user's clipboard. Capture itself no
    longer needs any of them (see keyboard_evdev), but xclip/wl-paste
    still do. Returns the decoded clipboard text, or None if both
    helpers are unavailable / errored / empty.
    """
    # Wayland sessions keep the Wayland clipboard separate from the
    # XWayland (X11) clipboard, and the user's "real" copy lives in
    # the Wayland one — so try wl-paste first when WAYLAND_DISPLAY
    # is set, falling back to xclip if it's missing or returns empty.
    candidates = []
    if os.environ.get('WAYLAND_DISPLAY'):
        candidates.append(['wl-paste', '--no-newline'])
    if os.environ.get('DISPLAY'):
        candidates.append(['xclip', '-selection', 'clipboard', '-out'])

    if not candidates:
        print('[kbd] clipboard read: no DISPLAY or WAYLAND_DISPLAY set; '
              'tray handshake may not have happened yet')
        return None

    last_err = None
    for cmd in candidates:
        try:
            r = subprocess.run(
                cmd, capture_output=True, timeout=2.0)
        except FileNotFoundError:
            last_err = (f'{cmd[0]} not installed; '
                        'apt install xclip wl-clipboard')
            continue
        except subprocess.TimeoutExpired:
            last_err = f'{cmd[0]} timed out after 2s'
            continue
        except Exception as e:
            last_err = f'{cmd[0]}: {type(e).__name__}: {e}'
            continue

        if r.returncode != 0:
            # xclip prints "Error: target STRING not available" to
            # stderr when the clipboard is empty or holds non-text;
            # surface it but keep trying further candidates.
            err = r.stderr.decode('utf-8', errors='replace').strip()
            last_err = f'{cmd[0]} rc={r.returncode}: {err or "(no stderr)"}'
            continue

        try:
            text = r.stdout.decode('utf-8')
        except UnicodeDecodeError:
            text = r.stdout.decode('latin-1', errors='replace')
        if text:
            return text
        last_err = f'{cmd[0]}: clipboard empty'

    if last_err:
        print(f'[kbd] clipboard read failed: {last_err}')
    return None


def _char_to_hid(c: str):
    """Map a single character to (hid_usage_id, needs_shift), or None
    if the character can't be typed on a US-layout HID keyboard.

    Newlines map to Enter (HID 40) and tabs map to Tab (HID 43); other
    control characters and any non-ASCII / unmapped Unicode codepoint
    are reported as untypeable (caller logs + skips). This is the
    same general philosophy as a USB keyboard: only chars that have a
    physical-key analogue make it through.
    """
    # Enter / line breaks. Treat \r the same as \n so CRLF clipboards
    # (common when the source app is a Windows tool over RDP, or any
    # text copied out of a terminal that retained CR) don't double-tap.
    if c in ('\n', '\r'):
        return (40, False)
    if c == '\t':
        return (43, False)
    if c == ' ':
        return (44, False)

    needs_shift = c in _SHIFTED_PRINTABLE_CHARS
    # The map is keyed by the lowercase character for letters; for
    # symbols both shifted and unshifted forms are present, but using
    # the lowercase variant for the lookup keeps the logic uniform.
    key = c.lower() if c.isalpha() else c
    usage = GlobalContext.convert_key_to_hid_usage_id(key)
    if usage is None:
        return None
    return (usage, needs_shift)


class ToothkeyKeyboardHandler:
    """Owns keyboard capture and the HID reports it produces.

    Capture itself lives in keyboard_evdev.KeyboardGrabber; this class
    is the policy layer the worker drives: when to hold a grab, what to
    do with the two chords we consume instead of forwarding, and how to
    synthesise a clipboard paste.

    The grab is only ever held while grab_mode is on. We deliberately
    don't read the keyboard when we aren't forwarding it.
    """

    # The active KeyboardGrabber, or None when not grabbing.
    _grabber = None
    # Guards _grabber against concurrent set_grab_mode / shutdown calls
    # arriving from the IPC reader thread and the BT state machine.
    _lock = threading.RLock()

    # Set while a clipboard paste is being typed out, so a second
    # Ctrl+V can't start an overlapping paste.
    paste_in_progress = False

    # Tells the worker's main loop whether to keep running.
    active = True

    # Called with the new grab state whenever capture stops for a
    # reason the user didn't ask for (panic chord, every keyboard
    # unplugged). Set by worker.py so the tray's icon and menu stay
    # truthful instead of claiming a grab that is no longer held.
    on_grab_lost = None

    # ----------------------------- lifecycle ----------------------------

    @classmethod
    def set_grab_mode(cls, on: bool) -> bool:
        """Turn capture on or off. Returns the state actually reached.

        Honest about failure: if the grab can't be taken (not root, no
        keyboard, another process holds an exclusive grab) this returns
        False and leaves grab_mode off, so the tray shows "not grabbed"
        rather than a green tooth that forwards nothing. Callers must
        use the return value, not assume the requested state.
        """
        on = bool(on)
        with cls._lock:
            # Already in the requested state, in both senses: the flag
            # agrees AND capture really is (or isn't) running. Checking
            # both matters because they can disagree — grab_mode stays
            # armed across a Bluetooth drop while no grab is held.
            if on and GlobalContext.grab_mode and cls.is_running():
                return True
            if not on and not GlobalContext.grab_mode \
                    and not cls.is_running():
                return False

            if not on:
                cls._stop_grab()
                GlobalContext.grab_mode = False
                return False

            if not GlobalContext.is_peer_connected():
                print('[kbd] refusing to grab: no Bluetooth peer connected')
                GlobalContext.grab_mode = False
                return False

            grabber = keyboard_evdev.KeyboardGrabber(
                on_report=cls._send_report,
                on_chord=cls._on_chord,
                on_lost=cls._on_capture_lost,
                log=print)
            if not grabber.start():
                GlobalContext.grab_mode = False
                return False

            cls._grabber = grabber
            GlobalContext.grab_mode = True
            return True

    @classmethod
    def start_listener(cls) -> bool:
        """Re-take the grab if grab_mode says it should be held.

        Called when a Bluetooth session comes up, so a grab that was on
        before a reconnect resumes by itself — the way a real keyboard
        carries on working after the link blips.
        """
        with cls._lock:
            if not GlobalContext.grab_mode or cls.is_running():
                return cls.is_running()
            GlobalContext.grab_mode = False
            return cls.set_grab_mode(True)

    @classmethod
    def stop_listener(cls) -> None:
        """Release the grab but remember that grab_mode was on.

        Used when the Bluetooth link drops: there is nowhere to forward
        keystrokes to, so the grab must go, but we want it back when
        the peer returns. grab_mode stays True for start_listener.
        """
        with cls._lock:
            cls._stop_grab()

    @classmethod
    def shutdown(cls) -> None:
        """Release the grab and tell the worker's loop to exit."""
        cls.active = False
        with cls._lock:
            cls._stop_grab()
            GlobalContext.grab_mode = False

    @classmethod
    def is_running(cls) -> bool:
        g = cls._grabber
        return g is not None and g.is_running()

    @classmethod
    def _stop_grab(cls) -> None:
        grabber, cls._grabber = cls._grabber, None
        if grabber is not None:
            grabber.stop()

    # ------------------------------- wire -------------------------------

    @classmethod
    def _send_report(cls, report: bytes) -> None:
        """Hand one HID input report to the Bluetooth interrupt channel."""
        GlobalContext.send_data_to_device(report)

    @classmethod
    def _on_chord(cls, name: str) -> None:
        if name == 'paste':
            cls._start_clipboard_paste()
        elif name == 'panic':
            cls._on_panic()

    @classmethod
    def _on_capture_lost(cls) -> None:
        """Capture stopped without anyone asking (keyboards unplugged).

        Clear grab_mode so the tray stops claiming a grab we no longer
        hold, and push the correction out. Runs on its own thread for
        the same reason _on_panic does: it is invoked from the reader
        thread that _stop_grab would try to join.
        """
        def run():
            print('[kbd] capture ended unexpectedly; clearing grab mode')
            with cls._lock:
                cls._stop_grab()
                GlobalContext.grab_mode = False
            cb = cls.on_grab_lost
            if cb is not None:
                try:
                    cb(False)
                except Exception as exc:
                    print(f'[kbd] on_grab_lost callback failed: {exc}')
        threading.Thread(target=run, daemon=True,
                         name='toothkey-kbd-lost').start()

    @classmethod
    def _on_panic(cls) -> None:
        """Emergency release triggered from inside the reader loop.

        Runs on a separate thread because it stops the grabber whose
        own thread invoked us, and stop() joins that thread.
        """
        def run():
            with cls._lock:
                cls._stop_grab()
                GlobalContext.grab_mode = False
            cb = cls.on_grab_lost
            if cb is not None:
                try:
                    cb(False)
                except Exception as exc:
                    print(f'[kbd] on_grab_lost callback failed: {exc}')
        threading.Thread(target=run, daemon=True,
                         name='toothkey-kbd-panic').start()

    # ------------------------------ paste -------------------------------

    @classmethod
    def _start_clipboard_paste(cls) -> None:
        """Begin typing the desktop clipboard into the peer.

        Runs on its own thread with the grabber's forwarding muted, so
        the reader thread keeps draining real key events — it has to,
        or the kernel would queue the user's keystrokes and replay them
        at the peer the moment the paste finished — while none of them
        reach the wire.
        """
        with cls._lock:
            if cls.paste_in_progress:
                return
            grabber = cls._grabber
            if grabber is None:
                return
            cls.paste_in_progress = True
            grabber.suppress_forwarding = True

        threading.Thread(target=cls._do_clipboard_paste, args=(grabber,),
                         daemon=True, name='toothkey-kbd-paste').start()

    @classmethod
    def _do_clipboard_paste(cls, grabber) -> None:
        """Type the user's desktop clipboard into the BT peer.

        Wire sequence per character:
            1. press report  : modifier = (Shift if needed else 0),
                               key[0]   = HID usage id
            2. release report: modifier = 0, all key slots zeroed
        With the inter-edge delay (`_PASTE_INTERCHAR_DELAY_S`) iOS sees
        each char as a discrete keypress.

        A quiescent "no modifiers, no keys" report goes out first so the
        Ctrl the user is still physically holding doesn't combine with
        the first typed character into an unwanted shortcut, and the
        real key state is re-asserted at the end.
        """
        try:
            text = _read_desktop_clipboard()
            if not text:
                print('[kbd] Ctrl+V intercept: clipboard is empty / '
                      'unreadable; nothing to paste')
                return

            print(f'[kbd] Ctrl+V intercept: pasting {len(text)} char(s) '
                  f'from desktop clipboard')

            quiescent = bytes([0xA1, 0x01, 0x00, 0x00,
                               0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
            GlobalContext.send_data_to_device(quiescent)
            time.sleep(_PASTE_INTERCHAR_DELAY_S)

            skipped = 0
            sent = 0
            for ch in text:
                if not GlobalContext.grab_mode:
                    print('[kbd] paste aborted: grab released mid-paste')
                    break
                mapped = _char_to_hid(ch)
                if mapped is None:
                    skipped += 1
                    continue
                usage, needs_shift = mapped

                press = bytearray(quiescent)
                press[2] = _HID_MOD_SHIFT_L if needs_shift else 0x00
                press[4] = usage
                GlobalContext.send_data_to_device(bytes(press))
                time.sleep(_PASTE_INTERCHAR_DELAY_S)

                # Release with the key slots zeroed, so the next press
                # is unambiguously an edge: iOS collapses two identical
                # back-to-back reports into a single keypress.
                GlobalContext.send_data_to_device(quiescent)
                time.sleep(_PASTE_INTERCHAR_DELAY_S)
                sent += 1

            msg = f'[kbd] paste: typed {sent} char(s)'
            if skipped:
                msg += (f', skipped {skipped} unmappable '
                        f'(non-ASCII / control codes)')
            print(msg)
        except Exception as exc:
            print(f'[kbd] paste failed: {type(exc).__name__}: {exc}')
        finally:
            cls.paste_in_progress = False
            grabber.suppress_forwarding = False
            # Re-assert whatever the user is still physically holding so
            # the peer's view matches the real keyboard again.
            grabber.send_current_state()
