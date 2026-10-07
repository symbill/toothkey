"""Tests for the peripheral-initiated HID open retry.

On a warm reconnect iOS often accepts the control channel at once and
refuses the interrupt channel for a few seconds. One attempt per page
turns that near-miss into a wait for the next watchdog cadence — about
45 seconds in the logs, against roughly 0.2s once iOS is ready. These
tests pin down the retry that closes that gap, and the bounds that stop
it becoming a page storm.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bluetooth_handler as bh
from bluetooth_handler import ToothkeyHandler as TH

# Documentation-range address (RFC 7042), not real hardware.
MAC = '00:00:5E:00:53:01'


class RetryTests(unittest.TestCase):

    def setUp(self):
        self.attempts = []
        self._saved = {
            'once': TH._try_open_hid_outbound_once,
            'acl': TH._peer_acl_connected,
            'tlog': bh._tlog,
            'connected': TH.connected,
            'running': TH._running,
        }
        bh._tlog = lambda msg: None
        TH.connected = False
        TH._running = True
        TH._peer_acl_connected = classmethod(lambda cls, mac: True)
        TH._reconnect_wake.clear()

    def tearDown(self):
        TH._try_open_hid_outbound_once = self._saved['once']
        TH._peer_acl_connected = self._saved['acl']
        bh._tlog = self._saved['tlog']
        TH.connected = self._saved['connected']
        TH._running = self._saved['running']
        TH._reconnect_wake.clear()

    def _script(self, outcomes):
        """Make _try_open_hid_outbound_once return each outcome in turn."""
        seq = list(outcomes)

        def fake(cls, mac, attempt):
            self.attempts.append(attempt)
            return seq.pop(0) if seq else False
        TH._try_open_hid_outbound_once = classmethod(fake)

    def test_succeeds_on_the_first_attempt(self):
        self._script([True])
        self.assertTrue(TH._try_open_hid_outbound(MAC))
        self.assertEqual(self.attempts, [1], 'no retry when it works')

    def test_retries_after_a_near_miss(self):
        """The case from the logs: fails once, then iOS is ready."""
        self._script([False, True])
        self.assertTrue(TH._try_open_hid_outbound(MAC))
        self.assertEqual(self.attempts, [1, 2])

    def test_gives_up_after_the_attempt_cap(self):
        self._script([False, False, False, False, False])
        self.assertFalse(TH._try_open_hid_outbound(MAC))
        self.assertEqual(self.attempts,
                         list(range(1, bh._HID_OUTBOUND_ATTEMPTS + 1)),
                         'must not retry forever inside one page')

    def test_stops_retrying_once_the_acl_is_gone(self):
        """Retrying against a dead ACL just burns the connect timeout."""
        self._script([False, False, False])
        TH._peer_acl_connected = classmethod(lambda cls, mac: False)
        self.assertFalse(TH._try_open_hid_outbound(MAC))
        self.assertEqual(self.attempts, [1])

    def test_does_not_race_an_inbound_session(self):
        """If the normal accept() path already won, stay out of its way."""
        TH.connected = True
        self._script([True])
        self.assertFalse(TH._try_open_hid_outbound(MAC))
        self.assertEqual(self.attempts, [])

    def test_stops_when_the_worker_is_shutting_down(self):
        self._script([False, False, False])
        TH._running = False
        self.assertFalse(TH._try_open_hid_outbound(MAC))
        self.assertEqual(self.attempts, [])

    def test_stops_as_soon_as_a_session_is_established(self):
        """A session adopted mid-retry must end the loop."""
        def fake(cls, mac, attempt):
            self.attempts.append(attempt)
            TH.connected = True      # wait_for_client adopted the pair
            return False
        TH._try_open_hid_outbound_once = classmethod(fake)
        TH._try_open_hid_outbound(MAC)
        self.assertEqual(self.attempts, [1])


class BudgetTests(unittest.TestCase):
    """The retry must not cost more wall clock than it used to."""

    def test_worst_case_is_bounded(self):
        worst = (bh._HID_OUTBOUND_ATTEMPTS * 2
                 * bh._HID_OUTBOUND_CONNECT_TIMEOUT_S
                 + (bh._HID_OUTBOUND_ATTEMPTS - 1)
                 * bh._HID_OUTBOUND_RETRY_GAP_S)
        self.assertLessEqual(
            worst, 20.0,
            'the whole sequence must fit the budget a single attempt at '
            '10s per channel used to cost')

    def test_fits_inside_the_acl_hold_window(self):
        """The caller holds the ACL up for 30s; overrunning that would
        mean retrying against a link that has already gone."""
        worst = (bh._HID_OUTBOUND_ATTEMPTS * 2
                 * bh._HID_OUTBOUND_CONNECT_TIMEOUT_S
                 + (bh._HID_OUTBOUND_ATTEMPTS - 1)
                 * bh._HID_OUTBOUND_RETRY_GAP_S)
        self.assertLess(worst, 30.0)

    def test_timeout_is_generous_against_a_ready_peer(self):
        """Measured accepts land in 0.1-0.5s when iOS is ready."""
        self.assertGreaterEqual(bh._HID_OUTBOUND_CONNECT_TIMEOUT_S, 2.0)

    def test_more_than_one_attempt(self):
        self.assertGreater(bh._HID_OUTBOUND_ATTEMPTS, 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
