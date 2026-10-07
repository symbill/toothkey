"""Window-manager hints that Qt has no API for.

Qt can ask for a window to be frameless and to stay above others, but
it cannot say "keep this one out of the taskbar and the pager". That is
an EWMH window state, `_NET_WM_STATE_SKIP_TASKBAR`, and the only way to
set it is to talk to the X server directly.

Tooth-key needs it for the two windows it floats over the desktop — the
grab indicator and the toast. Neither is something anyone should be
able to alt-tab to or click in a task manager; they are on-screen
status, not applications.

The window type is not enough on its own. On KWin 6, neither
`_NET_WM_WINDOW_TYPE_UTILITY` (what Qt gives a `Qt.Tool`) nor
`_NET_WM_WINDOW_TYPE_NOTIFICATION` (what the toast asks for via
`WA_X11NetWmWindowTypeNotification`) implies skip-taskbar: KWin reports
`skipTaskbar=false` for both, and Plasma's task manager shows a button
accordingly. Setting the state explicitly is what actually works.

Making the window override-redirect instead — `Qt.ToolTip`, or
`Qt.X11BypassWindowManagerHint` — also keeps it out of the taskbar,
because the window manager then doesn't manage it at all. That is the
wrong trade here: an unmanaged window no longer respects the things a
managed one does, and with the bypass hint KWin stops delivering mouse
input to it, which would break clicking the indicator to ungrab. These
windows stay managed.

Everything here degrades to a no-op rather than failing: on Wayland
there is no `_NET_WM_STATE` to set, and python-xlib may not be
installed. The caller checks the platform; this module just refuses
quietly if it can't do the job.
"""

try:
    from Xlib import X, Xatom
    from Xlib import display as _xdisplay
    from Xlib.protocol import event as _xevent
    _IMPORT_ERROR = None
except Exception as exc:          # pragma: no cover - depends on the box
    X = Xatom = _xdisplay = _xevent = None
    _IMPORT_ERROR = exc

# _NET_WM_STATE client-message actions, from the EWMH spec.
_NET_WM_STATE_ADD = 1

# One Xlib connection for the life of the process, opened on first use.
# Separate from Qt's own connection to the same server, which is fine —
# we only ever write properties and post one client message.
_display = None
_display_error = None


def unavailable_reason():
    """Why these hints can't be applied, or None if they can."""
    if _IMPORT_ERROR is not None:
        return f'python-xlib not available ({_IMPORT_ERROR})'
    if _display_error is not None:
        return f'cannot reach the X server ({_display_error})'
    return None


def _get_display():
    global _display, _display_error
    if _display is not None:
        return _display
    if _xdisplay is None:
        return None
    try:
        _display = _xdisplay.Display()
    except Exception as exc:
        _display_error = exc
        return None
    return _display


def skip_taskbar_and_pager(window_id) -> bool:
    """Keep the X window `window_id` out of the taskbar, pager and
    window switcher.

    Safe to call before the window is mapped, after it is mapped, and
    repeatedly — all three happen in normal use, and each needs a
    different half of this:

      - Writing the `_NET_WM_STATE` property while the window is still
        unmapped is how a client requests initial state; the window
        manager reads it when it maps the window, so no taskbar button
        appears even for a frame.
      - Once the window is mapped the property is the window manager's
        to own, and it only acts on a client message. Hence both.

    Returns True if the request was sent. Never raises: a missing
    taskbar hint is a cosmetic problem and must not take down a tray
    icon or swallow a toast.
    """
    d = _get_display()
    if d is None:
        return False
    try:
        wid = int(window_id)
    except (TypeError, ValueError):
        return False
    if wid <= 0:
        return False

    try:
        win = d.create_resource_object('window', wid)
        state = d.intern_atom('_NET_WM_STATE')
        skip_taskbar = d.intern_atom('_NET_WM_STATE_SKIP_TASKBAR')
        skip_pager = d.intern_atom('_NET_WM_STATE_SKIP_PAGER')
        # KDE extension, and the reason Alt+Tab would otherwise stop on
        # a floating status window. Ignored by window managers that
        # don't know it.
        skip_switcher = d.intern_atom('_KDE_NET_WM_STATE_SKIP_SWITCHER')
        wanted = (skip_taskbar, skip_pager, skip_switcher)

        # Merge rather than overwrite: Qt puts its own states here
        # (_NET_WM_STATE_ABOVE for WindowStaysOnTopHint, among others)
        # and replacing the property wholesale would drop them.
        existing = []
        prop = win.get_full_property(state, Xatom.ATOM)
        if prop is not None:
            existing = list(prop.value)
        merged = existing + [a for a in wanted if a not in existing]
        if merged != existing:
            win.change_property(state, Xatom.ATOM, 32, merged)

        # A _NET_WM_STATE message carries at most two property atoms,
        # so three states take two messages.
        root = d.screen().root
        mask = X.SubstructureRedirectMask | X.SubstructureNotifyMask
        for first, second in ((skip_taskbar, skip_pager),
                              (skip_switcher, 0)):
            ev = _xevent.ClientMessage(
                window=win,
                client_type=state,
                # action, first property, second property, source
                # indication (1 = a normal application), unused.
                data=(32, [_NET_WM_STATE_ADD, first, second, 1, 0]),
            )
            root.send_event(ev, event_mask=mask)
        d.flush()
        return True
    except Exception:
        return False
