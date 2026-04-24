"""Set a distinctive process title so the long-lived processes this
project spawns are easy to find with `ps aux | grep toothkey` or
`pgrep toothkey`.

Two backends, tried in order:

1. `setproctitle` (pip / `python3-setproctitle`)
   Rewrites argv[0..] in place, which is what `ps aux`, `ps -ef`,
   `pgrep -f`, and `/proc/PID/cmdline` all read. This is the
   robust, user-visible path.

2. Linux `prctl(PR_SET_NAME, ...)` via ctypes
   Only updates the kernel's per-thread "comm" name (capped at 15
   chars). That shows up in `pgrep` (without -f), `ps -o comm`,
   and `/proc/PID/comm`, but `ps aux`'s CMD column still shows
   the original argv. Good enough as a fallback so the very first
   run (before start.sh's ensure_python_deps installs
   setproctitle) isn't invisible to `pgrep`.

Always no-ops silently on failure \u2014 a missing proc title is a
cosmetic issue, never a reason to crash the app.
"""

import ctypes


# Kernel-exposed constant for prctl(2). Hard-coded rather than read
# from <sys/prctl.h> because we don't want a ctypes-based fallback
# to drag in build-time headers.
_PR_SET_NAME = 15


def set_title(title: str) -> None:
    # Prefer setproctitle because it updates the argv-visible name,
    # which is what users actually see in `ps aux`.
    try:
        import setproctitle  # type: ignore[import-not-found]
        setproctitle.setproctitle(title)
        return
    except Exception:
        # ImportError on first run, or any odd runtime failure \u2014
        # fall through to prctl so we still tag the comm name.
        pass

    try:
        libc = ctypes.CDLL('libc.so.6', use_errno=True)
        # Per man prctl(2): name buffer must be <=16 bytes including
        # the trailing NUL, i.e. 15 chars + NUL. Longer titles get
        # silently truncated by the kernel, but we pre-truncate so
        # the visible 15-char slice still starts with "toothkey".
        name = title.encode('utf-8', 'replace')[:15] + b'\0'
        libc.prctl(_PR_SET_NAME, name, 0, 0, 0)
    except Exception:
        pass
