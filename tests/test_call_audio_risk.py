"""Tests for the call-audio hijack check.

This exists because of a real incident. On 2026-10-08 a live phone call
went silent: with toothkey holding a bond and paging the phone, BlueZ's
policy plugin connected audio profiles the moment the link came up, and
the phone moved the call to the computer. Nothing at this end said so —
the keyboard carried on working perfectly, which is exactly why "does
HID still bind" is the wrong question to judge adapter cleanliness by.

So these tests pin the two independent conditions that allow it, and
pin that a working keyboard is not evidence of safety.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bluetooth_handler as bh

# A keyboard-only adapter: HID plus the harmless identity records.
CLEAN = ['0x1124', '0x1200', '0x1800', '0x1801', '0x180a']
# The records that let a phone put a call through this machine.
HEADSET = ['0x1108', '0x1112', '0x111e', '0x111f']


def conf_with(body):
    fh = tempfile.NamedTemporaryFile('w', suffix='.conf', delete=False,
                                     encoding='utf-8')
    fh.write(body)
    fh.close()
    return fh.name


class ReconnectUuidsParsingTests(unittest.TestCase):
    """None means "BlueZ uses its default", which is the dangerous case."""

    def tearDown(self):
        for p in getattr(self, '_paths', []):
            try:
                os.unlink(p)
            except OSError:
                pass

    def parse(self, body):
        path = conf_with(body)
        self._paths = getattr(self, '_paths', []) + [path]
        return bh._reconnect_uuids_setting(path)

    def test_explicitly_empty_reads_as_empty(self):
        self.assertEqual(self.parse('[Policy]\nReconnectUUIDs=\n'), '')

    def test_empty_with_spaces_reads_as_empty(self):
        self.assertEqual(self.parse('[Policy]\nReconnectUUIDs =   \n'), '')

    def test_non_empty_is_returned(self):
        self.assertEqual(
            self.parse('[Policy]\nReconnectUUIDs=0000111f-x,0000110a-y\n'),
            '0000111f-x,0000110a-y')

    def test_commented_out_reads_as_absent(self):
        """A commented setting means the built-in default applies."""
        self.assertIsNone(
            self.parse('[Policy]\n#ReconnectUUIDs=0000111f-x\n'))

    def test_missing_reads_as_absent(self):
        self.assertIsNone(self.parse('[General]\nClass = 0x000540\n'))

    def test_prose_mentioning_the_name_is_not_a_setting(self):
        """main.conf's own comments discuss ReconnectUUIDs in prose."""
        body = ('[Policy]\n'
                '# The ReconnectUUIDs defines the set of remote services\n'
                '# that should try to be reconnected to.\n')
        self.assertIsNone(self.parse(body))

    def test_unreadable_file_reads_as_absent(self):
        self.assertIsNone(
            bh._reconnect_uuids_setting('/nonexistent/main.conf'))


class HijackRiskTests(unittest.TestCase):

    def setUp(self):
        self.safe = conf_with('[Policy]\nReconnectUUIDs=\n')
        self.defaulted = conf_with('[Policy]\n#ReconnectUUIDs=0000111f-x\n')

    def tearDown(self):
        for p in (self.safe, self.defaulted):
            try:
                os.unlink(p)
            except OSError:
                pass

    def test_clean_adapter_and_empty_reconnect_is_clear(self):
        self.assertEqual(
            bh._check_call_audio_hijack_risk(CLEAN, self.safe), [])

    def test_handsfree_on_the_adapter_is_flagged(self):
        problems = bh._check_call_audio_hijack_risk(
            CLEAN + ['0x111e', '0x111f'], self.safe)
        self.assertEqual(len(problems), 1)
        self.assertIn('call-audio', problems[0])

    def test_each_headset_record_is_flagged_on_its_own(self):
        for short in HEADSET:
            problems = bh._check_call_audio_hijack_risk(
                CLEAN + [short], self.safe)
            self.assertTrue(problems, f'{short} should be flagged')

    def test_default_reconnect_uuids_is_flagged(self):
        """The condition that actually caused the incident."""
        problems = bh._check_call_audio_hijack_risk(CLEAN, self.defaulted)
        self.assertEqual(len(problems), 1)
        self.assertIn('ReconnectUUIDs', problems[0])

    def test_non_empty_reconnect_uuids_is_flagged(self):
        path = conf_with('[Policy]\nReconnectUUIDs=0000111f-x\n')
        try:
            problems = bh._check_call_audio_hijack_risk(CLEAN, path)
            self.assertEqual(len(problems), 1)
            self.assertIn('policy', problems[0])
        finally:
            os.unlink(path)

    def test_both_conditions_are_reported_separately(self):
        problems = bh._check_call_audio_hijack_risk(
            CLEAN + ['0x111f'], self.defaulted)
        self.assertEqual(len(problems), 2,
                         'the two conditions are independent')

    def test_a2dp_alone_is_not_a_call_audio_risk(self):
        """Music records can't carry a call, so they are not flagged."""
        problems = bh._check_call_audio_hijack_risk(
            CLEAN + ['0x110a', '0x110b', '0x110c', '0x110e'], self.safe)
        self.assertEqual(problems, [])

    def test_obex_alone_is_not_a_call_audio_risk(self):
        problems = bh._check_call_audio_hijack_risk(
            CLEAN + ['0x1105', '0x1106', '0x112f'], self.safe)
        self.assertEqual(problems, [])

    def test_a_working_keyboard_is_not_evidence_of_safety(self):
        """HID present and call-audio present are independent facts.

        Judging the adapter by whether iOS still pairs is what let this
        go unnoticed; the check must flag a hazard on an adapter whose
        HID record is perfectly in place.
        """
        adapter = CLEAN + ['0x111f']
        self.assertIn('0x1124', adapter, 'HID is still advertised')
        self.assertTrue(
            bh._check_call_audio_hijack_risk(adapter, self.safe))


class HazardSetTests(unittest.TestCase):

    def test_hazard_set_is_a_subset_of_the_forbidden_set(self):
        self.assertTrue(
            bh._CALL_AUDIO_UUID_SHORTS
            <= bh._FORBIDDEN_ADAPTER_UUID_SHORTS,
            'every call-audio record should also count as pollution')

    def test_hazard_set_covers_both_sides_of_both_profiles(self):
        """Headset and Handsfree, unit and gateway roles alike."""
        for short in ('0x1108', '0x1112', '0x111e', '0x111f'):
            self.assertIn(short, bh._CALL_AUDIO_UUID_SHORTS)

    def test_hazard_records_are_attributed_to_an_owner(self):
        for short in bh._CALL_AUDIO_UUID_SHORTS:
            self.assertIn(short, bh._UUID_OWNERS)


if __name__ == '__main__':
    unittest.main(verbosity=2)
