"""Unit tests for which peers we touch, and how loudly we log them.

The adapter hears every phone, watch and pair of earbuds in radio
range. These tests pin down the two rules that keep that from turning
into pairing requests sent to strangers and gigabytes of log: which
devices are pairing candidates, and how often a bystander may be
logged.

`bluetooth_handler._tlog` is stubbed throughout. The real one imports
worker, which installs logging_setup and would append to the live
logs/toothkey.log.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bluetooth_handler as bh
from bluetooth_handler import ToothkeyHandler as TH


# Property shapes as BlueZ actually reports them, with addresses from
# the documentation range reserved by RFC 7042 rather than any real
# hardware. The byte pattern is not what the code keys on anyway —
# AddressType is — but the LE entries use a first octet of 0x40 so the
# top two bits read as a resolvable private address, which is what a
# real LE advertiser looks like. Note they carry no Class at all,
# which is the other tell.
PHONE = {'Address': '00:00:5E:00:53:01', 'AddressType': 'public',
         'Class': 0x7a020c, 'Name': 'test-phone',
         'Paired': False, 'Bonded': False, 'Connected': False,
         'Blocked': False}
LE_STRANGER = {'Address': '40:00:5E:00:53:10', 'AddressType': 'random',
               'Name': '40-00-5E-00-53-10',
               'Paired': False, 'Bonded': False, 'Connected': False,
               'Blocked': False}


class PairingCandidateTests(unittest.TestCase):

    def ok(self, dev):
        candidate, why = TH._classic_pairing_candidate(dev)
        return candidate

    def reason(self, dev):
        return TH._classic_pairing_candidate(dev)[1]

    def test_classic_phone_is_a_candidate(self):
        self.assertTrue(self.ok(PHONE))

    def test_candidate_reason_is_empty(self):
        self.assertEqual(self.reason(PHONE), '')

    def test_le_random_address_rejected(self):
        self.assertFalse(self.ok(LE_STRANGER))

    def test_le_rejection_reason_mentions_the_address_type(self):
        self.assertIn('random', self.reason(LE_STRANGER))

    def test_public_address_without_class_still_a_candidate(self):
        """A BR/EDR peer's Class may not have landed yet.

        Refusing it would break first-time pairing, which is the one
        case that most needs us to drive SSP.
        """
        dev = dict(PHONE)
        del dev['Class']
        self.assertTrue(self.ok(dev))

    def test_already_paired_rejected(self):
        self.assertFalse(self.ok(dict(PHONE, Paired=True)))

    def test_blocked_rejected(self):
        self.assertFalse(self.ok(dict(PHONE, Blocked=True)))

    def test_missing_address_type_treated_as_public(self):
        """Classic-only entries sometimes omit AddressType."""
        dev = dict(PHONE)
        del dev['AddressType']
        self.assertTrue(self.ok(dev))

    def test_class_of_zero_is_still_a_class(self):
        """0 is a real value; only absence means LE-only."""
        self.assertTrue(self.ok(dict(PHONE, Class=0)))

    def test_a_scanful_of_le_strangers_is_rejected(self):
        """A running scan produces a stream of these, all to be ignored.

        One per rotation of one nearby device's privacy address: in the
        logs this was thousands of pairing attempts on passing phones
        and earbuds.
        """
        for n in range(0x10, 0x20):
            dev = dict(LE_STRANGER, Address=f'40:00:5E:00:53:{n:02X}')
            self.assertFalse(self.ok(dev),
                             f'{dev["Address"]} must be ignored')


class PairAttemptRateLimitTests(unittest.TestCase):

    def setUp(self):
        TH._pair_attempts = {}
        self._tlog = bh._tlog
        bh._tlog = lambda msg: None
        bh._throttle_state.clear()

    def tearDown(self):
        bh._tlog = self._tlog
        TH._pair_attempts = {}
        bh._throttle_state.clear()

    def test_first_attempt_allowed(self):
        self.assertTrue(TH._pair_attempt_allowed('AA:BB:CC:DD:EE:FF'))

    def test_immediate_retry_refused(self):
        TH._pair_attempt_allowed('AA:BB:CC:DD:EE:FF')
        self.assertFalse(TH._pair_attempt_allowed('AA:BB:CC:DD:EE:FF'))

    def test_retry_allowed_after_the_interval(self):
        addr = 'AA:BB:CC:DD:EE:FF'
        TH._pair_attempt_allowed(addr)
        count, last = TH._pair_attempts[addr]
        TH._pair_attempts[addr] = (count, last - TH._PAIR_RETRY_INTERVAL_S - 1)
        self.assertTrue(TH._pair_attempt_allowed(addr))

    def test_total_attempts_are_capped(self):
        addr = 'AA:BB:CC:DD:EE:FF'
        for _ in range(TH._PAIR_MAX_ATTEMPTS_PER_PEER):
            count, _last = TH._pair_attempts.get(addr, (0, 0.0))
            TH._pair_attempts[addr] = (count, 0.0)   # age the timestamp out
            self.assertTrue(TH._pair_attempt_allowed(addr))
        TH._pair_attempts[addr] = (TH._PAIR_MAX_ATTEMPTS_PER_PEER, 0.0)
        self.assertFalse(TH._pair_attempt_allowed(addr),
                         'a peer that never pairs must stop being paged')

    def test_peers_are_limited_independently(self):
        self.assertTrue(TH._pair_attempt_allowed('AA:BB:CC:DD:EE:01'))
        self.assertTrue(TH._pair_attempt_allowed('AA:BB:CC:DD:EE:02'))

    def test_empty_address_refused(self):
        self.assertFalse(TH._pair_attempt_allowed(''))
        self.assertFalse(TH._pair_attempt_allowed(None))


class InterestingPathTests(unittest.TestCase):

    def setUp(self):
        self._mac, self._paused = TH.client_mac_address, TH._paused_mac
        TH.client_mac_address = '00:00:5E:00:53:01'
        TH._paused_mac = None

    def tearDown(self):
        TH.client_mac_address, TH._paused_mac = self._mac, self._paused

    def test_connected_peer_is_interesting(self):
        self.assertTrue(TH._is_interesting_path(
            '/org/bluez/hci0/dev_00_00_5E_00_53_01'))

    def test_stranger_is_not(self):
        self.assertFalse(TH._is_interesting_path(
            '/org/bluez/hci0/dev_40_00_5E_00_53_10'))

    def test_paused_peer_is_interesting(self):
        TH.client_mac_address = None
        TH._paused_mac = '00:00:5E:00:53:02'
        self.assertTrue(TH._is_interesting_path(
            '/org/bluez/hci0/dev_00_00_5E_00_53_02'))

    def test_no_peer_means_nothing_is_interesting(self):
        TH.client_mac_address = None
        TH._paused_mac = None
        self.assertFalse(TH._is_interesting_path(
            '/org/bluez/hci0/dev_00_00_5E_00_53_01'))

    def test_empty_path_is_not_interesting(self):
        self.assertFalse(TH._is_interesting_path(None))
        self.assertFalse(TH._is_interesting_path(''))


class ThrottledLogTests(unittest.TestCase):

    def setUp(self):
        self.lines = []
        self._tlog = bh._tlog
        bh._tlog = self.lines.append
        bh._throttle_state.clear()

    def tearDown(self):
        bh._tlog = self._tlog
        bh._throttle_state.clear()

    def test_first_call_emits(self):
        bh._tlog_throttled('k', 'hello')
        self.assertEqual(self.lines, ['hello'])

    def test_repeat_within_interval_suppressed(self):
        for _ in range(100):
            bh._tlog_throttled('k', 'hello')
        self.assertEqual(len(self.lines), 1)

    def test_distinct_keys_each_emit(self):
        bh._tlog_throttled('a', 'one')
        bh._tlog_throttled('b', 'two')
        self.assertEqual(len(self.lines), 2)

    def test_suppressed_count_is_reported(self):
        bh._tlog_throttled('k', 'hello')
        for _ in range(9):
            bh._tlog_throttled('k', 'hello')
        # Age the entry out so the next call emits.
        last, suppressed = bh._throttle_state['k']
        bh._throttle_state['k'] = (last - bh._THROTTLE_INTERVAL_S - 1,
                                   suppressed)
        bh._tlog_throttled('k', 'hello')
        self.assertEqual(len(self.lines), 2)
        self.assertIn('+9 more', self.lines[-1],
                      'the log must not imply this happened once')

    def test_state_is_bounded(self):
        """Rotating LE addresses must not grow the throttle map."""
        for i in range(bh._THROTTLE_MAX_KEYS * 2):
            bh._tlog_throttled(f'key{i}', 'x')
        self.assertLessEqual(len(bh._throttle_state), bh._THROTTLE_MAX_KEYS)


if __name__ == '__main__':
    unittest.main(verbosity=2)
