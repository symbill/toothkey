"""System-tray UI for Tooth-key (runs as the invoking user).

Process topology (both processes are started by start.sh as direct
siblings — neither spawns the other):

    user's shell
      ├─ sudo python3 worker.py --socket <UDS>       (root; BT + HID + pynput)
      └─ python3 tray.py --socket <UDS>              (user; this file)

Why split?
    QSystemTrayIcon on Kubuntu is the org.kde.StatusNotifierItem DBus
    protocol hosted on the user's session bus. Plasma's systray widget
    silently ignores SNI registrations whose peer UID doesn't match
    the session owner — so running the tray as root (via sudo) made
    the icon invisible even though isSystemTrayAvailable() said yes.

    The worker owns everything that needs CAP_NET_RAW (L2CAP sockets)
    or DBus-to-BlueZ (agent / profile registration), and talks to the
    tray over a Unix domain socket with a tiny NDJSON protocol.

Why start.sh spawns both instead of tray spawning worker:
    The tray needs to be detached (setsid) so the user's shell returns
    immediately after launch. Once detached, the tray has no controlling
    tty, so `sudo -n` from inside the tray can't find the credential
    timestamp the user's `sudo -v` primed (Ubuntu's sudo defaults to
    timestamp_type=tty). Running sudo from start.sh — which DOES still
    have a tty — sidesteps this entirely, and lets us prompt normally.

Lifecycle:
    - __init__ gets a `--socket` path from start.sh and hands it to
      WorkerLink, which polls-connects to the worker (already spawned
      and listening).
    - A reader thread pulls NDJSON from the socket, translates each
      message into a pyqtSignal so the UI thread can react safely.
    - Qt event loop runs the UI. Exit menu sends {"type":"shutdown"};
      tray reads EOF when the worker exits, then quits itself.

Menu contract (per the original spec):
    - if connected       : "Disconnect <name>"
    - if not grab-mode   : "Grab keyboard"
    - if grab-mode       : "Ungrab keyboard"
    - always             : "Open log folder"
    - always             : "Exit" (signals the worker to clean up)
"""

# ---------------------------------------------------------------------------
# EARLY-BOOT DIAGNOSTICS
#
# The tray has repeatedly died silently — no bootstrap-log output, no
# toothkey.log output, nothing. logging_setup routes stdout/stderr through
# a pipe drained by a daemon thread, which loses messages on fast exits;
# bootstrap-log redirection also eats pre-crash buffered data sometimes.
# So before touching ANYTHING that could fail or buffer, we open a tiny
# dedicated log file in append mode and dump signposts synchronously at
# every non-trivial stage. If the tray dies, this file is the source of
# truth for where it got to.
# ---------------------------------------------------------------------------
import os
import sys
_TRAY_DIAG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'logs', 'tray-diag.log')
try:
    os.makedirs(os.path.dirname(_TRAY_DIAG_PATH), mode=0o775, exist_ok=True)
except Exception:
    pass
try:
    _tray_diag = open(_TRAY_DIAG_PATH, 'ab', buffering=0)
except Exception:
    _tray_diag = None

def _diag(msg: str) -> None:
    """Write one timestamped line to tray-diag.log. Unbuffered, bypasses
    logging_setup. Must not raise."""
    if _tray_diag is None:
        return
    try:
        from datetime import datetime as _dt
        ts = _dt.now().astimezone().isoformat(timespec='milliseconds')
        _tray_diag.write(f'{ts} [tray-diag pid={os.getpid()}] {msg}\n'.encode('utf-8', 'replace'))
    except Exception:
        pass

_diag(f'enter: argv={sys.argv!r}')
_diag(f'env: DISPLAY={os.environ.get("DISPLAY")!r} '
      f'WAYLAND_DISPLAY={os.environ.get("WAYLAND_DISPLAY")!r} '
      f'XDG_RUNTIME_DIR={os.environ.get("XDG_RUNTIME_DIR")!r} '
      f'XDG_CURRENT_DESKTOP={os.environ.get("XDG_CURRENT_DESKTOP")!r} '
      f'KDE_FULL_SESSION={os.environ.get("KDE_FULL_SESSION")!r} '
      f'DBUS_SESSION_BUS_ADDRESS={os.environ.get("DBUS_SESSION_BUS_ADDRESS")!r} '
      f'HOME={os.environ.get("HOME")!r} USER={os.environ.get("USER")!r}')
_diag(f'python={sys.executable} ver={sys.version.split()[0]} '
      f'uid={os.getuid()} euid={os.geteuid()}')

try:
    _diag('importing proc_title')
    from proc_title import set_title
    set_title('toothkey-tray')
    _diag('set_title ok')
except Exception as _e:
    _diag(f'proc_title failed: {type(_e).__name__}: {_e}')
    # non-fatal; continue

try:
    _diag('importing logging_setup')
    import logging_setup
    _diag('calling logging_setup.install()')
    logging_setup.install()
    _diag('logging_setup.install() ok')
except Exception as _e:
    _diag(f'logging_setup failed: {type(_e).__name__}: {_e}')
    raise

_diag('importing stdlib (argparse/json/socket/...)')
import argparse
import json
import socket
import subprocess
import threading
import time

_diag('importing PyQt5.QtCore')
from PyQt5.QtCore import QObject, QTimer, Qt, pyqtSignal
_diag('importing PyQt5.QtGui')
from PyQt5.QtGui import (
    QBrush, QColor, QFont, QIcon, QPainter, QPainterPath,
    QPen, QPixmap, QTextDocument,
)
_diag('importing PyQt5.QtSvg')
from PyQt5.QtSvg import QSvgRenderer
_diag('importing PyQt5.QtWidgets')
from PyQt5.QtWidgets import (
    QApplication, QMenu, QSystemTrayIcon, QWidget,
)
_diag('PyQt5 imports ok')

from logging_setup import LOG_DIR

HERE = os.path.dirname(os.path.abspath(__file__))
ICON_SVG_PATH = os.path.join(HERE, 'toothkey.svg')

# Pixmap sizes we cache. Qt picks the best-matching one when the tray
# asks for a specific size; supplying several avoids blurry downscaling
# on hi-DPI displays and keeps the 16px variant sharp.
ICON_SIZES = (16, 22, 24, 32, 48, 64, 128, 256)

# Red X overlay geometry (in the SVG's 64x64 viewBox coordinates).
X_STROKE_COLOR = QColor('#e23c3c')
X_OUTLINE_COLOR = QColor('#5a0d0d')
X_STROKE_WIDTH_FRAC = 0.11   # as fraction of icon edge length
X_INSET_FRAC = 0.15          # how far the X stays from the icon edges

