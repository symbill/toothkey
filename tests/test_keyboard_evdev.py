"""Unit tests for the evdev capture layer.

These cover the parts that decide what goes on the wire — report
assembly, chord handling, device eligibility — none of which need
root, a Bluetooth peer or a real keyboard. The one thing they cannot
cover is EVIOCGRAB itself; `tools/kbd_selftest.py` exercises that
against real hardware.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import keyboard_evdev as ke
from evdev import ecodes as E


class FakeEvent:
    """Stands in for an evdev InputEvent."""

    def __init__(self, code, value, type=E.EV_KEY):
        self.code = code
        self.value = value
        self.type = type


class FakeDevice:
    """Stands in for an evdev InputDevice for capability tests."""

    def __init__(self, caps, name='fake', path='/dev/input/eventX'):
        self._caps = caps
        self.name = name
        self.path = path

    def capabilities(self):
        return self._caps


def kbd_caps(extra_keys=(), sections=None):
    """Capability dict for a minimal text keyboard, plus any extras.

    `sections` is keyed by EV_* constants, which are ints, so it has to
    be a dict rather than keyword arguments.
    """
    keys = [E.KEY_A, E.KEY_Z, E.KEY_SPACE, E.KEY_ENTER, *extra_keys]
    caps = {E.EV_KEY: keys}
    caps.update(sections or {})
    return caps


class ReportBuildingTests(unittest.TestCase):
    """A report must be exactly what a HID boot keyboard would send."""

    def setUp(self):
        self.sent = []
        self.chords = []
        self.g = ke.KeyboardGrabber(
            on_report=self.sent.append,
            on_chord=self.chords.append,
            log=lambda m: None)

    def press(self, *codes):
        for c in codes:
            self.g._on_key_event(FakeEvent(c, 1))

    def release(self, *codes):
        for c in codes:
            self.g._on_key_event(FakeEvent(c, 0))

    def test_header_and_length(self):
        self.press(E.KEY_A)
        report = self.sent[-1]
        self.assertEqual(len(report), 10)
        self.assertEqual(report[0], 0xA1, 'byte 0 must be the DATA header')
        self.assertEqual(report[1], 0x01, 'byte 1 must be report id 1')
        self.assertEqual(report[3], 0x00, 'byte 3 is reserved')

    def test_letter_maps_to_usage(self):
        self.press(E.KEY_A)
        self.assertEqual(self.sent[-1][4], 4, 'a is HID usage 4')
        self.press(E.KEY_Z)
        self.assertEqual(self.sent[-1][5], 29, 'z is HID usage 29')

    def test_release_clears_slot(self):
        self.press(E.KEY_A)
        self.release(E.KEY_A)
        self.assertEqual(self.sent[-1][4:], b'\x00' * 6)

    def test_modifier_goes_in_bitmask_not_key_slots(self):
        self.press(E.KEY_LEFTSHIFT)
        report = self.sent[-1]
        self.assertEqual(report[2], 1 << 1)
        self.assertEqual(report[4:], b'\x00' * 6,
                         'a modifier must not occupy a key slot')

    def test_left_and_right_modifiers_are_distinct(self):
        self.press(E.KEY_LEFTCTRL)
        left = self.sent[-1][2]
        self.release(E.KEY_LEFTCTRL)
        self.press(E.KEY_RIGHTCTRL)
        right = self.sent[-1][2]
        self.assertNotEqual(left, right)
        self.assertEqual(left, 1 << 0)
        self.assertEqual(right, 1 << 4)

    def test_shifted_letter_sets_both(self):
        self.press(E.KEY_LEFTSHIFT, E.KEY_A)
        report = self.sent[-1]
        self.assertEqual(report[2], 1 << 1)
        self.assertEqual(report[4], 4)

    def test_six_keys_fit(self):
        codes = [E.KEY_A, E.KEY_B, E.KEY_C, E.KEY_D, E.KEY_E, E.KEY_F]
        self.press(*codes)
        self.assertEqual(list(self.sent[-1][4:]), [4, 5, 6, 7, 8, 9])

    def test_seventh_key_is_error_rollover(self):
        self.press(E.KEY_A, E.KEY_B, E.KEY_C, E.KEY_D, E.KEY_E, E.KEY_F,
                   E.KEY_G)
        self.assertEqual(list(self.sent[-1][4:]), [1] * 6,
                         'over six keys must report ErrorRollOver')

    def test_rollover_recovers_when_a_key_lifts(self):
        self.press(E.KEY_A, E.KEY_B, E.KEY_C, E.KEY_D, E.KEY_E, E.KEY_F,
                   E.KEY_G)
        self.release(E.KEY_G)
        self.assertEqual(list(self.sent[-1][4:]), [4, 5, 6, 7, 8, 9])

    def test_slot_order_is_stable_across_churn(self):
        """Lifting a middle key must not reshuffle the others."""
        self.press(E.KEY_A, E.KEY_B, E.KEY_C)
        self.release(E.KEY_B)
        self.assertEqual(list(self.sent[-1][4:7]), [4, 6, 0])

    def test_autorepeat_is_not_forwarded(self):
        self.press(E.KEY_A)
        before = len(self.sent)
        self.g._on_key_event(FakeEvent(E.KEY_A, 2))
        self.assertEqual(len(self.sent), before,
                         'kernel autorepeat must not reach the wire; '
                         'the host does typematic repeat')

    def test_unmapped_key_is_dropped_not_crashing(self):
        self.press(E.KEY_BRIGHTNESSUP)
        self.assertEqual(self.sent[-1][4:], b'\x00' * 6)

    def test_release_without_press_is_ignored(self):
        self.release(E.KEY_A)
        self.assertEqual(self.sent[-1][4:], b'\x00' * 6)

    def test_suppress_forwarding_mutes_the_wire(self):
        self.g.suppress_forwarding = True
        self.press(E.KEY_A)
        self.assertEqual(self.sent, [],
                         'nothing may be forwarded during a paste')

    def test_suppressed_events_still_track_state(self):
        """State must stay accurate while muted, or the resync lies."""
        self.g.suppress_forwarding = True
        self.press(E.KEY_LEFTCTRL)
        self.g.suppress_forwarding = False
        self.g.send_current_state()
        self.assertEqual(self.sent[-1][2], 1 << 0)


class ChordTests(unittest.TestCase):

    def setUp(self):
        self.sent = []
        self.chords = []
        self.g = ke.KeyboardGrabber(
            on_report=self.sent.append,
            on_chord=self.chords.append,
            log=lambda m: None)

    def press(self, *codes):
        for c in codes:
            self.g._on_key_event(FakeEvent(c, 1))

    def test_ctrl_v_fires_paste_and_is_not_forwarded(self):
        self.press(E.KEY_LEFTCTRL, E.KEY_V)
        self.assertEqual(self.chords, ['paste'])
        self.assertTrue(all(r[4] != 25 for r in self.sent),
                        'the V of Ctrl+V must not reach the peer')

    def test_plain_v_is_forwarded(self):
        self.press(E.KEY_V)
        self.assertEqual(self.chords, [])
        self.assertEqual(self.sent[-1][4], 25)

    def test_ctrl_alt_v_is_forwarded(self):
        """Only bare Ctrl+V is ours; Ctrl+Alt+V belongs to the peer."""
        self.press(E.KEY_LEFTCTRL, E.KEY_LEFTALT, E.KEY_V)
        self.assertEqual(self.chords, [])
        self.assertEqual(self.sent[-1][4], 25)

    def test_right_ctrl_also_triggers_paste(self):
        self.press(E.KEY_RIGHTCTRL, E.KEY_V)
        self.assertEqual(self.chords, ['paste'])

    def test_panic_chord_fires(self):
        self.press(E.KEY_LEFTCTRL, E.KEY_LEFTALT, E.KEY_LEFTSHIFT,
                   E.KEY_ESC)
        self.assertEqual(self.chords, ['panic'])

    def test_panic_chord_stops_the_loop(self):
        self.press(E.KEY_LEFTCTRL, E.KEY_LEFTALT, E.KEY_LEFTSHIFT,
                   E.KEY_ESC)
        self.assertTrue(self.g._stop.is_set())

    def test_panic_esc_is_not_forwarded(self):
        self.press(E.KEY_LEFTCTRL, E.KEY_LEFTALT, E.KEY_LEFTSHIFT,
                   E.KEY_ESC)
        self.assertTrue(all(r[4] != 41 for r in self.sent),
                        'the panic Esc must not reach the peer')

    def test_plain_esc_is_forwarded(self):
        self.press(E.KEY_ESC)
        self.assertEqual(self.chords, [])
        self.assertEqual(self.sent[-1][4], 41)

    def test_partial_panic_chord_forwards_esc(self):
        self.press(E.KEY_LEFTCTRL, E.KEY_LEFTALT, E.KEY_ESC)
        self.assertEqual(self.chords, [])
        self.assertEqual(self.sent[-1][4], 41)

    def test_mixed_sides_satisfy_panic_chord(self):
        self.press(E.KEY_RIGHTCTRL, E.KEY_RIGHTALT, E.KEY_LEFTSHIFT,
                   E.KEY_ESC)
        self.assertEqual(self.chords, ['panic'])


class DeviceEligibilityTests(unittest.TestCase):
    """Grab text keyboards; never grab a pointing device."""

    def test_plain_keyboard_accepted(self):
        self.assertTrue(ke.is_text_keyboard(FakeDevice(kbd_caps())))

    def test_device_with_no_keys_rejected(self):
        self.assertFalse(ke.is_text_keyboard(FakeDevice({})))

    def test_power_button_rejected(self):
        self.assertFalse(ke.is_text_keyboard(
            FakeDevice({E.EV_KEY: [E.KEY_POWER]})))

    def test_volume_only_device_rejected(self):
        """Headsets and monitors expose volume keys; not keyboards."""
        self.assertFalse(ke.is_text_keyboard(FakeDevice(
            {E.EV_KEY: [E.KEY_VOLUMEUP, E.KEY_VOLUMEDOWN, E.KEY_MUTE]})))

    def test_mouse_with_key_codes_rejected(self):
        """A gaming mouse's keyboard interface must never be grabbed."""
        caps = kbd_caps(extra_keys=(E.BTN_LEFT, E.BTN_RIGHT),
                        sections={E.EV_REL: [E.REL_X, E.REL_Y]})
        self.assertFalse(ke.is_text_keyboard(FakeDevice(caps)))

    def test_relative_axes_alone_reject(self):
        caps = kbd_caps(sections={E.EV_REL: [E.REL_X, E.REL_Y]})
        self.assertFalse(ke.is_text_keyboard(FakeDevice(caps)))

    def test_touchscreen_rejected(self):
        caps = kbd_caps(extra_keys=(E.BTN_TOUCH,),
                        sections={E.EV_ABS: [E.ABS_X, E.ABS_Y]})
        self.assertFalse(ke.is_text_keyboard(FakeDevice(caps)))

    def test_keyboard_with_leds_accepted(self):
        caps = kbd_caps(sections={E.EV_LED: [E.LED_CAPSL],
                                  E.EV_MSC: [E.MSC_SCAN]})
        self.assertTrue(ke.is_text_keyboard(FakeDevice(caps)))

    def test_partial_alphabet_rejected(self):
        self.assertFalse(ke.is_text_keyboard(
            FakeDevice({E.EV_KEY: [E.KEY_A, E.KEY_SPACE]})))

    def test_unreadable_device_rejected_not_raising(self):
        class Boom:
            name = 'boom'
            path = '/dev/input/event9'

            def capabilities(self):
                raise OSError('gone')
        self.assertFalse(ke.is_text_keyboard(Boom()))


