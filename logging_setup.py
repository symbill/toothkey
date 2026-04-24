"""Capture everything written to stdout/stderr (including subprocess output
from tools like bluetoothctl, hciconfig, etc.) into a timestamped log file.

Implementation note: we replace the process-level file descriptors 1 and 2
with a pipe we read on a background thread. That way any child process that
inherits fd 1/2 also flows through our timestamper — which is how we grab
`Changing power on succeeded` and friends from bluetoothctl into the log.
"""

import atexit
import os
import sys
import threading
import traceback
from datetime import datetime

# All runtime log files (toothkey.log, bluetoothd.log, hci_monitor.log, etc.)
# live under ./logs/ so they don't clutter the repo root and can be wiped as
# a single directory. The tray app's "Open log folder" menu item also opens
# exactly this path.
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
LOG_FILE = os.path.join(LOG_DIR, 'toothkey.log')


def _iso_now() -> bytes:
    return datetime.now().astimezone().isoformat(timespec='milliseconds').encode()


def _ensure_log_dir():
    # chmod 0o775 so an interactive (non-root) invocation can still read the
    # folder after it was first created by a sudo'd run.
    os.makedirs(LOG_DIR, mode=0o775, exist_ok=True)


def _open_log():
    # Unbuffered so everything hits disk immediately, even on crash.
    _ensure_log_dir()
    return open(LOG_FILE, 'ab', buffering=0)


def install():
    """Redirect fd 1/2 through a timestamping tee to both the original
    terminal and the log file. Safe to call exactly once at process start.
    """

    # Save originals so we can still write to the real terminal.
    try:
        orig_stdout_fd = os.dup(1)
    except OSError:
        orig_stdout_fd = None
    try:
        orig_stderr_fd = os.dup(2)
    except OSError:
        orig_stderr_fd = None

    orig_stdout = os.fdopen(orig_stdout_fd, 'wb', buffering=0) if orig_stdout_fd is not None else None
    orig_stderr = os.fdopen(orig_stderr_fd, 'wb', buffering=0) if orig_stderr_fd is not None else None

    # Create a pipe and route both fd 1 and fd 2 through it.
    pipe_r, pipe_w = os.pipe()
    os.dup2(pipe_w, 1)
    os.dup2(pipe_w, 2)
    os.close(pipe_w)

    # Rewire Python-level stdout/stderr to the (now redirected) fds.
    sys.stdout = os.fdopen(1, 'w', buffering=1, encoding='utf-8', errors='replace')
    sys.stderr = os.fdopen(2, 'w', buffering=1, encoding='utf-8', errors='replace')

    log_fh = _open_log()

    def _reader():
        buf = b''
        while True:
            try:
                chunk = os.read(pipe_r, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b'\n' in buf:
                line, buf = buf.split(b'\n', 1)
                stamped = _iso_now() + b' ' + line + b'\n'
                if orig_stdout is not None:
                    try: orig_stdout.write(stamped)
                    except Exception: pass
                try: log_fh.write(stamped)
                except Exception: pass

    reader_thread = threading.Thread(target=_reader, daemon=True, name='log-tee')
    reader_thread.start()

    # Drain the pipe on interpreter shutdown. Without this, a fast
    # sys.exit() (e.g. the early-return DISPLAY / tray-availability
    # checks in tray.py) races the daemon reader thread: Python kills
    # daemons abruptly before they read the last write, so the error
    # message the user most wanted to see (the one that explains WHY
    # the process is exiting) silently disappears. Closing fd 1 and
    # fd 2 drops pipe_w's refcount to 0, the reader sees EOF, and we
    # join briefly so it finishes writing to log_fh before exit().
    def _drain_on_exit():
        try: sys.stdout.flush()
        except Exception: pass
        try: sys.stderr.flush()
        except Exception: pass
        try: os.close(1)
        except OSError: pass
        try: os.close(2)
        except OSError: pass
        reader_thread.join(timeout=1.0)
    atexit.register(_drain_on_exit)

    # Session banner so multiple sessions are easy to tell apart in the file.
    pid = os.getpid()
    print(f'===== toothkey session start pid={pid} log={LOG_FILE} =====')

    # Any uncaught exception should land in the log too, not just die silently.
    #
    # Subtle bug we're dodging: sys.stdout/stderr are routed through a pipe
    # whose reader is a *daemon* thread. When Python propagates an unhandled
    # exception it prints the traceback and then exits — daemon threads get
    # killed immediately, often before they finish draining the pipe, which
    # previously left the log with just "Traceback (most recent call last):"
    # and no frames. Sidestep it by formatting the traceback into a string
    # and writing it directly to log_fh (which is unbuffered), then also
    # echo it to the original stderr so the user still sees it live.
    def _excepthook(exc_type, exc, tb):
        text = ''.join(traceback.format_exception(exc_type, exc, tb))
        header = b'--- uncaught exception ---\n'
        stamped_header = _iso_now() + b' ' + header
        try:
            log_fh.write(stamped_header)
            for line in text.splitlines():
                log_fh.write(_iso_now() + b' ' + line.encode('utf-8', 'replace') + b'\n')
            log_fh.flush()
        except Exception:
            pass
        if orig_stderr is not None:
            try:
                orig_stderr.write(stamped_header)
                orig_stderr.write(text.encode('utf-8', 'replace'))
            except Exception:
                pass

    sys.excepthook = _excepthook
