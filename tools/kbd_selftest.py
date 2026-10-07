#!/usr/bin/env python3
"""Prove keyboard capture works on this machine, without Bluetooth.

Run as root:

    sudo python3 tools/kbd_selftest.py            # probe only, no grab
    sudo python3 tools/kbd_selftest.py --grab     # grab for 10s and
                                                  # decode what you type

`--grab` takes the same exclusive EVIOCGRAB the worker uses, so for its
duration your keystrokes go here and nowhere else — not to the focused
window, not to the compositor. That is the whole point: if the keys
show up below, capture works, and whether anything reaches the iPhone
is then purely a Bluetooth question.

Getting out, in order of preference:
  - the timeout expires on its own (default 10s, --seconds to change)
  - Ctrl+Alt+Shift+Esc, the same panic chord the worker honours
  - kill the process from another terminal; the kernel drops a grab
    when the file descriptor closes, so dying releases the keyboard

The grab cannot outlive this process.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import keyboard_evdev as ke
from evdev import ecodes as E


def _fmt(report: bytes) -> str:
    """Render a boot-keyboard report the way a HID sniffer would."""
    mod = report[2]
    names = [n for bit, n in (
        (1 << 0, 'LCtrl'), (1 << 1, 'LShift'), (1 << 2, 'LAlt'),
        (1 << 3, 'LMeta'), (1 << 4, 'RCtrl'), (1 << 5, 'RShift'),
        (1 << 6, 'RAlt'), (1 << 7, 'RMeta')) if mod & bit]
    keys = [f'0x{b:02x}' for b in report[4:] if b]
    return (f'{report.hex(" ")}   mod={"+".join(names) or "-"} '
            f'keys={",".join(keys) or "-"}')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--grab', action='store_true',
                    help='actually take the exclusive grab')
    ap.add_argument('--seconds', type=float, default=10.0,
                    help='how long to hold the grab (default 10)')
    args = ap.parse_args()

    print(f'euid={os.geteuid()}  '
          f'session={os.environ.get("XDG_SESSION_TYPE", "?")}')
    if os.geteuid() != 0:
        print('\nNot root. /dev/input/event* is root:input 0660, so '
              'nothing below will work.\nRe-run with sudo.')

    print('\n--- device probe ---')
    count = ke.describe_input_devices()
    if not count:
        return 1
    if not args.grab:
        print('\nProbe only. Re-run with --grab to capture keystrokes.')
        return 0

    print(f'\n--- grabbing for {args.seconds:.0f}s ---')
    print('Type something. Your keystrokes will NOT reach any other '
          'application.')
    print('Press Ctrl+Alt+Shift+Esc to stop early.\n')

    reports = []

    def on_report(report):
        reports.append(report)
        print('  ' + _fmt(report), flush=True)

    def on_chord(name):
        print(f'  >>> chord: {name}', flush=True)

    grabber = ke.KeyboardGrabber(on_report=on_report, on_chord=on_chord)
    if not grabber.start():
        print('FAILED: could not take the grab (see messages above)')
        return 1

    try:
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline and grabber.is_running():
            time.sleep(0.1)
    except KeyboardInterrupt:
        print('\ninterrupted')
    finally:
        grabber.stop()

    print(f'\n--- released. {len(reports)} report(s) produced ---')
    if not reports:
        print('No keystrokes were captured. If you did type, the grab '
              'is not seeing this keyboard —\ncheck which device the '
              'probe above chose.')
        return 1
    print('Capture works.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