class HidTableTests(unittest.TestCase):
    """The table is a true inverse of the kernel's own HID mapping."""

    def test_no_duplicate_usages(self):
        values = list(ke.KEYCODE_TO_HID.values())
        self.assertEqual(len(values), len(set(values)))

    def test_usages_in_spec_range(self):
        for code, usage in ke.KEYCODE_TO_HID.items():
            self.assertTrue(1 <= usage <= 231, f'{code} -> {usage}')

    def test_modifiers_not_also_in_key_table(self):
        self.assertEqual(
            set(ke.KEYCODE_TO_HID) & set(ke.KEYCODE_TO_MOD_BIT), set())

    def test_modifier_bits_are_distinct_and_single(self):
        bits = list(ke.KEYCODE_TO_MOD_BIT.values())
        self.assertEqual(len(bits), len(set(bits)))
        for b in bits:
            self.assertEqual(bin(b).count('1'), 1)

    def test_known_anchors(self):
        """Spot-check against the HID Keyboard/Keypad usage table."""
        for code, usage in (
            (E.KEY_A, 4), (E.KEY_Z, 29),
            (E.KEY_1, 30), (E.KEY_0, 39),
            (E.KEY_ENTER, 40), (E.KEY_ESC, 41),
            (E.KEY_BACKSPACE, 42), (E.KEY_TAB, 43), (E.KEY_SPACE, 44),
            (E.KEY_MINUS, 45), (E.KEY_EQUAL, 46),
            (E.KEY_LEFTBRACE, 47), (E.KEY_RIGHTBRACE, 48),
            (E.KEY_BACKSLASH, 49), (E.KEY_SEMICOLON, 51),
            (E.KEY_APOSTROPHE, 52), (E.KEY_GRAVE, 53),
            (E.KEY_COMMA, 54), (E.KEY_DOT, 55), (E.KEY_SLASH, 56),
            (E.KEY_CAPSLOCK, 57), (E.KEY_F1, 58), (E.KEY_F12, 69),
            (E.KEY_SYSRQ, 70), (E.KEY_INSERT, 73), (E.KEY_HOME, 74),
            (E.KEY_DELETE, 76), (E.KEY_RIGHT, 79), (E.KEY_LEFT, 80),
            (E.KEY_DOWN, 81), (E.KEY_UP, 82),
            (E.KEY_KPSLASH, 84), (E.KEY_KPENTER, 88), (E.KEY_KP0, 98),
            (E.KEY_KPDOT, 99), (E.KEY_102ND, 100), (E.KEY_COMPOSE, 101),
            (E.KEY_F13, 104), (E.KEY_F24, 115),
        ):
            self.assertEqual(ke.KEYCODE_TO_HID.get(code), usage,
                             f'keycode {code} should be HID usage {usage}')

    def test_digit_row_is_contiguous(self):
        digits = [E.KEY_1, E.KEY_2, E.KEY_3, E.KEY_4, E.KEY_5,
                  E.KEY_6, E.KEY_7, E.KEY_8, E.KEY_9, E.KEY_0]
        self.assertEqual([ke.KEYCODE_TO_HID[d] for d in digits],
                         list(range(30, 40)))

    def test_whole_alphabet_present_and_ordered(self):
        letters = [getattr(E, f'KEY_{c}') for c in
                   'ABCDEFGHIJKLMNOPQRSTUVWXYZ']
        self.assertEqual([ke.KEYCODE_TO_HID[l] for l in letters],
                         list(range(4, 30)))


if __name__ == '__main__':
    unittest.main(verbosity=2)
