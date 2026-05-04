"""Keyboard capture + HID report generation.

All user-facing control (grab/ungrab, shutdown) is driven by the tray
menu. With ONE exception — Ctrl+V — every key pressed while grab_mode
is on is forwarded verbatim to the connected Bluetooth host. Ctrl+V
is special-cased to "paste the Linux desktop clipboard into the BT
peer as simulated keystrokes", because (a) iOS doesn't bind Ctrl+V to
paste anyway (it expects Cmd+V on hardware keyboards) and (b) there
is otherwise no easy way to ferry text from the Linux clipboard into
an iPhone over a BT-HID link. See `_do_clipboard_paste` for the
mechanics.

Lifecycle:
    - `start()` spins up a pynput Listener in the current thread
      (blocking join). Called repeatedly by the worker while a BT
      client is connected.
    - `set_grab_mode(on)` flips the grab flag from outside (tray
      menu). Since pynput bakes `suppress` in at construction, we
      tear down the listener; the outer loop reconstructs it with
      the new flag.
    - `shutdown()` flips `active=False` and stops the listener so
      the worker's main loop exits.

Lazy-pynput rationale:
    pynput opens a connection to the X server at import time (on X11)
    or to the compositor (Wayland). When this module is imported
    pre-login — which happens when the worker is started by systemd
    at boot, before anyone has logged in graphically — that import
    crashes with "Bad display name ''". So we DON'T import pynput at
    module load: we import it inside _ensure_pynput_loaded() which is
    called only right before we actually need to build a Listener,
    and only after the tray has connected and given us its DISPLAY /
    WAYLAND_DISPLAY / XAUTHORITY / XDG_RUNTIME_DIR via the handshake.
"""

import os
import string
import subprocess
import threading
import time

from common import GlobalContext


# US-QWERTY printable characters that require Shift to produce. Used
# by the clipboard-paste synth path to decide whether to set the Shift
# bit in the modifier byte before each per-character report. Keep this
# in sync with `keyboard_hid_usage_id_map.json` — every shifted symbol
# in the JSON map must have its base character listed here.
_SHIFTED_PRINTABLE_CHARS = set('!@#$%^&*()_+{}|:"<>?~') | set(string.ascii_uppercase)

# HID modifier-byte bit for left-shift. Mirrors the value in
# `ToothkeyKeyboardHandler.modifier_key_bitmasks[Key.shift_l]`, but we
# need it here as a literal because the synth path runs without going
# through pynput's Key enum (and may run before pynput is loaded if the
# clipboard read also fails).
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
    from the tray's `client_hello` handshake, so the same env vars
    pynput uses for keyboard grab are also what xclip/wl-paste need
    to reach the user's session. Returns the decoded clipboard text,
    or None if both helpers are unavailable / errored / empty.
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


# Populated on first successful pynput load. `None` until then.
keyboard = None


def _ensure_pynput_loaded():
    """Import pynput.keyboard and populate our lazy class attributes.

    Called from everywhere that needs to reference pynput types.
    Idempotent after first success; re-raises if the import fails
    (e.g. no DISPLAY / no X authority yet).
    """
    global keyboard
    if keyboard is not None:
        return
    from pynput import keyboard as _kb
    keyboard = _kb

    # Bitmask positions per the HID Boot Keyboard spec (modifier byte
    # in report ID 1, usage page 0x07). Each side of each modifier
    # gets its own bit so the host can tell left-shift from right-shift.
    # We populate the class-level dict here, on first use, because
    # keyboard.Key.* only exists after the pynput import above succeeds.
    ToothkeyKeyboardHandler.modifier_key_bitmasks = {
        keyboard.Key.ctrl_l:    1 << 0,
        keyboard.Key.shift_l:   1 << 1,
        keyboard.Key.alt_l:     1 << 2,
        keyboard.Key.cmd_l:     1 << 3,
        keyboard.Key.ctrl_r:    1 << 4,
        keyboard.Key.shift_r:   1 << 5,
        keyboard.Key.alt_r:     1 << 6,
        keyboard.Key.cmd_r:     1 << 7,
    }


