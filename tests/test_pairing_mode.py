"""Tests for the time-boxed pairing window.

Being discoverable and pairable is only needed to acquire a bond. Left
on permanently it advertises the machine to everyone in range, and
because the agent auto-confirms numeric comparison, a stranger who
tries could end up a bonded HID host — receiving whatever is typed
while grab is on. So the window has to default closed, open only on
request or when there is no bond yet, and close by itself.

The D-Bus writes are stubbed; what matters here is the policy and the
bookkeeping the tray reads.
"""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bluetooth_handler as bh
from bluetooth_handler import ToothkeyHandler as TH


class FakeProps:
    """Records property writes the way BlueZ would receive them."""

    def __init__(self, fail=False):
        self.writes = []
        self.fail = fail

    def Set(self, iface, name, value):
        if self.fail:
            raise RuntimeError('d-bus is unhappy')
        self.writes.append((name, bool(value) if name in
                            ('Pairable', 'Discoverable') else int(value)))

    def value(self, name):
        for n, v in reversed(self.writes):
            if n == name:
                return v
        return None


class PairingModeTestBase(unittest.TestCase):

    def setUp(self):
        self.props = FakeProps()
        self._saved = {
            'bus': TH._bus, 'path': TH._adapter_path,
            'until': TH._pairing_until, 'thread': TH._pairing_thread,
            'running': TH._running, 'tlog': bh._tlog,
            'name': bh.build_device_name,
        }
        bh._tlog = lambda msg: None
        bh.build_device_name = lambda: 'Tooth-key (testbox)'
        TH._bus = object()               # just needs to be non-None
        TH._adapter_path = '/org/bluez/hci0'
        TH._pairing_until = 0.0
        TH._pairing_thread = None
        TH._pairing_wake.clear()
        TH._running = True
        # Intercept the adapter property writes.
        TH._pairing_props = self.props
        self._orig_iface = bh.Interface
        bh.Interface = lambda obj, iface: self.props
        TH._bus = type('B', (), {'get_object': lambda *a: object()})()

    def tearDown(self):
        bh.Interface = self._orig_iface
        bh._tlog = self._saved['tlog']
        bh.build_device_name = self._saved['name']
        TH._bus = self._saved['bus']
        TH._adapter_path = self._saved['path']
        TH._pairing_until = self._saved['until']
        TH._pairing_thread = self._saved['thread']
        TH._running = self._saved['running']
        TH._pairing_wake.set()
        time.sleep(0.05)
        TH._pairing_wake.clear()


class OpeningAndClosingTests(PairingModeTestBase):

    def test_opening_sets_both_flags(self):
        self.assertTrue(TH.set_pairing_mode(True))
        self.assertIs(self.props.value('Pairable'), True)
        self.assertIs(self.props.value('Discoverable'), True)

    def test_closing_clears_both_flags(self):
        TH.set_pairing_mode(True)
        self.assertFalse(TH.set_pairing_mode(False))
        self.assertIs(self.props.value('Pairable'), False)
        self.assertIs(self.props.value('Discoverable'), False)

    def test_timeouts_are_set_before_the_flags(self):
        """BlueZ applies the timeout in force when the flag flips."""
        TH.set_pairing_mode(True)
        names = [n for n, _ in self.props.writes]
        self.assertLess(names.index('PairableTimeout'),
                        names.index('Pairable'))
        self.assertLess(names.index('DiscoverableTimeout'),
                        names.index('Discoverable'))

    def test_bluez_is_given_the_window_so_it_expires_without_us(self):
        """The window must close even if this process dies."""
        TH.set_pairing_mode(True)
        self.assertEqual(self.props.value('DiscoverableTimeout'),
                         bh.PAIRING_WINDOW_S)
        self.assertEqual(self.props.value('PairableTimeout'),
                         bh.PAIRING_WINDOW_S)

    def test_closing_zeroes_the_timeouts(self):
        TH.set_pairing_mode(False)
        self.assertEqual(self.props.value('DiscoverableTimeout'), 0)

    def test_custom_window_is_honoured(self):
        TH.set_pairing_mode(True, window_s=30)
        self.assertEqual(self.props.value('DiscoverableTimeout'), 30)
        self.assertLessEqual(TH.pairing_secs_left(), 30)

    def test_secs_left_counts_down_from_the_window(self):
        TH.set_pairing_mode(True)
        self.assertGreater(TH.pairing_secs_left(), bh.PAIRING_WINDOW_S - 5)
        self.assertLessEqual(TH.pairing_secs_left(), bh.PAIRING_WINDOW_S)

    def test_closed_reports_zero_and_not_pairing(self):
        TH.set_pairing_mode(False)
        self.assertEqual(TH.pairing_secs_left(), 0)
        self.assertFalse(TH.is_pairing())

    def test_expired_window_reports_not_pairing(self):
        TH.set_pairing_mode(True)
        TH._pairing_until = time.monotonic() - 1    # age it out
        self.assertFalse(TH.is_pairing())
        self.assertEqual(TH.pairing_secs_left(), 0)

    def test_reopening_restarts_the_window(self):
        TH.set_pairing_mode(True)
        TH._pairing_until = time.monotonic() + 5
        TH.set_pairing_mode(True)
        self.assertGreater(TH.pairing_secs_left(), 60,
                           'a second request should extend the window')

    def test_dbus_failure_is_reported_not_raised(self):
        self.props.fail = True
        self.assertFalse(TH.set_pairing_mode(True))
        self.assertFalse(TH.is_pairing(),
                         'a failed open must not claim to be pairing')

    def test_no_adapter_refuses_cleanly(self):
        TH._adapter_path = None
        self.assertFalse(TH.set_pairing_mode(True))

    def test_no_bus_refuses_cleanly(self):
        TH._bus = None
        self.assertFalse(TH.set_pairing_mode(True))


class ExpiryTests(PairingModeTestBase):

    def test_window_closes_itself(self):
        TH.set_pairing_mode(True, window_s=1)
        # BlueZ closes its own flags exactly on the window; our expiry
        # thread reconciles shortly after, so poll for the write rather
        # than assuming it has landed the instant is_pairing() flips.
        deadline = time.monotonic() + 5
        while (self.props.value('Discoverable') is not False
               and time.monotonic() < deadline):
            time.sleep(0.05)
        self.assertFalse(TH.is_pairing())
        self.assertIs(self.props.value('Discoverable'), False,
                      'expiry must actually close it, not just forget')
        self.assertIs(self.props.value('Pairable'), False)

    def test_only_one_expiry_thread(self):
        TH.set_pairing_mode(True, window_s=5)
        first = TH._pairing_thread
        TH.set_pairing_mode(True, window_s=5)
        self.assertIs(TH._pairing_thread, first)


class StartupPolicyTests(PairingModeTestBase):
    """With no bond there is nothing to reconnect to, so be findable."""

    def test_no_bond_opens_the_window(self):
        TH._bonded_peer_count = classmethod(lambda cls: 0)
        try:
            TH._apply_initial_pairing_mode()
            self.assertTrue(TH.is_pairing())
        finally:
            del TH._bonded_peer_count

    def test_existing_bond_keeps_it_shut(self):
        TH._bonded_peer_count = classmethod(lambda cls: 1)
        try:
            TH._apply_initial_pairing_mode()
            self.assertFalse(TH.is_pairing())
            self.assertIs(self.props.value('Discoverable'), False)
        finally:
            del TH._bonded_peer_count


if __name__ == '__main__':
    unittest.main(verbosity=2)
