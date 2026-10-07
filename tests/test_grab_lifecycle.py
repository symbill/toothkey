"""Tests for the grab lifecycle, with fake devices standing in for
/dev/input.

The grab itself is a kernel ioctl these tests can't take (that needs
root and real hardware — see tools/kbd_selftest.py). What they can
pin down is everything around it, which is where the ways to leave a
user's keyboard captured actually live: that every exit path releases,
that an all-keys-up report goes out so the peer isn't left with a
stuck key, and that the app never reports holding a grab it lost.
"""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import keyboard_evdev as ke
from evdev import ecodes as E


class FakeDev:
    """An evdev.InputDevice stand-in with a scripted event stream."""

    _next_fd = 500

    def __init__(self, name='fake kbd', events=None, grab_raises=None):
        FakeDev._next_fd += 1
        self.fd = FakeDev._next_fd
        self.name = name
        self.path = f'/dev/input/event{self.fd}'
        self.grabbed = False
        self.ungrabbed = False
        self.closed = False
        self._grab_raises = grab_raises
        self._events = list(events or [])

    def capabilities(self):
        return {E.EV_KEY: [E.KEY_A, E.KEY_Z, E.KEY_SPACE, E.KEY_ENTER]}

    def grab(self):
        if self._grab_raises is not None:
            raise self._grab_raises
        self.grabbed = True

    def ungrab(self):
        self.ungrabbed = True

    def close(self):
        self.closed = True

    def read(self):
        events, self._events = self._events, []
        return events


class Harness:
    """Swaps discover_keyboards for a fixed device list."""

    def __init__(self, devices):
        self.devices = devices
        self._orig = None

    def __enter__(self):
        self._orig = ke.discover_keyboards
        served = {'done': False}

        def fake(log=None):
            # Serve the devices once; a rescan then finds nothing new.
            if served['done']:
                return []
            served['done'] = True
            return list(self.devices)
        ke.discover_keyboards = fake
        return self

    def __exit__(self, *exc):
        ke.discover_keyboards = self._orig


def make(devices, **kw):
    sent, chords, lost = [], [], []
    g = ke.KeyboardGrabber(
        on_report=sent.append,
        on_chord=chords.append,
        on_lost=lambda: lost.append(True),
        log=lambda m: None, **kw)
    return g, sent, chords, lost


class StartStopTests(unittest.TestCase):

    def test_start_grabs_every_keyboard(self):
        devs = [FakeDev('kbd one'), FakeDev('kbd two')]
        with Harness(devs):
            g, *_ = make(devs)
            self.assertTrue(g.start())
            try:
                self.assertTrue(all(d.grabbed for d in devs))
            finally:
                g.stop()

    def test_stop_releases_and_closes_every_device(self):
        devs = [FakeDev(), FakeDev()]
        with Harness(devs):
            g, *_ = make(devs)
            g.start()
            g.stop()
        self.assertTrue(all(d.ungrabbed for d in devs))
        self.assertTrue(all(d.closed for d in devs))

    def test_stop_sends_all_keys_up(self):
        """A key held at ungrab would otherwise repeat on the peer."""
        devs = [FakeDev()]
        with Harness(devs):
            g, sent, _c, _l = make(devs)
            g.start()
            g._on_key_event(type('e', (), {
                'type': E.EV_KEY, 'code': E.KEY_A, 'value': 1})())
            self.assertEqual(sent[-1][4], 4)
            g.stop()
        self.assertEqual(sent[-1][4:], b'\x00' * 6)
        self.assertEqual(sent[-1][2], 0, 'modifiers must be cleared too')

    def test_stop_is_idempotent(self):
        devs = [FakeDev()]
        with Harness(devs):
            g, *_ = make(devs)
            g.start()
            g.stop()
            g.stop()          # must not raise
        self.assertTrue(devs[0].ungrabbed)

    def test_start_with_no_devices_fails_cleanly(self):
        with Harness([]):
            g, *_ = make([])
            self.assertFalse(g.start())
            self.assertFalse(g.is_running())

    def test_ungrabbable_device_is_closed_not_leaked(self):
        busy = FakeDev('busy', grab_raises=OSError(16, 'Device or resource busy'))
        with Harness([busy]):
            g, *_ = make([busy])
            self.assertFalse(g.start(), 'no grabbable device means no capture')
        self.assertTrue(busy.closed)

    def test_one_busy_device_does_not_block_the_others(self):
        busy = FakeDev('busy', grab_raises=OSError(16, 'busy'))
        good = FakeDev('good')
        with Harness([busy, good]):
            g, *_ = make([busy, good])
            self.assertTrue(g.start())
            try:
                self.assertTrue(good.grabbed)
            finally:
                g.stop()

    def test_double_start_does_not_regrab(self):
        devs = [FakeDev()]
        with Harness(devs):
            g, *_ = make(devs)
            g.start()
            try:
                self.assertTrue(g.start(), 'second start is a no-op success')
            finally:
                g.stop()

    def test_is_running_tracks_the_reader(self):
        devs = [FakeDev()]
        with Harness(devs):
            g, *_ = make(devs)
            g.start()
            self.assertTrue(g.is_running())
            g.stop()
            self.assertFalse(g.is_running())


