"""Tests for ToothkeyKeyboardHandler: when a grab is taken, and what it
reports.

The thing being pinned down here is honesty. A grab that could not be
taken has to be reported as off, because the tray believes what the
worker tells it: a green tooth over an uncaptured keyboard is the
state that makes "I pressed keys and nothing reached the phone" look
like a Bluetooth problem.
"""

import io
import os
import sys
import unittest
import contextlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import keyboard_evdev
import keyboard_handler
from common import GlobalContext
from keyboard_handler import ToothkeyKeyboardHandler as KH


class FakeGrabber:
    """Stands in for KeyboardGrabber. `can_start` picks the outcome."""

    can_start = True
    instances = []

    def __init__(self, on_report, on_chord=None, log=None, on_lost=None):
        self.on_report = on_report
        self.on_chord = on_chord
        self.on_lost = on_lost
        self.started = False
        self.stopped = False
        self.suppress_forwarding = False
        FakeGrabber.instances.append(self)

    def start(self):
        self.started = FakeGrabber.can_start
        return FakeGrabber.can_start

    def stop(self):
        self.stopped = True
        self.started = False

    def is_running(self):
        return self.started

    def send_current_state(self):
        pass


class HandlerTestBase(unittest.TestCase):

    def setUp(self):
        self._real_grabber = keyboard_evdev.KeyboardGrabber
        self._real_connected = GlobalContext.is_peer_connected
        keyboard_handler.keyboard_evdev.KeyboardGrabber = FakeGrabber
        FakeGrabber.instances = []
        FakeGrabber.can_start = True
        GlobalContext.is_peer_connected = classmethod(lambda cls: True)
        GlobalContext.grab_mode = False
        KH._grabber = None
        KH.paste_in_progress = False
        self._stdout = contextlib.redirect_stdout(io.StringIO())
        self._stdout.__enter__()

    def tearDown(self):
        self._stdout.__exit__(None, None, None)
        KH._grabber = None
        GlobalContext.grab_mode = False
        keyboard_handler.keyboard_evdev.KeyboardGrabber = self._real_grabber
        GlobalContext.is_peer_connected = self._real_connected


class GrabRefusalTests(HandlerTestBase):

    def test_grab_succeeds_when_connected(self):
        self.assertTrue(KH.set_grab_mode(True))
        self.assertTrue(GlobalContext.grab_mode)
        self.assertTrue(KH.is_running())

    def test_grab_refused_without_a_peer(self):
        GlobalContext.is_peer_connected = classmethod(lambda cls: False)
        self.assertFalse(KH.set_grab_mode(True),
                         'never hold the keyboard with nowhere to send it')
        self.assertFalse(GlobalContext.grab_mode)
        self.assertEqual(FakeGrabber.instances, [],
                         'should not even construct a grabber')

    def test_grab_refused_when_capture_cannot_start(self):
        FakeGrabber.can_start = False
        self.assertFalse(KH.set_grab_mode(True),
                         'a failed grab must be reported as off')
        self.assertFalse(GlobalContext.grab_mode)

    def test_failed_grab_leaves_nothing_behind(self):
        FakeGrabber.can_start = False
        KH.set_grab_mode(True)
        self.assertFalse(KH.is_running())
        self.assertIsNone(KH._grabber)

    def test_ungrab_stops_the_grabber(self):
        KH.set_grab_mode(True)
        g = FakeGrabber.instances[-1]
        self.assertFalse(KH.set_grab_mode(False))
        self.assertTrue(g.stopped)
        self.assertFalse(GlobalContext.grab_mode)

    def test_repeat_grab_does_not_make_a_second_grabber(self):
        KH.set_grab_mode(True)
        KH.set_grab_mode(True)
        self.assertEqual(len(FakeGrabber.instances), 1)

    def test_repeat_ungrab_is_harmless(self):
        KH.set_grab_mode(True)
        KH.set_grab_mode(False)
        self.assertFalse(KH.set_grab_mode(False))

    def test_ungrab_stops_a_grabber_even_if_flag_disagrees(self):
        """The off path must not short-circuit past a live grab."""
        KH.set_grab_mode(True)
        g = FakeGrabber.instances[-1]
        GlobalContext.grab_mode = False      # flag and reality diverge
        KH.set_grab_mode(False)
        self.assertTrue(g.stopped, 'a live grab must still be released')


class ArmedAcrossReconnectTests(HandlerTestBase):
    """grab_mode means "armed"; is_running() means "held right now"."""

    def test_link_drop_releases_but_stays_armed(self):
        KH.set_grab_mode(True)
        g = FakeGrabber.instances[-1]
        KH.stop_listener()
        self.assertTrue(g.stopped, 'the grab must be released')
        self.assertTrue(GlobalContext.grab_mode, 'but it stays armed')
        self.assertFalse(KH.is_running())

    def test_reconnect_retakes_the_grab(self):
        KH.set_grab_mode(True)
        KH.stop_listener()
        self.assertTrue(KH.start_listener())
        self.assertTrue(KH.is_running())
        self.assertEqual(len(FakeGrabber.instances), 2)

    def test_reconnect_does_nothing_when_not_armed(self):
        self.assertFalse(KH.start_listener())
        self.assertEqual(FakeGrabber.instances, [])

    def test_reconnect_that_cannot_regrab_disarms(self):
        KH.set_grab_mode(True)
        KH.stop_listener()
        FakeGrabber.can_start = False
        self.assertFalse(KH.start_listener())
        self.assertFalse(GlobalContext.grab_mode,
                         'a grab we cannot retake must not be claimed')

    def test_shutdown_releases_and_disarms(self):
        KH.set_grab_mode(True)
        g = FakeGrabber.instances[-1]
        try:
            KH.shutdown()
            self.assertTrue(g.stopped)
            self.assertFalse(GlobalContext.grab_mode)
            self.assertFalse(KH.active)
        finally:
            KH.active = True


class PanicNotificationTests(HandlerTestBase):

    def test_panic_clears_grab_and_notifies(self):
        seen = []
        KH.on_grab_lost = staticmethod(lambda state: seen.append(state))
        try:
            KH.set_grab_mode(True)
            KH._on_chord('panic')
            deadline = __import__('time').monotonic() + 3.0
            while not seen and __import__('time').monotonic() < deadline:
                __import__('time').sleep(0.02)
            self.assertEqual(seen, [False])
            self.assertFalse(GlobalContext.grab_mode)
        finally:
            KH.on_grab_lost = None

    def test_capture_lost_clears_grab_and_notifies(self):
        seen = []
        KH.on_grab_lost = staticmethod(lambda state: seen.append(state))
        try:
            KH.set_grab_mode(True)
            KH._on_capture_lost()
            deadline = __import__('time').monotonic() + 3.0
            while not seen and __import__('time').monotonic() < deadline:
                __import__('time').sleep(0.02)
            self.assertEqual(seen, [False])
            self.assertFalse(GlobalContext.grab_mode)
        finally:
            KH.on_grab_lost = None


if __name__ == '__main__':
    unittest.main(verbosity=2)
