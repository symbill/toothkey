#!/usr/bin/env python3
"""Run every Tooth-key test suite. No root, Bluetooth or display needed.

    python3 tests/run_all.py
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

if __name__ == '__main__':
    suite = unittest.defaultTestLoader.discover(HERE, pattern='test_*.py')
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