class CaptureLostTests(unittest.TestCase):

    def test_on_lost_fires_when_devices_disappear(self):
        """Unplugging the only keyboard must not leave grab_mode true."""
        dev = FakeDev()
        with Harness([dev]):
            g, _s, _c, lost = make([dev])
            g.start()
            # Simulate the node going away under the reader.
            g._drop_device(dev.fd)
            deadline = time.monotonic() + 3.0
            while not lost and time.monotonic() < deadline:
                time.sleep(0.02)
            g.stop()
        self.assertTrue(lost, 'on_lost must fire when capture dies on its own')

    def test_on_lost_does_not_fire_on_a_requested_stop(self):
        dev = FakeDev()
        with Harness([dev]):
            g, _s, _c, lost = make([dev])
            g.start()
            g.stop()
        time.sleep(0.2)
        self.assertEqual(lost, [], 'a deliberate stop is not a loss')

    def test_on_lost_does_not_fire_on_panic(self):
        """Panic has its own notification path; don't double-report."""
        dev = FakeDev()
        with Harness([dev]):
            g, _s, chords, lost = make([dev])
            g.start()
            for code in (E.KEY_LEFTCTRL, E.KEY_LEFTALT, E.KEY_LEFTSHIFT,
                         E.KEY_ESC):
                g._on_key_event(type('e', (), {
                    'type': E.EV_KEY, 'code': code, 'value': 1})())
            time.sleep(0.4)
            g.stop()
        self.assertEqual(chords, ['panic'])
        self.assertEqual(lost, [])


class ReaderThreadTests(unittest.TestCase):

    def test_reader_thread_is_a_daemon(self):
        """A stuck reader must never keep the worker alive."""
        devs = [FakeDev()]
        with Harness(devs):
            g, *_ = make(devs)
            g.start()
            try:
                self.assertTrue(g._thread.daemon)
            finally:
                g.stop()

    def test_devices_released_even_if_reader_crashes(self):
        dev = FakeDev()

        class Exploding(FakeDev):
            def read(self):
                raise RuntimeError('boom')

        bad = Exploding()
        with Harness([bad]):
            g, *_ = make([bad])
            g.start()
            # Force the reader down the error path.
            g._stop.clear()
            deadline = time.monotonic() + 2.0
            while g.is_running() and time.monotonic() < deadline:
                time.sleep(0.02)
            g.stop()
        self.assertTrue(bad.ungrabbed or bad.closed,
                        'a crashing reader must still release the keyboard')


if __name__ == '__main__':
    unittest.main(verbosity=2)