# Tint colour for the "grab on" tray icon — a saturated green that reads
# well at 16 px against both light and dark panel backgrounds.
GRAB_TINT_COLOR = QColor('#28b84a')


# ---------------------------------------------------------------------------
# icon building
# ---------------------------------------------------------------------------

def _render_svg_pixmap(renderer: QSvgRenderer, size: int) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    renderer.render(painter)
    painter.end()
    return pm


def _paint_red_x(pm: QPixmap) -> QPixmap:
    """Dark outline + bright red cross on top of `pm`, so the X stays
    legible against both light and dark tooth fills at small sizes.
    """
    out = QPixmap(pm)
    size = out.width()
    inset = size * X_INSET_FRAC
    stroke = max(2, round(size * X_STROKE_WIDTH_FRAC))

    painter = QPainter(out)
    painter.setRenderHint(QPainter.Antialiasing, True)

    outline_pen = QPen(X_OUTLINE_COLOR, stroke + 2)
    outline_pen.setCapStyle(Qt.RoundCap)
    painter.setPen(outline_pen)
    painter.drawLine(int(inset), int(inset),
                     int(size - inset), int(size - inset))
    painter.drawLine(int(size - inset), int(inset),
                     int(inset), int(size - inset))

    fill_pen = QPen(X_STROKE_COLOR, stroke)
    fill_pen.setCapStyle(Qt.RoundCap)
    painter.setPen(fill_pen)
    painter.drawLine(int(inset), int(inset),
                     int(size - inset), int(size - inset))
    painter.drawLine(int(size - inset), int(inset),
                     int(inset), int(size - inset))
    painter.end()
    return out


def _paint_tinted(pm: QPixmap, color: QColor) -> QPixmap:
    """Return a copy of pm with its non-transparent pixels flooded with
    `color`, preserving the original alpha channel. Used for the "grab on"
    green tooth tray icon.

    Works by painting `color` over the full bounding rect using
    CompositionMode_SourceIn, which keeps only the parts of the source
    (the color fill) where the destination (the tooth shape) is opaque.
    """
    out = QPixmap(pm)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setCompositionMode(QPainter.CompositionMode_SourceIn)
    painter.fillRect(out.rect(), color)
    painter.end()
    return out


def _paint_dimmed(pm: QPixmap, opacity: float = 0.35) -> QPixmap:
    """Return a faded copy of pm, used for the 'shutting down' icon so
    the user sees instant feedback that the Exit click actually took.
    """
    out = QPixmap(pm.size())
    out.fill(Qt.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setOpacity(opacity)
    painter.drawPixmap(0, 0, pm)
    painter.end()
    return out


def build_icons() -> tuple:
    """Build the tray icon variants we render:
      - connected     : plain tooth (white/default fill)
      - disconnected  : tooth + red X
      - grabbed       : green-tinted tooth (keyboard-grab is on)
      - shutting_down : faded tooth (Exit in progress)

    Returns (connected, disconnected, grabbed, shutting_down).
    """
    renderer = QSvgRenderer(ICON_SVG_PATH)
    if not renderer.isValid():
        raise RuntimeError(f'failed to load SVG icon at {ICON_SVG_PATH}')
    connected = QIcon()
    disconnected = QIcon()
    grabbed = QIcon()
    shutting_down = QIcon()
    for size in ICON_SIZES:
        base = _render_svg_pixmap(renderer, size)
        connected.addPixmap(base)
        disconnected.addPixmap(_paint_red_x(base))
        grabbed.addPixmap(_paint_tinted(base, GRAB_TINT_COLOR))
        shutting_down.addPixmap(_paint_dimmed(base))
    return connected, disconnected, grabbed, shutting_down


# ---------------------------------------------------------------------------
# floating grab indicator
# ---------------------------------------------------------------------------

# Visible size of the floating tooth (logical px before HiDPI scaling).
GRAB_INDICATOR_SIZE = 100
# Margin from the top-right corner of the primary screen's available area
# (i.e. excluding panels / taskbars — we get whatever Qt reports).
GRAB_INDICATOR_MARGIN = 16
# Pulse animation period while the grab is "pending" (user clicked Grab
# but the worker hasn't confirmed grab=True yet). A full breath cycle.
GRAB_PULSE_PERIOD_MS = 900
# Opacity bounds the pulse animation oscillates between.
GRAB_PULSE_MIN_OPACITY = 0.35
GRAB_PULSE_MAX_OPACITY = 1.00


class GrabIndicator(QWidget):
    """A small always-on-top tooth shown at the top-right of the screen
    whenever keyboard grab is on (or pending).

    - State "pending": widget is visible and its opacity oscillates
      between GRAB_PULSE_MIN_OPACITY and GRAB_PULSE_MAX_OPACITY on a
      GRAB_PULSE_PERIOD_MS cycle, signalling "working on it". Used
      between the user clicking Grab and the worker confirming.
    - State "active":  widget is visible at full opacity — grab is on.
    - State "off":     widget is hidden.

    Clicking the widget emits `clicked`, which Tray wires to the same
    code path as "Ungrab keyboard" in the menu.
    """

    clicked = pyqtSignal()

    def __init__(self, tooth_pixmap: QPixmap, parent: QObject = None):
        # Qt.Tool keeps it off the taskbar; Qt.FramelessWindowHint removes
        # the window chrome; Qt.WindowStaysOnTopHint keeps it above other
        # windows; Qt.WA_TranslucentBackground lets us paint with alpha.
        super().__init__(
            None,
            Qt.Tool
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_AlwaysStackOnTop, True)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip('Tooth-key: keyboard grabbed — click to release')
        self.setFixedSize(GRAB_INDICATOR_SIZE, GRAB_INDICATOR_SIZE)

        # Pre-render the tooth scaled to our display size.
        self._pixmap = tooth_pixmap.scaled(
            GRAB_INDICATOR_SIZE, GRAB_INDICATOR_SIZE,
            Qt.KeepAspectRatio, Qt.SmoothTransformation)

        self._opacity = GRAB_PULSE_MAX_OPACITY
        self._pending = False

        # Drive the pulse animation via a repaint timer. Opacity is
        # computed each tick from monotonic time so pausing/unpausing
        # stays visually continuous without extra state.
        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(33)  # ~30 fps
        self._pulse_timer.timeout.connect(self._on_pulse_tick)
        self._pulse_start_ms = 0

    # ------------------------- public API --------------------------

    def show_pending(self) -> None:
        """Show the widget and start the breathing animation."""
        self._pending = True
        self.setToolTip('Tooth-key: grabbing keyboard... (click to cancel)')
        self._pulse_start_ms = int(time.monotonic() * 1000)
        if not self._pulse_timer.isActive():
            self._pulse_timer.start()
        self._position_top_right()
        if not self.isVisible():
            self.show()
        self.raise_()
        self.update()

    def show_active(self) -> None:
        """Show the widget at full opacity — grab is confirmed on."""
        self._pending = False
        self.setToolTip('Tooth-key: keyboard grabbed — click to release')
        if self._pulse_timer.isActive():
            self._pulse_timer.stop()
        self._opacity = GRAB_PULSE_MAX_OPACITY
        self._position_top_right()
        if not self.isVisible():
            self.show()
        self.raise_()
        self.update()

    def hide_indicator(self) -> None:
        """Hide the widget and stop animating."""
        self._pending = False
        if self._pulse_timer.isActive():
            self._pulse_timer.stop()
        if self.isVisible():
            self.hide()

    # ------------------------- internals ---------------------------

    def _position_top_right(self) -> None:
        app = QApplication.instance()
        if app is None:
            return
        screen = app.primaryScreen()
        if screen is None:
            return
        # availableGeometry excludes panels / taskbars where Qt can
        # detect them (works on KDE / GNOME); falls back to the full
        # screen rect otherwise.
        rect = screen.availableGeometry()
        x = rect.right() - self.width() - GRAB_INDICATOR_MARGIN
        y = rect.top() + GRAB_INDICATOR_MARGIN
        self.move(x, y)

    def _on_pulse_tick(self) -> None:
        # Sinusoidal opacity in [MIN, MAX] with period PULSE_PERIOD_MS.
        import math
        elapsed = int(time.monotonic() * 1000) - self._pulse_start_ms
        phase = (elapsed % GRAB_PULSE_PERIOD_MS) / GRAB_PULSE_PERIOD_MS
        # cosine goes 1 -> -1 -> 1, map to [0, 1] then to [MIN, MAX].
        amp = (1 - math.cos(phase * 2 * math.pi)) / 2
        self._opacity = (
            GRAB_PULSE_MIN_OPACITY
            + amp * (GRAB_PULSE_MAX_OPACITY - GRAB_PULSE_MIN_OPACITY)
        )
        self.update()

    # ------------------------- Qt overrides ------------------------

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.setOpacity(self._opacity)
        painter.drawPixmap(0, 0, self._pixmap)
        painter.end()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)