class ToothkeyKeyboardHandler:

    # Populated lazily by _ensure_pynput_loaded() on first use.
    # Defined as an empty dict at class scope so attribute-access
    # paths (e.g. `cls.modifier_key_bitmasks`) don't AttributeError
    # before we've loaded pynput; they simply miss the `in` check,
    # which is harmless — the listener isn't running yet either.
    modifier_key_bitmasks = {}

    # Boot Keyboard input report template. [0]=Report Type 0xA1 (DATA),
    # [1]=Report ID 0x01 (keyboard), [2]=modifier bitmask, [3]=reserved,
    # [4..9]=up to six simultaneously-pressed non-modifier usage IDs.
    states = bytearray([
        0xA1,
        0x01,
        0x00,
        0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    ])

    states_size = len(states)
    states_modifier_keys_index = 2
    states_input_key_start_index = 4
    states_input_key_limit = states_size - states_input_key_start_index

    input_key_set = set()
    input_modifier_key_set = set()

    listener = None

    event_handlers = None

    active = True

    # Set True for the duration of a clipboard-paste synth so we don't
    # re-enter _do_clipboard_paste from a nested on_press (X11 key
    # repeat can re-fire on_press for V while we're mid-paste). Also
    # consulted by on_press / on_release as a "ignore the user's real
    # keyboard while we drive the BT peer ourselves" interlock.
    paste_in_progress = False

    # True between the Ctrl+V chord press and the matching V release.
    # X11 auto-repeat will re-fire on_press(V) while V stays down, and
    # without this flag we'd kick off a fresh paste on every repeat.
    # Cleared in on_release(V).
    paste_v_consumed = False

    @classmethod
    def get_event_handlers(cls):
        if cls.event_handlers is not None:
            return cls.event_handlers
        cls.event_handlers = {
            name: method.__func__.__get__(cls, cls)
            for name, method in cls.__dict__.items()
            if type(method) == classmethod and name.startswith('on_')
        }
        return cls.event_handlers

    @classmethod
    def _run_listener_blocking(cls):
        """Construct the pynput Listener with whatever suppress flag
        matches the current grab_mode and block on it until stop()
        is called. Always runs in a dedicated daemon thread so the
        caller isn't tied to pynput's join() — on X11 the backend
        sits in XNextEvent and listener.stop() doesn't wake it up
        reliably if no key event is pending."""
        if cls.listener is not None and cls.listener.is_alive():
            return

        # Lazy pynput import — raises ImportError if the worker has no
        # DISPLAY (e.g. systemd-started, pre-tray-handshake). The caller
        # has wrapped this in a thread, so we log and bail rather than
        # crash the whole worker; the user will see "can't grab yet" in
        # the logs and we'll retry next time they toggle grab.
        try:
            _ensure_pynput_loaded()
        except Exception as e:
            print(f'[kbd] pynput unavailable ({type(e).__name__}: {e}); '
                  f'grab requires a connected tray with DISPLAY')
            return

        event_handlers = cls.get_event_handlers()

        # `suppress=True` on pynput's X11 backend grabs the keyboard so
        # local apps don't also see the keystrokes — essential when the
        # iPhone is the intended target. suppress is locked at construct
        # time, so set_grab_mode() has to stop-and-rebuild to flip it.
        cls.listener = keyboard.Listener(
            **event_handlers, suppress=GlobalContext.grab_mode)
        cls.listener.start()
        cls.listener.join()

    @classmethod
    def start_listener(cls):
        """Spin up the keyboard listener in the background. Returns
        immediately; the listener runs in a daemon thread until
        stop_listener() (or grab_mode flipping off) stops it.

        Only called when grab_mode is True — we intentionally don't
        capture keystrokes at all when we aren't forwarding them.
        """
        if cls.listener is not None and cls.listener.is_alive():
            return
        threading.Thread(
            target=cls._run_listener_blocking, daemon=True,
            name='toothkey-kbd-listener',
        ).start()

    @classmethod
    def stop_listener(cls):
        """Ask the pynput listener to stop. May take a moment to
        actually return on X11 (stop() doesn't unblock XNextEvent
        instantly without a pending event), but the listener is a
        daemon thread so the main BT state machine never waits on it.
        """
        listener = cls.listener
        if listener is not None and listener.is_alive():
            listener.stop()

    @classmethod
    def set_grab_mode(cls, on: bool):
        """Flip grab mode from outside (e.g. tray menu). Starts/stops
        the listener to match: ON spins up a fresh listener with
        suppress=True; OFF tears the listener down completely so we
        aren't capturing keystrokes we don't need.

        No-op if the requested state already matches.
        """
        if bool(on) == bool(GlobalContext.grab_mode):
            return
        GlobalContext.grab_mode = bool(on)
        cls.stop_listener()
        if on:
            cls.start_listener()

    @classmethod
    def shutdown(cls):
        """Tell the worker's keyboard loop to exit cleanly.

        Used by the tray's "Exit" menu: flips `active=False` and kicks
        any running listener.
        """
        cls.active = False
        cls.stop_listener()

    @classmethod
    def on_press(cls, key):
        # We intentionally do NOT look at key combinations to toggle
        # grab mode or shut down — control lives in the tray menu.
        # Every key we see is passed straight through when grab_mode
        # is on, EXCEPT Ctrl+V which we hijack to paste the user's
        # desktop clipboard into the BT peer as simulated keystrokes.
        # See _do_clipboard_paste for the rationale.
        if cls._maybe_intercept_paste(key):
            return

        # Drop real key events while we're synthesising a paste so the
        # user's keystrokes don't interleave with the typed-out
        # clipboard. The listener stays alive (we'd lose Ctrl release
        # tracking otherwise), we just don't forward.
        if cls.paste_in_progress:
            return

        pressed_common_key_count = (
            len(cls.input_key_set) - len(cls.input_modifier_key_set))
        if pressed_common_key_count >= cls.states_input_key_limit:
            return

        cls.input_key_set.add(key)
        if key in cls.modifier_key_bitmasks:
            cls.input_modifier_key_set.add(key)

        cls.update_states()

        if GlobalContext.grab_mode:
            GlobalContext.send_data_to_device(bytes(cls.states))

    @classmethod
    def on_release(cls, key):
        # Clear the V-consumed latch as soon as the user lifts V, so
        # the NEXT distinct Ctrl+V press triggers a fresh paste rather
        # than being suppressed as a stale auto-repeat.
        if cls.paste_v_consumed:
            name = cls.parse_key_name(key)
            if name and name.lower() == 'v':
                cls.paste_v_consumed = False
                # The V press never reached input_key_set (we consumed
                # it in _maybe_intercept_paste), so we don't have a
                # corresponding press report to undo. Drop the release
                # silently — the boot-keyboard rollover below would
                # also no-op, but returning early keeps the wire
                # quiet.
                return

        if cls.paste_in_progress:
            return

        # Boot Keyboard roll-over semantics are "release any key =>
        # flush the non-modifier slots". That matches what real USB
        # keyboards report when the host's HID driver is polling.
        cls.input_key_set.clear()
        cls.input_modifier_key_set.discard(key)

        cls.update_states()

        if GlobalContext.grab_mode:
            GlobalContext.send_data_to_device(bytes(cls.states))

    @classmethod
    def _maybe_intercept_paste(cls, key) -> bool:
        """If `key` is the V of a Ctrl+V chord and we're in grab mode,
        kick off a clipboard-to-keystrokes paste and return True.
        Otherwise return False and let on_press handle the key
        normally.

        Returns True even when the chord is detected but a paste is
        already in flight (X11 auto-repeats Ctrl+V while V stays
        down) — the caller must NOT also forward the key through the
        normal path in that case.
        """
        if not GlobalContext.grab_mode:
            return False
        if keyboard is None:
            # pynput hasn't loaded yet, which means we're not inside a
            # listener callback — `key` is bogus.
            return False

        name = cls.parse_key_name(key)
        if not name or name.lower() != 'v':
            return False

        ctrl_held = (keyboard.Key.ctrl_l in cls.input_modifier_key_set
                     or keyboard.Key.ctrl_r in cls.input_modifier_key_set)
        if not ctrl_held:
            return False

        if cls.paste_in_progress or cls.paste_v_consumed:
            # Either we're mid-paste right now, or this is an X11 key
            # repeat for the V we already consumed. Either way: eat
            # the event so the chord doesn't slip through to the BT
            # peer as a stray Ctrl+V.
            return True

        cls.paste_v_consumed = True
        cls._do_clipboard_paste()
        return True

    @classmethod
    def _do_clipboard_paste(cls):
        """Type the user's desktop clipboard into the BT peer.

        Runs synchronously on the listener thread so the user's
        physical keystrokes can't interleave with the synthesised
        ones (pynput delivers key events one-at-a-time per listener,
        so blocking here naturally blocks further on_press / on_release
        until we return). For a 1k-character paste that's ~8 s of
        unresponsive keyboard, which matches user expectations: while
        the clipboard is being typed out, you don't want your own
        keypresses to go to the iPhone too.

        Wire sequence per character:
            1. press report  : modifier = (Shift if needed else 0),
                               key[0]   = HID usage id
            2. release report: modifier = 0, all key slots zeroed
        With the inter-edge delay (`_PASTE_INTERCHAR_DELAY_S`) iOS
        sees each char as a discrete keypress.

        Before/after the loop:
            - Pre-loop: emit a "no modifiers, no keys" report so the
              Ctrl the user is still physically holding doesn't taint
              the typed chars on the iPhone side.
            - Post-loop: rebuild the report from the still-current
              input_modifier_key_set (Ctrl is most likely still down)
              so subsequent on_release(Ctrl) sees a coherent state.
        """
        cls.paste_in_progress = True
        try:
            text = _read_desktop_clipboard()
            if not text:
                print('[kbd] Ctrl+V intercept: clipboard is empty / '
                      'unreadable; nothing to paste')
                return

            print(f'[kbd] Ctrl+V intercept: pasting {len(text)} char(s) '
                  f'from desktop clipboard')

            # Step 1: tell the iPhone "no modifiers, no keys" so the
            # Ctrl the user is still holding doesn't combine with the
            # first typed char into an unwanted shortcut.
            quiescent = bytearray(cls.states)
            quiescent[cls.states_modifier_keys_index] = 0x00
            for i in range(cls.states_input_key_start_index,
                           cls.states_size):
                quiescent[i] = 0x00
            GlobalContext.send_data_to_device(bytes(quiescent))
            time.sleep(_PASTE_INTERCHAR_DELAY_S)

            # Step 2: type each char as press + release.
            skipped = 0
            for ch in text:
                mapped = _char_to_hid(ch)
                if mapped is None:
                    skipped += 1
                    continue
                usage, needs_shift = mapped

                press = bytearray(quiescent)
                press[cls.states_modifier_keys_index] = (
                    _HID_MOD_SHIFT_L if needs_shift else 0x00)
                press[cls.states_input_key_start_index] = usage
                GlobalContext.send_data_to_device(bytes(press))
                time.sleep(_PASTE_INTERCHAR_DELAY_S)

                # Release: zero modifier byte AND zero key slots, so
                # the next iteration's "press" is unambiguously an
                # edge transition (iOS treats two identical reports
                # back-to-back as a single keypress).
                GlobalContext.send_data_to_device(bytes(quiescent))
                time.sleep(_PASTE_INTERCHAR_DELAY_S)

            if skipped:
                print(f'[kbd] paste: skipped {skipped} unmappable '
                      f'char(s) (non-ASCII / control codes)')

            # Step 3: re-assert whatever modifiers the user is still
            # physically holding. on_release for those modifiers will
            # then see a coherent BT state and can drive it back to
            # zero normally.
            cls.update_states()
            GlobalContext.send_data_to_device(bytes(cls.states))
        finally:
            cls.paste_in_progress = False

    @classmethod
    def parse_key_name(cls, key):
        if key is None:
            return None
        # pynput must be loaded by now because this runs inside the
        # listener thread, which was spun up from _run_listener_blocking
        # after _ensure_pynput_loaded() succeeded.
        if isinstance(key, keyboard.KeyCode):
            return key.char
        if isinstance(key, keyboard.Key):
            return key.name
        return None

    @classmethod
    def update_states(cls):
        cls.states[cls.states_modifier_keys_index] = 0x00
        for key in cls.input_modifier_key_set:
            cls.states[cls.states_modifier_keys_index] |= cls.modifier_key_bitmasks[key]

        index = cls.states_input_key_start_index
        for key in cls.input_key_set:
            if key in cls.input_modifier_key_set:
                continue
            keyname = cls.parse_key_name(key)
            hid_usage_id = GlobalContext.convert_key_to_hid_usage_id(
                keyname.lower() if keyname else keyname)
            if hid_usage_id is None:
                continue
            cls.states[index] = hid_usage_id
            index += 1

        cls.states[index:] = [0] * (cls.states_size - index)
