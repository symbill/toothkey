"""Tests for _read_desktop_clipboard: which helpers are asked, and what
a failure says.

The thing being pinned down here is the failure line: it has to name
every helper's failure. Under Wayland xclip is tried last and often
fails, and its error on its own hides the real cause when wl-paste is
missing.
"""

import io
import os
import sys
import subprocess
import unittest
import contextlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import keyboard_handler

_ENV_KEYS = ('DISPLAY', 'WAYLAND_DISPLAY')

_XCLIP_NO_OWNER = subprocess.CompletedProcess(
    args=[], returncode=1, stdout=b'',
    stderr=b'Error: target STRING not available\n')


class ClipboardReadTests(unittest.TestCase):

    def setUp(self):
        self._real_run = keyboard_handler.subprocess.run
        self._saved_env = {k: os.environ.get(k) for k in _ENV_KEYS}
        os.environ['DISPLAY'] = ':0'
        os.environ['WAYLAND_DISPLAY'] = 'wayland-0'
        # cmd[0] -> CompletedProcess, or an exception class to raise.
        self.outcomes = {}
        self.calls = []
        keyboard_handler.subprocess.run = self._fake_run
        self.out = io.StringIO()
        self._stdout = contextlib.redirect_stdout(self.out)
        self._stdout.__enter__()

    def tearDown(self):
        self._stdout.__exit__(None, None, None)
        keyboard_handler.subprocess.run = self._real_run
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _fake_run(self, cmd, **kwargs):
        self.calls.append(cmd)
        outcome = self.outcomes[cmd[0]]
        if isinstance(outcome, type) and issubclass(outcome, BaseException):
            raise outcome(cmd[0])
        return outcome

    def test_wayland_reads_text_through_wl_paste(self):
        self.outcomes['wl-paste'] = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b'hello', stderr=b'')
        self.assertEqual(keyboard_handler._read_desktop_clipboard(), 'hello')
        self.assertEqual(self.calls,
                         [['wl-paste', '--no-newline', '--type', 'text']])

    def test_missing_wl_paste_is_named_alongside_xclip_failure(self):
        self.outcomes['wl-paste'] = FileNotFoundError
        self.outcomes['xclip'] = _XCLIP_NO_OWNER
        self.assertIsNone(keyboard_handler._read_desktop_clipboard())
        line = self.out.getvalue()
        self.assertIn('wl-paste not installed (sudo apt install wl-clipboard)',
                      line)
        self.assertIn('xclip rc=1: Error: target STRING not available', line)
        self.assertLess(line.index('wl-paste'), line.index('xclip'))

    def test_xclip_answers_when_wl_paste_fails(self):
        self.outcomes['wl-paste'] = subprocess.CompletedProcess(
            args=[], returncode=1, stdout=b'', stderr=b'Nothing is copied\n')
        self.outcomes['xclip'] = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b'from x11', stderr=b'')
        self.assertEqual(keyboard_handler._read_desktop_clipboard(),
                         'from x11')
        self.assertNotIn('clipboard read failed', self.out.getvalue())

    def test_x11_session_only_asks_xclip(self):
        os.environ.pop('WAYLAND_DISPLAY')
        self.outcomes['xclip'] = _XCLIP_NO_OWNER
        self.assertIsNone(keyboard_handler._read_desktop_clipboard())
        self.assertEqual([c[0] for c in self.calls], ['xclip'])
        self.assertIn('xclip rc=1', self.out.getvalue())

    def test_no_display_at_all_asks_nothing(self):
        for k in _ENV_KEYS:
            os.environ.pop(k)
        self.assertIsNone(keyboard_handler._read_desktop_clipboard())
        self.assertEqual(self.calls, [])
        self.assertIn('no DISPLAY or WAYLAND_DISPLAY', self.out.getvalue())


if __name__ == '__main__':
    unittest.main()