# ---------------------------------------------------------------------------
# transient toast
# ---------------------------------------------------------------------------

# Toast geometry + display timing. Centred on the primary screen's
# available geometry (excluding panels/taskbars). Well out of the way
# of the GrabIndicator in the top-right corner.
TOAST_DURATION_MS = 2000
TOAST_PADDING_X = 22
TOAST_PADDING_Y = 12
TOAST_FONT_POINT_SIZE = 12


class Toast(QWidget):
    """A frameless, always-on-top transient notification.

    Used for short "you just did X" confirmations (e.g. entering grab
    mode). Shown for TOAST_DURATION_MS, then hides itself. Calling
    show_message() again while a toast is already visible restarts the
    timer with the new text — no stacking, no queue.

    Implementation notes: three bullet-proof mechanics stacked on top
    of each other, because toasts on Plasma Wayland have a history of
    mysteriously not painting:

      1. Qt.SplashScreen window type. The Wayland spec has a specific
         "splash" role which compositors (mutter, kwin) know to map
         immediately, unlike Qt.Tool which relies on xdg-shell popup
         heuristics and sometimes silently fails. Qt.Tool + Frameless
         can produce an invisible but present window; SplashScreen
         gives us reliable mapping on both X11 and Wayland.
      2. Solid (non-translucent) background. WA_TranslucentBackground
         requires the compositor to honour per-pixel alpha on our
         buffer; some kwin-Wayland sessions silently fall back to a
         fully transparent buffer. We paint a solid dark pill instead.
      3. Belt-and-suspenders: every show_message() invocation also
         renders the toast to a PNG in logs/ (for post-mortem visual
         confirmation that paint worked even if screen didn't update)
         and logs geometry + visibility flags after show().

    Why not QSystemTrayIcon.showMessage? That fires a libnotify
    notification that ends up in the KDE/Gnome notification DRAWER
    (persistent history). It's also throttled / suppressed by Do Not
    Disturb and by Plasma's "notification history already shown"
    dedup. For a 2-second "you just pressed grab" nudge we want
    something that ALWAYS appears immediately.
    """

    _BG_COLOR = QColor(30, 30, 30, 255)
    _BORDER_COLOR = QColor(255, 255, 255, 60)
    _TEXT_COLOR = QColor(255, 255, 255, 255)
    _BORDER_RADIUS = 10

    def __init__(self, parent: QObject = None):
        # Window-type choice has a tricky history here:
        #
        #   Qt.Tool / Qt.SplashScreen: reliably mapped on X11 + Wayland,
        #     BUT on Plasma Wayland kwin ignores client-driven move()
        #     for these roles and auto-places them (top-right corner),
        #     so we couldn't centre the toast despite Qt reporting the
        #     correct geometry internally.
        #
        #   Qt.Dialog: also reliably mapped, and kwin DOES honour
        #     client-driven positioning for dialog-role surfaces.
        #     By default dialogs are modal; we drop that with
        #     WindowModality=NonModal and the NotModal hint.
        #     This is the current choice.
        super().__init__(
            None,
            Qt.Dialog
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint,
        )
        self.setWindowModality(Qt.NonModal)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_AlwaysStackOnTop, True)
        # NO WA_TranslucentBackground: we paint a fully opaque pill
        # and live with square window corners. Per-pixel alpha on a
        # SplashScreen-role surface isn't reliable across compositors.

        self._doc = QTextDocument(self)
        font = QFont()
        font.setPointSize(TOAST_FONT_POINT_SIZE)
        self._doc.setDefaultFont(font)
        self._doc.setDocumentMargin(0)
        # White text for Rich-text rendering.
        self._doc.setDefaultStyleSheet('body { color: #ffffff; }')

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)

    # ------------------------- public API --------------------------

    def show_message(self, message_html: str,
                     duration_ms: int = TOAST_DURATION_MS) -> None:
        """Display `message_html` (HTML-formatted) for `duration_ms`.
        Re-shows and restarts the timer if already visible.
        """
        print(f'[toast] show_message: {message_html!r} duration={duration_ms}ms')
        self._doc.setHtml(message_html)
        # Unbounded width first so we can measure the natural single-
        # line width of the rendered HTML; then pad to widget size.
        self._doc.setTextWidth(-1)
        text_size = self._doc.size()
        w = int(text_size.width()) + TOAST_PADDING_X * 2
        h = int(text_size.height()) + TOAST_PADDING_Y * 2
        print(f'[toast] natural text size={text_size.width():.1f}x'
              f'{text_size.height():.1f} -> widget {w}x{h}')
        self.resize(w, h)

        # Centre on the primary screen (both axes). availableGeometry
        # excludes panels / taskbars where the platform reports them,
        # so "centre" lands in the usable area, not behind a panel.
        screen = QApplication.primaryScreen()
        target_x = target_y = 0
        if screen is not None:
            avail = screen.availableGeometry()
            target_x = avail.x() + (avail.width() - self.width()) // 2
            target_y = avail.y() + (avail.height() - self.height()) // 2
            print(f'[toast] screen avail geom={avail.x()},{avail.y()} '
                  f'{avail.width()}x{avail.height()} -> move to '
                  f'{target_x},{target_y}')
            # setGeometry is accepted by more compositors than move()
            # alone for frameless dialog-role surfaces on Wayland.
            self.setGeometry(target_x, target_y, self.width(), self.height())

        # Hide-then-show forces the compositor to remap the surface
        # even if we're already visible. Cheap (sub-frame).
        if self.isVisible():
            self.hide()
            QApplication.processEvents()

        self.show()
        self.raise_()
        self.update()
        QApplication.processEvents()

        # AFTER show() the QWindow (QPA backing) exists, which lets us
        # reach the platform layer directly. On Wayland this is often
        # the only positioning API that actually takes effect for non-
        # xdg-popup surfaces; QWidget.move() gets coalesced away by Qt.
        wh = self.windowHandle()
        if wh is not None and screen is not None:
            try:
                wh.setScreen(screen)
            except Exception:
                pass
            wh.setPosition(target_x, target_y)
            # Re-request focus/stack so the newly positioned surface
            # ends up on top rather than behind the app it relocated
            # on top of.
            self.raise_()
            QApplication.processEvents()
            print(f'[toast] windowHandle setPosition -> ({target_x},{target_y}); '
                  f'actual={self.x()},{self.y()}')

        # Diagnostic: dump the widget's own rendered pixmap to a PNG
        # so we can tell whether paint() ran and produced the right
        # image, separately from whether the compositor mapped it.
        try:
            from PyQt5.QtGui import QPixmap
            pm = QPixmap(self.size())
            pm.fill(Qt.transparent if self.testAttribute(Qt.WA_TranslucentBackground)
                    else self._BG_COLOR)
            self.render(pm)
            dbg_path = os.path.join(LOG_DIR, 'toast-last.png')
            pm.save(dbg_path, 'PNG')
            print(f'[toast] wrote render dump to {dbg_path}')
        except Exception as e:
            print(f'[toast] render-dump failed: {type(e).__name__}: {e}')

        print(f'[toast] after show: visible={self.isVisible()} '
              f'geometry=({self.x()},{self.y()} {self.width()}x{self.height()}) '
              f'windowHandle={self.windowHandle() is not None}')

        self._hide_timer.start(duration_ms)

    # ------------------------- Qt overrides ------------------------

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)

        # Rounded dark pill. Because the window is NOT translucent,
        # the rounded corners clip to square at the window edges —
        # acceptable aesthetic trade-off for reliable visibility.
        rect = self.rect().adjusted(0, 0, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(
            float(rect.x()), float(rect.y()),
            float(rect.width()), float(rect.height()),
            float(self._BORDER_RADIUS), float(self._BORDER_RADIUS),
        )
        painter.fillPath(path, QBrush(self._BG_COLOR))
        pen = QPen(self._BORDER_COLOR)
        pen.setWidth(1)
        painter.setPen(pen)
        painter.drawPath(path)

        # Text, offset by padding. QTextDocument draws starting at the
        # painter origin, so we translate to the content rect first.
        painter.translate(TOAST_PADDING_X, TOAST_PADDING_Y)
        painter.setPen(self._TEXT_COLOR)
        self._doc.drawContents(painter)
        painter.end()


# ---------------------------------------------------------------------------
# misc helpers
# ---------------------------------------------------------------------------

# Candidate GUI ssh-askpass helpers, in preference order. KDE Plasma
# ships ksshaskpass (matches the desktop theme on Kubuntu, which is
# what the user is running); Gnome has its own helper at two different
# paths depending on packaging. ssh-askpass is the generic fallback —
# it's Debian's /etc/alternatives target for this role.
_ASKPASS_CANDIDATES = (
    '/usr/bin/ksshaskpass',
    '/usr/bin/ssh-askpass',
    '/usr/libexec/openssh/gnome-ssh-askpass',
    '/usr/lib/openssh/gnome-ssh-askpass',
    '/usr/lib/ssh/ssh-askpass',
    '/usr/bin/lxqt-openssh-askpass',
    '/usr/bin/x11-ssh-askpass',
)


def _find_gui_askpass() -> str:
    """Return an absolute path to a GUI ssh-askpass binary, or '' if
    none can be found. The returned path is suitable for use as
    SUDO_ASKPASS in conjunction with `sudo -A`.
    """
    for path in _ASKPASS_CANDIDATES:
        try:
            if os.access(path, os.X_OK):
                return path
        except OSError:
            continue
    # Fall back to PATH lookup for generic 'ssh-askpass' (Debian
    # alternatives target). shutil.which handles the PATH traversal.
    import shutil
    for name in ('ksshaskpass', 'ssh-askpass',
                 'gnome-ssh-askpass', 'lxqt-openssh-askpass'):
        p = shutil.which(name)
        if p:
            return p
    return ''


def _is_installed_mode() -> bool:
    """True if we were launched by the systemd units install.sh lays
    down, false if this is a manual ./start.sh invocation.

    Set by both unit files as Environment=TOOTHKEY_INSTALLED=1. Used
    to decide whether Restart should talk to systemctl (installed) or
    re-exec start.sh (manual).
    """
    return os.environ.get('TOOTHKEY_INSTALLED') == '1'


def _open_folder(path: str) -> None:
    """Spawn `xdg-open <path>` detached. The tray runs as the invoking
    user now, so there's no sudo round-trip to re-enter a user session.
    """
    try:
        subprocess.Popen(
            ['xdg-open', path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        print(f'[tray] opened folder: {path}')
    except FileNotFoundError:
        print(f'[tray] xdg-open missing; folder is at {path}')
    except Exception as e:
        print(f'[tray] failed to open {path}: {type(e).__name__}: {e}')


# ---------------------------------------------------------------------------
# worker link
# ---------------------------------------------------------------------------

class WorkerLink(QObject):
    """Connects to a running worker process over a Unix-domain socket.

    The worker is spawned by start.sh (not here), binds the socket
    before we even start, and blocks in accept(). We just dial in as
    a client. This keeps sudo out of the tray's process tree entirely
    — sudo prompts in the user's interactive shell once, at
    ./start.sh time.

    Signals:
        state_changed(dict)  : emitted on every {"type":"state", ...}
                               event. Dict keys: connected (bool),
                               name (str|None), mac (str|None),
                               grab (bool).
        worker_gone()        : emitted when the socket reads EOF (the
                               worker process exited or died). UI
                               thread decides what to do about it.
        shutdown_ack()       : emitted on {"type":"shutdown_ack"}.
    """

    state_changed = pyqtSignal(dict)
    worker_gone = pyqtSignal()
    shutdown_ack = pyqtSignal()

    def __init__(self, sock_path: str, parent: QObject = None):
        super().__init__(parent)
        self._sock_path = sock_path
        self._send_lock = threading.Lock()
        self._wfh = None
        self._conn = None
        self._reader_thread = None
        self._worker_pid = None  # Filled in when we receive "hello".

    # --------------------------- startup ---------------------------

    def start(self, connect_timeout: float = 10.0) -> None:
        """Poll-connect to the worker's UDS until it accepts or we
        hit `connect_timeout`. start.sh is expected to have already
        waited for the socket file to appear, so this is mostly a
        safety net for the "accept still not ready" race window.
        """
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        deadline = time.monotonic() + connect_timeout
        last_err = None
        while time.monotonic() < deadline:
            try:
                sock.connect(self._sock_path)
                break
            except (FileNotFoundError, ConnectionRefusedError) as e:
                last_err = e
                time.sleep(0.1)
        else:
            raise RuntimeError(
                f'worker not reachable at {self._sock_path}: {last_err}')

        self._conn = sock
        rfh = sock.makefile('r', encoding='utf-8')
        self._wfh = sock.makefile('w', encoding='utf-8')

        self._reader_thread = threading.Thread(
            target=self._reader_loop, args=(rfh,), daemon=True,
            name='ipc-reader')
        self._reader_thread.start()
        print(f'[tray] connected to worker at {self._sock_path}')

        # Hand the worker the graphical-session env vars it needs to
        # lazy-import pynput for keyboard grab. When the worker is
        # started by systemd at boot it has no DISPLAY / WAYLAND_DISPLAY
        # / XAUTHORITY / XDG_RUNTIME_DIR, because those belong to the
        # per-user graphical session, not to the system. The tray runs
        # inside that session, so it forwards them here on connect.
        env = {}
        for k in ('DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY',
                  'XDG_RUNTIME_DIR', 'XDG_SESSION_TYPE'):
            v = os.environ.get(k)
            if v:
                env[k] = v
        self.send({'type': 'client_hello', 'env': env,
                   'pid': os.getpid()})
        print(f'[tray] sent client_hello env: {list(env.keys())}')

    # --------------------------- shutdown --------------------------

    def request_shutdown(self) -> None:
        """Send {"type":"shutdown"} and return immediately.

        We can't `kill` the worker (it runs as root, we don't), so
        graceful-only. The UI path waits on worker_gone() to finish
        teardown, with a timeout safety net in Tray.
        """
        self.send({'type': 'shutdown'})

    # --------------------------- I/O -------------------------------

    def send(self, obj: dict) -> None:
        with self._send_lock:
            if self._wfh is None:
                return
            try:
                self._wfh.write(json.dumps(obj, separators=(',', ':')) + '\n')
                self._wfh.flush()
            except (BrokenPipeError, OSError) as e:
                print(f'[tray] ipc send failed: {e}')
                self._wfh = None

    def _reader_loop(self, rfh):
        try:
            for line in rfh:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    print(f'[tray] bad json from worker: {line!r}')
                    continue
                t = msg.get('type')
                if t == 'state':
                    self.state_changed.emit(msg)
                elif t == 'shutdown_ack':
                    self.shutdown_ack.emit()
                elif t == 'hello':
                    self._worker_pid = msg.get('pid')
                    print(f'[tray] worker hello: pid={self._worker_pid}')
                else:
                    print(f'[tray] unknown worker msg: {msg}')
        except Exception as e:
            print(f'[tray] reader crashed: {type(e).__name__}: {e}')
        finally:
            print('[tray] worker link closed')
            self.worker_gone.emit()


# ---------------------------------------------------------------------------
# tray app
# ---------------------------------------------------------------------------

class Tray(QObject):

    def __init__(self, app: QApplication, sock_path: str):
        super().__init__()
        self.app = app

        (self.icon_connected, self.icon_disconnected,
         self.icon_grabbed, self.icon_shutting_down) = build_icons()

        self.tray = QSystemTrayIcon(self.icon_disconnected, self)
        self.tray.setToolTip('Tooth-key: starting...')
        self.tray.activated.connect(self._on_activated)

        self.menu = QMenu()
        self.tray.setContextMenu(self.menu)
        self._rebuild_menu()
        self.tray.show()

        # Last-rendered state, used to skip redundant menu rebuilds.
        self._state = {'connected': False, 'name': None, 'mac': None, 'grab': False}
        self._shutting_down = False
        # True between "user clicked Grab" and "worker confirmed grab=True".
        # Drives the pulse animation on the floating indicator.
        self._grab_pending = False

        # Floating "keyboard is grabbed" indicator (top-right of screen).
        # Render a fresh high-res tooth pixmap for it so it stays sharp
        # at GRAB_INDICATOR_SIZE on HiDPI displays.
        renderer = QSvgRenderer(ICON_SVG_PATH)
        tooth_pm = _render_svg_pixmap(renderer, 256)
        self.indicator = GrabIndicator(tooth_pm)
        self.indicator.clicked.connect(self._on_indicator_clicked)

        # Transient confirmation toast — shown on grab enable.
        self.toast = Toast()

        self.link = WorkerLink(sock_path, self)
        self.link.state_changed.connect(self._on_state_changed)
        self.link.worker_gone.connect(self._on_worker_gone)
        self.link.shutdown_ack.connect(self._on_shutdown_ack)

        # Defer the connect attempt until after the Qt event loop is
        # spinning so a failure shows up as a log entry + error bubble
        # rather than a silent stall before QApplication.exec_().
        QTimer.singleShot(0, self._start_link)

    # ------------------------- menu / icon -------------------------

    def _rebuild_menu(self):
        self.menu.clear()
        s = getattr(self, '_state', {'connected': False, 'name': None, 'mac': None, 'grab': False})

        if s['connected']:
            label = s.get('name') or s.get('mac') or 'device'
            act = self.menu.addAction(f'Disconnect {label}')
            act.triggered.connect(self._on_disconnect)
            self.menu.addSeparator()

            # Grab/ungrab is only meaningful while we have a peer to
            # forward keys to; hide it entirely when disconnected so
            # the user can't toggle into a state that silently drops
            # every keystroke they type.
            if s.get('grab'):
                grab = self.menu.addAction('Ungrab keyboard')
            else:
                grab = self.menu.addAction('Grab keyboard')
            grab.triggered.connect(self._on_toggle_grab)
            self.menu.addSeparator()

        log_act = self.menu.addAction('Open log folder')
        log_act.triggered.connect(self._on_open_log_folder)

        self.menu.addSeparator()
        restart_act = self.menu.addAction('Restart')
        restart_act.triggered.connect(self._on_restart)
        exit_act = self.menu.addAction('Exit')
        exit_act.triggered.connect(self._on_exit)

    def _refresh_ui_from_state(self):
        s = self._state
        connected = bool(s.get('connected'))
        name = s.get('name') or s.get('mac')
        grab = bool(s.get('grab'))

        # Icon priority:
        #   - disconnected  -> red-X tooth (state trumps everything)
        #   - connected + grabbed -> green tooth (keystrokes are being
        #     forwarded right now; this is the loudest UI cue we can
        #     give a user who's about to type into a real keyboard)
        #   - connected, not grabbed -> plain white tooth
        # The floating top-right indicator still drives its own visual
        # state independently (see _on_toggle_grab / _on_state_changed).
        if connected:
            if grab:
                self.tray.setIcon(self.icon_grabbed)
            else:
                self.tray.setIcon(self.icon_connected)
            label = name or 'device'
            # Multi-line plain-text tooltip. We tried HTML (<b>, <br/>)
            # here first but QSystemTrayIcon under KDE Plasma routes
            # setToolTip() through the StatusNotifierItem DBus protocol,
            # whose renderer on Plasma (and on most other SNI hosts)
            # shows the tags LITERALLY instead of formatting them. So
            # we lean on line breaks + a short label on the product
            # line to give "Tooth-key" visual weight without markup.
            if grab:
                self.tray.setToolTip(
                    f'Tooth-key\n'
                    f'Connected to: {label}\n'
                    f'Click to ungrab keyboard'
                )
            else:
                self.tray.setToolTip(
                    f'Tooth-key\n'
                    f'Connected to: {label}\n'
                    f'Click to grab keyboard'
                )
        else:
            self.tray.setIcon(self.icon_disconnected)
            suffix = ' (grab ON)' if grab else ''
            self.tray.setToolTip(f'Tooth-key: not connected{suffix}')

        self._rebuild_menu()

    def _on_activated(self, reason):
        # Right-click (Context) is already handled by the platform via
        # setContextMenu() — leave it alone. On KDE Plasma in particular,
        # intercepting it here would double-show the menu because SNI
        # fires both activated(Context) AND opens the context menu via
        # its own DBus menu protocol.
        if reason not in (QSystemTrayIcon.Trigger, QSystemTrayIcon.MiddleClick):
            return

        # If our menu is already visible (e.g. the user just dismissed
        # the platform context menu and immediately left-clicked), don't
        # stack a second popup on top of it.
        if self.menu.isVisible():
            self.menu.hide()
            return

        # Left-click shortcut: when we have a connected HID client, a
        # plain click on the tray icon is treated as "toggle grab"
        # (the primary action the user reaches for in this mode). The
        # full menu is still one right-click away. When disconnected,
        # there's no useful primary action, so we fall through and
        # show the menu as before.
        if self._state.get('connected'):
            self._on_toggle_grab()
            return

        # QSystemTrayIcon.geometry() is commonly (0,0,0,0) under KDE
        # Plasma's StatusNotifierItem — SNI doesn't expose a real
        # on-screen rect — so calling popup(self.tray.geometry().bottomLeft())
        # stuck the menu at (0, 0). Anchor at the actual click location
        # (QCursor.pos()) instead; that works across KDE, GNOME and
        # Wayland-native environments.
        from PyQt5.QtGui import QCursor
        pos = QCursor.pos()

        # Pre-render so sizeHint() is populated, then shift so the menu
        # ends at the cursor rather than starting at it — matches how
        # most desktops anchor tray menus (menu grows up-and-left from
        # the click when the tray is along the bottom of the screen).
        self.menu.adjustSize()
        sh = self.menu.sizeHint()
        screen = QApplication.primaryScreen()
        avail = screen.availableGeometry() if screen is not None else None
        if avail is not None and pos.y() > avail.center().y():
            pos.setY(max(avail.top(), pos.y() - sh.height()))
        if avail is not None and pos.x() + sh.width() > avail.right():
            pos.setX(max(avail.left(), pos.x() - sh.width()))

        self.menu.popup(pos)

    # ------------------------- link plumbing -----------------------

    def _start_link(self):
        try:
            self.link.start()
        except Exception as e:
            print(f'[tray] failed to reach worker: {type(e).__name__}: {e}')
            self.tray.setToolTip(f'Tooth-key: worker unreachable ({e})')
            self.tray.showMessage('Tooth-key', f'Worker unreachable: {e}',
                                  QSystemTrayIcon.Critical, 8000)

    def _on_state_changed(self, msg: dict):
        # Filter out the "type" key so _state has the same shape
        # _current_state() in worker.py produces.
        self._state = {k: msg.get(k) for k in ('connected', 'name', 'mac', 'grab')}

        # Reconcile the floating indicator with the authoritative state
        # from the worker. This fires in three distinct flows:
        #   1. User clicked Grab → we set _grab_pending and showed a
        #      pulsing indicator. Now the worker broadcasts grab=True
        #      and we lock the indicator to its solid "active" state.
        #   2. User clicked Ungrab (menu or indicator) → grab=False
        #      arrives and we hide the indicator.
        #   3. Disconnect / worker-initiated ungrab → same as #2.
        #
        # Do this BEFORE _refresh_ui_from_state so the tray icon /
        # menu update and indicator update land on the same Qt tick.
        grab = bool(self._state.get('grab'))
        if grab:
            self._grab_pending = False
            self.indicator.show_active()
        else:
            self._grab_pending = False
            self.indicator.hide_indicator()

        self._refresh_ui_from_state()

    def _on_worker_gone(self):
        print('[tray] worker link closed')
        if self._shutting_down:
            # Expected path — Exit was clicked, finish quitting.
            self._quit_now()
            return
        # Unexpected: worker died on its own. Flip the icon to a clear
        # "broken" state and notify. We don't auto-respawn — restart
        # by the user via ./start.sh is the intended recovery.
        self._state = {'connected': False, 'name': None, 'mac': None, 'grab': False}
        self._grab_pending = False
        self.indicator.hide_indicator()
        self.tray.setIcon(self.icon_disconnected)
        self.tray.setToolTip('Tooth-key: worker died — re-run ./start.sh')
        self.tray.showMessage(
            'Tooth-key', 'Worker process exited unexpectedly. '
                         'Quit the tray and re-run ./start.sh.',
            QSystemTrayIcon.Critical, 8000)
        self._rebuild_menu()

    def _on_shutdown_ack(self):
        # Worker confirmed the shutdown request; the subsequent EOF on
        # the socket fires worker_gone(), which is where we actually
        # quit. Just a log breadcrumb here.
        print('[tray] shutdown ack from worker')

    # ------------------------- user actions ------------------------

    def _on_disconnect(self):
        print('[tray] disconnect requested')
        self.link.send({'type': 'disconnect'})

    def _on_toggle_grab(self):
        new_state = not self._state.get('grab', False)
        print(f'[tray] grab mode -> {new_state}')
        self.link.send({'type': 'set_grab', 'on': new_state})
        # Optimistically reflect the toggle so the menu label flips
        # before the worker's next state broadcast arrives.
        self._state = dict(self._state, grab=new_state)

        # Show the floating indicator *immediately* in pending/pulsing
        # mode. The worker's state broadcast (via _on_state_changed)
        # will promote it to solid once the pynput listener is actually
        # up and grabbing. If the user is turning grab OFF, hide now —
        # we also get an authoritative grab=False state update shortly
        # but hiding optimistically keeps the UI snappy.
        if new_state:
            self._grab_pending = True
            self.indicator.show_pending()
            # Brief on-screen confirmation so the user knows the grab
            # actually took hold. Matches the wording of the tray
            # tooltip's "Click to ungrab" so the mental model stays
            # consistent.
            self.toast.show_message(
                'Keyboard grabbed. Click '
                '<b>Tooth-key</b> to ungrab.'
            )
        else:
            self._grab_pending = False
            self.indicator.hide_indicator()

        self._refresh_ui_from_state()

    def _on_indicator_clicked(self):
        """User clicked the floating tooth — force an ungrab. We always
        send grab=off here regardless of current state: the indicator
        is only ever visible when grab is on (or pending), so clicking
        it always means 'turn it off'.
        """
        print('[tray] indicator clicked -> ungrab')
        self._grab_pending = False
        self.indicator.hide_indicator()
        self.link.send({'type': 'set_grab', 'on': False})
        self._state = dict(self._state, grab=False)
        self._refresh_ui_from_state()

    def _on_open_log_folder(self):
        _open_folder(LOG_DIR)

    def _on_exit(self):
        self._begin_shutdown(restart=False)

    def _on_restart(self):
        """Menu action: quit cleanly, then launch start.sh again.

        Since start.sh needs root (via sudo) to start the BT worker,
        and we're being invoked from a GUI context with no controlling
        tty, the usual `sudo -v` prompt would have nowhere to read a
        password from. We solve that by locating a GUI ssh-askpass
        helper and handing it to sudo through SUDO_ASKPASS. start.sh
        detects this case and switches to `sudo -A`, which pops a
        native password dialog on whichever desktop environment the
        user is running.

        If no askpass helper exists on the system, we fall back to
        spawning start.sh in a terminal emulator so the user can type
        the password there. Worst case (no askpass, no terminal), we
        show a desktop notification explaining the situation.
        """
        print('[tray] restart requested')
        self._begin_shutdown(restart=True)

    def _begin_shutdown(self, restart: bool):
        """Common shutdown path for both Exit and Restart. `restart`
        schedules a fresh start.sh to be spawned right before the
        tray quits.
        """
        if self._shutting_down:
            return
        self._shutting_down = True
        self._restart_on_quit = bool(restart)

        # Immediate visual feedback so the user knows the click took,
        # even if the worker takes a moment to tear down (disconnecting
        # iOS's ACL, closing L2CAP servers, joining threads, etc).
        self.tray.setIcon(self.icon_shutting_down)
        self.tray.setToolTip(
            'Tooth-key: restarting...' if restart
            else 'Tooth-key: shutting down...')
        self.menu.clear()
        msg = 'Restarting...' if restart else 'Shutting down...'
        quitting = self.menu.addAction(msg)
        quitting.setEnabled(False)

        self.link.request_shutdown()
        # Safety net: if the worker never drops the socket (stuck in
        # a blocking syscall we can't interrupt), quit anyway after
        # 5 seconds. Typical teardown completes in under 200ms.
        QTimer.singleShot(5000, self._quit_now)

    def _quit_now(self):
        # Make sure the floating indicator doesn't outlive the tray —
        # it's parentless (top-level widget), so Qt would happily keep
        # it on screen after app.quit() returns if we don't close it
        # explicitly.
        try: self.indicator.hide_indicator()
        except Exception: pass
        # Launch the replacement start.sh BEFORE app.quit(); spawning
        # it under start_new_session=True detaches it from our process
        # group so it outlives the dying Qt event loop. We deliberately
        # don't wait for it — start.sh's own kill_previous_instances
        # step will gracefully take us out once we're gone.
        if getattr(self, '_restart_on_quit', False):
            self._spawn_restart()
        self.tray.hide()
        self.app.quit()

    def _spawn_restart(self) -> None:
        """Bring Toothkey back up cleanly after a Restart click.

        Two flavours depending on how we were launched:

          A. Installed mode (TOOTHKEY_INSTALLED=1, the install.sh flow).
             The worker is managed by systemd, the tray is managed by
             `systemctl --user`. Restart = ask both services to
             restart. install.sh's sudoers drop-in lets us restart
             the root worker without a password prompt, so the whole
             sequence is silent from the user's POV.

          B. start.sh mode (the default, not installed). Fall back
             to re-exec'ing start.sh with a GUI askpass helper for
             the sudo prompt — same as it's always worked.
        """
        if _is_installed_mode():
            self._spawn_restart_installed()
        else:
            self._spawn_restart_start_sh()

    def _spawn_restart_installed(self) -> None:
        """Systemd-managed restart path. Fires off two systemctl calls
        (one system, one --user) and exits; systemd respawns both.
        """
        # 1. Ask systemd (system bus) to restart the worker. This runs
        #    under the sudoers NOPASSWD drop-in that install.sh wrote,
        #    so it completes with no password prompt.
        try:
            subprocess.Popen(
                ['sudo', '-n', 'systemctl', 'restart',
                 'toothkey-worker.service'],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            print('[tray] restart (installed): spawned '
                  'sudo -n systemctl restart toothkey-worker.service')
        except Exception as e:
            print(f'[tray] restart worker spawn failed: '
                  f'{type(e).__name__}: {e}')
            # Continue anyway — user may still want the tray to restart.

        # 2. Tray restart via the user bus. We schedule it as a
        #    detached `sleep 1 && systemctl --user restart` so it
        #    runs AFTER this process exits (otherwise systemd would
        #    see us as still alive and refuse to respawn).
        try:
            subprocess.Popen(
                ['bash', '-c',
                 'sleep 1 && exec systemctl --user restart '
                 'toothkey-tray.service'],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            print('[tray] restart (installed): scheduled '
                  'systemctl --user restart toothkey-tray.service')
        except Exception as e:
            print(f'[tray] restart tray spawn failed: '
                  f'{type(e).__name__}: {e}')
            self.tray.showMessage(
                'Tooth-key', f'Restart spawn failed: {e}',
                QSystemTrayIcon.Critical, 8000)

    def _spawn_restart_start_sh(self) -> None:
        """Non-installed fallback: re-exec ./start.sh with a GUI
        askpass helper for the sudo prompt it's going to do.
        """
        script_dir = os.path.dirname(os.path.abspath(__file__))
        start_sh = os.path.join(script_dir, 'start.sh')
        if not os.path.isfile(start_sh):
            print(f'[tray] restart failed: {start_sh} not found')
            self.tray.showMessage(
                'Tooth-key', f'Restart failed: {start_sh} not found.',
                QSystemTrayIcon.Critical, 8000)
            return

        askpass = _find_gui_askpass()
        env = os.environ.copy()
        # Tell start.sh that it's being launched without a tty from
        # the tray, so it picks sudo -A instead of sudo -v.
        env['TOOTHKEY_GUI_SUDO'] = '1'
        if askpass:
            env['SUDO_ASKPASS'] = askpass
            print(f'[tray] restart: using askpass {askpass}')
        else:
            # No askpass found. start.sh will detect SUDO_ASKPASS is
            # unset, realize it has no tty, and launch itself inside a
            # terminal emulator as a last resort. If it can't find one
            # it'll write the reason to its own bootstrap log — the
            # user will see the tray never come back.
            print('[tray] restart: no GUI askpass found; '
                  'start.sh will fall back to a terminal')

        # Small delay before start.sh takes over: the current Qt tray
        # needs a moment to actually exit (disown the tray icon,
        # release the SNI DBus name, let the worker's ack land).
        # Otherwise start.sh's kill_previous_instances fires during
        # that window and we get misleading "killed zombie instance"
        # messages. 1s is enough in practice.
        try:
            subprocess.Popen(
                ['bash', '-c', f'sleep 1 && exec {start_sh!s}'],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            print(f'[tray] restart: spawned {start_sh} detached')
        except Exception as e:
            print(f'[tray] restart spawn failed: {type(e).__name__}: {e}')
            self.tray.showMessage(
                'Tooth-key', f'Restart spawn failed: {e}',
                QSystemTrayIcon.Critical, 8000)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    _diag('main: parsing args')
    parser = argparse.ArgumentParser()
    parser.add_argument('--socket', required=True,
                        help='Path of the worker\'s UDS to connect to.')
    args, qt_args = parser.parse_known_args()
    _diag(f'main: args.socket={args.socket!r} qt_args={qt_args!r}')

    # Qt needs a graphical session. When launched detached from start.sh
    # we inherit DISPLAY/WAYLAND_DISPLAY from the user's shell — if none
    # is set we bail cleanly rather than crashing inside QApplication.
    if not os.environ.get('DISPLAY') and not os.environ.get('WAYLAND_DISPLAY'):
        _diag('main: no DISPLAY/WAYLAND_DISPLAY -> exit 2')
        print('error: no DISPLAY or WAYLAND_DISPLAY set. '
              'Launch this from a graphical session.', file=sys.stderr)
        return 2

    _diag('main: creating QApplication')
    try:
        app = QApplication([sys.argv[0]] + qt_args)
    except Exception as e:
        _diag(f'main: QApplication() raised: {type(e).__name__}: {e}')
        raise
    _diag('main: QApplication created')
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName('Tooth-key')
    app.setDesktopFileName('toothkey')

    # Grant the root worker access to our X server so pynput (running
    # as root) can open a display. Ignored on Wayland and harmless
    # otherwise. Run quietly — if xhost is missing or this is Wayland
    # we just skip it; grab will still work on Wayland via the pynput
    # Wayland backends (evdev / libinput).
    try:
        subprocess.run(['xhost', '+SI:localuser:root'],
                       stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL,
                       timeout=2.0, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    _diag('main: checking isSystemTrayAvailable()')
    avail = QSystemTrayIcon.isSystemTrayAvailable()
    _diag(f'main: isSystemTrayAvailable -> {avail}')
    if not avail:
        print('error: no system tray detected. On Ubuntu GNOME, install '
              'the AppIndicator extension and log out/in.', file=sys.stderr)
        return 3

    _diag('main: constructing Tray')
    tray = Tray(app, args.socket)  # noqa: F841  (kept alive by local scope)
    _diag('main: entering Qt event loop')
    rc = app.exec_()
    _diag(f'main: Qt event loop returned rc={rc}')
    return rc


if __name__ == '__main__':
    try:
        _rc = main()
        _diag(f'__main__: main() returned {_rc}, calling sys.exit')
        sys.exit(_rc)
    except SystemExit as _se:
        _diag(f'__main__: SystemExit code={_se.code!r}')
        raise
    except BaseException as _e:
        import traceback as _tb
        _diag(f'__main__: uncaught {type(_e).__name__}: {_e}\n'
              + _tb.format_exc())
        raise
