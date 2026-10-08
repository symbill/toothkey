"""Tests for the worker's tray link: it must outlive any one tray.

The worker owns the Bluetooth link and is meant to survive the tray
being restarted, the user logging out and back in, and the tray
crashing. That only works if it keeps accepting new trays — closing the
listening socket after the first one leaves a healthy worker that every
later tray reports as unreachable.
"""

import os
import socket
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# worker.py installs logging at import time. Send it to a scratch dir so
# these tests don't append to the live logs/toothkey.log.
os.environ.setdefault('TOOTHKEY_LOG_DIR',
                      tempfile.mkdtemp(prefix='toothkey-test-logs-'))


class FakeServer:
    """Stands in for the listening socket, handing out scripted conns."""

    def __init__(self, conns):
        self._conns = list(conns)
        self.timeouts = []
        self.closed = False
        self.accepts = 0

    def settimeout(self, t):
        self.timeouts.append(t)

    def accept(self):
        self.accepts += 1
        if self._conns:
            return self._conns.pop(0), None
        raise socket.timeout()

    def close(self):
        self.closed = True


class FakeConn:
    """A tray connection whose command stream ends immediately."""

    def __init__(self, lines=()):
        self._lines = list(lines)
        self.closed = False
        self.written = []

    def settimeout(self, _t):
        pass

    def makefile(self, mode, encoding=None):
        conn = self

        class FH:
            def __iter__(self_inner):
                return iter(conn._lines)

            def write(self_inner, data):
                conn.written.append(data)

            def flush(self_inner):
                pass

            def close(self_inner):
                pass
        return FH()

    def close(self):
        self.closed = True


class TrayLinkLoopTests(unittest.TestCase):

    def setUp(self):
        import worker
        self.worker = worker
        worker._shutdown.clear()
        self._out = worker._out_fh
        worker._out_fh = None

    def tearDown(self):
        self.worker._out_fh = self._out
        self.worker._shutdown.clear()

    def test_serves_a_second_tray_after_the_first_disconnects(self):
        """The regression: one tray per worker left later trays stranded."""
        first, second = FakeConn(), FakeConn()
        server = FakeServer([second])

        t = threading.Thread(
            target=self.worker._tray_link_loop, args=(server, first),
            daemon=True)
        t.start()
        deadline = time.monotonic() + 3.0
        while not second.closed and time.monotonic() < deadline:
            time.sleep(0.02)
        self.worker._shutdown.set()
        t.join(timeout=3.0)

        self.assertTrue(first.closed, 'first tray connection closed')
        self.assertTrue(second.closed, 'second tray was served too')

    def test_listening_socket_is_not_closed_when_a_tray_leaves(self):
        first = FakeConn()
        server = FakeServer([])
        t = threading.Thread(
            target=self.worker._tray_link_loop, args=(server, first),
            daemon=True)
        t.start()
        time.sleep(0.5)
        self.assertFalse(server.closed,
                         'the worker must keep accepting trays')
        self.worker._shutdown.set()
        t.join(timeout=3.0)

    def test_accept_uses_a_timeout_so_shutdown_is_noticed(self):
        first = FakeConn()
        server = FakeServer([])
        t = threading.Thread(
            target=self.worker._tray_link_loop, args=(server, first),
            daemon=True)
        t.start()
        time.sleep(0.4)
        self.worker._shutdown.set()
        t.join(timeout=3.0)
        self.assertFalse(t.is_alive(), 'loop must end on shutdown')
        self.assertTrue(any(x is not None for x in server.timeouts),
                        'a blocking accept would never see shutdown')

    def test_loop_ends_if_the_listening_socket_dies(self):
        class DeadServer(FakeServer):
            def accept(self):
                raise OSError('socket closed')

        t = threading.Thread(
            target=self.worker._tray_link_loop,
            args=(DeadServer([]), FakeConn()), daemon=True)
        t.start()
        t.join(timeout=3.0)
        self.assertFalse(t.is_alive())


class ServeTrayTests(unittest.TestCase):

    def setUp(self):
        import worker
        self.worker = worker
        worker._shutdown.clear()
        self._out = worker._out_fh
        worker._out_fh = None

    def tearDown(self):
        self.worker._out_fh = self._out
        self.worker._shutdown.clear()

    def test_sends_hello_and_an_unprompted_state(self):
        """A reconnecting tray must not wait for the next change.

        The state poller only emits on change, so without an explicit
        push a tray that reconnects to a long-running worker would sit
        on its startup "disconnected" icon indefinitely.
        """
        conn = FakeConn()
        self.worker._serve_tray(conn)
        sent = ''.join(conn.written)
        self.assertIn('"hello"', sent)
        self.assertIn('"state"', sent)

    def test_clears_the_out_handle_on_the_way_out(self):
        """A stale handle would make _send write into a dead socket."""
        self.worker._serve_tray(FakeConn())
        self.assertIsNone(self.worker._out_fh)

    def test_closes_the_connection(self):
        conn = FakeConn()
        self.worker._serve_tray(conn)
        self.assertTrue(conn.closed)

    def test_a_reset_is_not_treated_as_a_crash(self):
        """A killed tray resets the socket; that is routine, not a fault.

        It must not print a traceback, and the connection must still be
        cleaned up so the next tray can be served.
        """
        class ResettingConn(FakeConn):
            def makefile(self, mode, encoding=None):
                outer = self

                class FH:
                    def __iter__(self_inner):
                        raise ConnectionResetError(104, 'reset by peer')

                    def write(self_inner, data):
                        outer.written.append(data)

                    def flush(self_inner):
                        pass

                    def close(self_inner):
                        pass
                return FH()

        conn = ResettingConn()
        self.worker._serve_tray(conn)   # must not raise
        self.assertTrue(conn.closed)
        self.assertIsNone(self.worker._out_fh)


if __name__ == '__main__':
    unittest.main(verbosity=2)
