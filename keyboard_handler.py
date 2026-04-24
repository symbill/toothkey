"""Keyboard capture + HID report generation.

All user-facing control (grab/ungrab, shutdown) is driven by the tray
menu. This module deliberately has NO keyboard shortcuts of its own —
every key pressed while grab_mode is on is forwarded verbatim to the
connected Bluetooth host. That means there's no key combo the user
could press that would fail to pass through to e.g. their iPhone.

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

import threading

from common import GlobalContext


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
        # grab mode or shut down — control lives in the tray menu. Every
        # key we see is passed straight through when grab_mode is on.
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
        # Boot Keyboard roll-over semantics are "release any key =>
        # flush the non-modifier slots". That matches what real USB
        # keyboards report when the host's HID driver is polling.
        cls.input_key_set.clear()
        cls.input_modifier_key_set.discard(key)

        cls.update_states()

        if GlobalContext.grab_mode:
            GlobalContext.send_data_to_device(bytes(cls.states))

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
