"""Tests for the taskbar/pager/switcher hint helper.

Whether KWin honours the hint can only be answered by asking KWin, and
these tests don't try to. What they pin down is that the helper never
becomes a way to break the tray: it refuses bad input, survives a
missing X server or a missing python-xlib, and never raises — a lost
taskbar hint is cosmetic, a crash in the toast path is not.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import x11_window_hints as xwh


class BadInputTests(unittest.TestCase):
    """A bogus window id must be refused, not passed to the X server."""

    def test_none_refused(self):
        self.assertFalse(xwh.skip_taskbar_and_pager(None))

    def test_zero_refused(self):
        self.assertFalse(xwh.skip_taskbar_and_pager(0))

    def test_negative_refused(self):
        self.assertFalse(xwh.skip_taskbar_and_pager(-1))

    def test_non_numeric_refused(self):
        self.assertFalse(xwh.skip_taskbar_and_pager('not-a-window'))

    def test_numeric_string_accepted_as_an_id(self):
        """Qt's winId() is an int-like; a digit string is still an id.

        It names no real window, so the X request fails and we report
        False — the point is that it is parsed, not rejected outright,
        and that nothing raises.
        """
        self.assertIsInstance(xwh.skip_taskbar_and_pager('123456789'), bool)


class DegradationTests(unittest.TestCase):

    def setUp(self):
        self._display = xwh._display
        self._err = xwh._display_error
        self._import_err = xwh._IMPORT_ERROR
        self._xdisplay = xwh._xdisplay

    def tearDown(self):
        xwh._display = self._display
        xwh._display_error = self._err
        xwh._IMPORT_ERROR = self._import_err
        xwh._xdisplay = self._xdisplay

    def test_no_xlib_is_a_quiet_no_op(self):
        xwh._display = None
        xwh._xdisplay = None
        self.assertFalse(xwh.skip_taskbar_and_pager(12345))

    def test_no_xlib_is_reported_as_a_reason(self):
        xwh._IMPORT_ERROR = ImportError('no module named Xlib')
        self.assertIn('python-xlib', xwh.unavailable_reason())

    def test_unreachable_display_is_reported(self):
        xwh._IMPORT_ERROR = None
        xwh._display_error = OSError('cannot connect')
        self.assertIn('X server', xwh.unavailable_reason())

    def test_unreachable_display_is_a_quiet_no_op(self):
        xwh._display = None
        xwh._xdisplay = None
        xwh._display_error = OSError('cannot connect')
        self.assertFalse(xwh.skip_taskbar_and_pager(12345))

    def test_display_failure_is_not_retried_every_call(self):
        """Opening a dead connection per toast would be a stall."""
        calls = []

        class Boom:
            @staticmethod
            def Display():
                calls.append(1)
                raise OSError('nope')

        xwh._display = None
        xwh._display_error = None
        xwh._xdisplay = Boom
        xwh.skip_taskbar_and_pager(1)
        first = len(calls)
        xwh.skip_taskbar_and_pager(1)
        self.assertEqual(len(calls), first + 1,
                         'each attempt opens at most one connection')


class AvailabilityOnThisBoxTests(unittest.TestCase):
    """Informational: says whether the hint can work where this runs."""

    def test_reason_is_none_or_a_string(self):
        reason = xwh.unavailable_reason()
        self.assertTrue(reason is None or isinstance(reason, str))

    @unittest.skipUnless(os.environ.get('DISPLAY'), 'needs an X display')
    def test_xlib_reaches_the_display_when_one_exists(self):
        self.assertIsNone(xwh.unavailable_reason(),
                          'python-xlib should reach the X server here')


if __name__ == '__main__':
    unittest.main(verbosity=2)
