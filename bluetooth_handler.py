import os, select, struct, subprocess, threading, time

import dbus

from socket import socket, AF_BLUETOOTH, SOCK_SEQPACKET, BTPROTO_L2CAP, SOL_SOCKET, SO_REUSEADDR, gethostname

# Bluetooth socket security options. Python's socket module doesn't expose
# these as constants, so we hard-code from /usr/include/bluetooth/bluetooth.h.
SOL_BLUETOOTH = 274
BT_SECURITY = 4
# Bluetooth link security levels (from <bluetooth/bluetooth.h>):
#   LOW    = 1   no auth/encryption (pin-only legacy)
#   MEDIUM = 2   auth + encryption, MITM not required (allows Just Works SSP)
#   HIGH   = 3   auth + encryption + MITM protection required
#   FIPS   = 4   HIGH + FIPS-approved ciphers
# HID channels *must* be HIGH for iOS: Apple's HID policy refuses Just-Works
# SSP on keyboards (a Just-Works keylogger would be trivial to MITM), so if
# we advertise MEDIUM the iPhone accepts the ACL, realises the negotiated
# security level is insufficient for HID, and drops with HCI 0x0E "Connection
# Rejected due to Security Reasons" before ever sending IO-Capability-Request.
BT_SECURITY_MEDIUM = 2
BT_SECURITY_HIGH = 3
from dbus import SystemBus, Interface
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib
from bluetooth_profile import ToothkeyProfile, ToothkeyAgent

# Bluetooth "Class of Device" for a Peripheral Keyboard.
# Major Device Class = Peripheral (0x05), Minor Device Class = Keyboard.
# iOS uses this to pick the HID-specific pairing flow; without it the iPhone
# sees us as a Computer, does a brief SDP probe, and bails before pairing.
CLASS_OF_DEVICE_PERIPHERAL_KEYBOARD = '0x002540'

DBUS_OBJECT_MAPPER_INTERFACE = 'org.freedesktop.DBus.ObjectManager'
DBUS_PROPERTIES_INTERFACE = 'org.freedesktop.DBus.Properties'

BLUEZ_ADAPTER_INTERFACE = 'org.bluez.Adapter1'
BLUEZ_SERVICE_NAME = 'org.bluez'

CONTROL_CHANNEL = 0x0011
INTERRUPT_CHANNEL = 0x0013

CLIENT_MAC_ADDRESS_CACHE = 'client_mac_address.cache'


def _tlog(msg: str) -> None:
    """Log msg with a guaranteed ISO timestamp.

    Why this exists: logging_setup's pipe-based stdout timestamper is
    racy — on rare occasions (e.g. when bluetoothd spews a burst bigger
    than the kernel pipe buffer) its reader thread drops the prefix and
    the line lands in logs/toothkey.log without any "when". That made
    reconnect diagnostics really painful: we'd see "ACL up ... Connected
    False ... paging ... ACL up ..." with no idea if it was 1s or 60s
    between each event.

    _tlog guarantees a timestamp on every call by writing the message
    (pre-formatted with a local ISO-8601 timestamp) directly to
    logs/worker-diag.log via worker._wdiag — which is a separate,
    unbuffered file handle that does NOT go through logging_setup. Then
    it also emits the raw message to stdout so it still shows in
    toothkey.log / the terminal alongside everything else.
    """
    try:
        from worker import _wdiag as _wd
        _wd(msg)
    except Exception:
        pass
    print(msg, flush=True)


def build_device_name() -> str:
    """The name iOS (and any scanner) will see for this adapter."""
    host = gethostname() or 'unknown'
    return f'Tooth-key ({host})'


# Short tags for the few 16-bit BT SDP UUIDs we commonly see advertised by
# BlueZ plugins, so the adapter UUID dump is self-explanatory.
# (The "classic BT profile" UUIDs live in the 0x1000..0x12FF range.)
_UUID_TAGS = {
    '0x1000': 'ServiceDiscoveryServer',
    '0x1001': 'BrowseGroupDescriptor',
    '0x1002': 'PublicBrowseGroup',
    '0x1101': 'SerialPort',
    '0x1105': 'OBEXObjectPush',        # -> Object Transfer service bit
    '0x1106': 'OBEXFileTransfer',      # -> Object Transfer service bit
    '0x1108': 'Headset',               # -> Audio service bit
    '0x110a': 'AudioSource',           # -> Audio service bit
    '0x110b': 'AudioSink',             # -> Audio service bit
    '0x110c': 'AVRCPTarget',           # -> Audio service bit
    '0x110d': 'A2DPProfile',
    '0x110e': 'AVRCPRemote',           # -> Audio service bit
    '0x110f': 'AVRCPController',       # -> Audio service bit
    '0x1112': 'HeadsetAudioGateway',   # -> Audio + Telephony
    '0x111e': 'Handsfree',             # -> Audio + Telephony
    '0x111f': 'HandsfreeAudioGateway', # -> Audio + Telephony
    '0x1124': 'HID',                   # <- the only one we actually want
    '0x1130': 'PhonebookAccessPSE',    # -> Object Transfer
    '0x1132': 'MessageAccessServer',   # -> Object Transfer
    '0x1200': 'PnPInformation',
    '0x1800': 'GenericAccess',
    '0x1801': 'GenericAttribute',
    '0x180a': 'DeviceInformation',
    '0x180f': 'BatteryService',
}


def _uuid_tag(uuid):
    """Return a short human tag for a 128-bit SDP UUID if we know it."""
    try:
        s = str(uuid).lower()
        if len(s) == 36 and s.endswith('-0000-1000-8000-00805f9b34fb'):
            short = '0x' + s[4:8]
            return _UUID_TAGS.get(short, '')
    except Exception:
        pass
    return ''


def _decode_service_class_bits(bits: int) -> str:
    """Render the service-class field (bits 13..23 of CoD) as a
    comma-separated list of human names. Returns '<none>' if zero.

    Reference: Bluetooth Assigned Numbers — Device Class (base rate).
    bits is the value already shifted down (i.e. what `(cod >> 13) & 0x7FF`
    gives you).
    """
    names = [
        (0x001, 'LimitedDiscoverable'),
        (0x002, 'LEAudio'),
        (0x004, 'Reserved'),
        (0x008, 'Positioning'),
        (0x010, 'Networking'),
        (0x020, 'Rendering'),
        (0x040, 'Capturing'),
        (0x080, 'ObjectTransfer'),
        (0x100, 'Audio'),
        (0x200, 'Telephony'),
        (0x400, 'Information'),
    ]
    set_names = [n for mask, n in names if bits & mask]
    return ', '.join(set_names) if set_names else '<none>'


def _stringify(value):
    """Render a dbus typed value as plain Python for readable logging.

    dbus-python wraps primitives in subclasses (dbus.Boolean is an int
    subclass, dbus.String is a str subclass, etc.) with a chatty repr. We
    unwrap them to plain Python types so logs don't get cluttered."""
    try:
        if isinstance(value, dbus.Boolean):
            return bool(value)
        if isinstance(value, dbus.ByteArray):
            return bytes(value).hex()
        if isinstance(value, dbus.String):
            return str(value)
        if isinstance(value, dbus.ObjectPath):
            return str(value)
        if isinstance(value, (dbus.Byte, dbus.Int16, dbus.Int32, dbus.Int64,
                              dbus.UInt16, dbus.UInt32, dbus.UInt64)):
            return int(value)
        if isinstance(value, dbus.Double):
            return float(value)
        if isinstance(value, dbus.Dictionary):
            return {_stringify(k): _stringify(v) for k, v in value.items()}
        if isinstance(value, (dbus.Array, dbus.Struct)):
            return [_stringify(v) for v in value]
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float, str)):
            return value
        if isinstance(value, bytes):
            return value.hex()
        if isinstance(value, dict):
            return {_stringify(k): _stringify(v) for k, v in value.items()}
        if hasattr(value, '__iter__'):
            return [_stringify(v) for v in value]
    except Exception:
        pass
    return str(value)

# HID Report Descriptor
HID_REPORT_DESCRIPTOR = bytearray([
    # ---------------- Keyboard ----------------
    0x05, 0x01,     # Usage Page (Generic Desktop)
    0x09, 0x06,     # Usage (Keyboard)
    0xA1, 0x01,     # Collection (Application)
    0x85, 0x01,     # Report ID (1)
    0x05, 0x07,     # Usage Page (Key Codes)
    0x19, 0xE0,     # Usage Minimum (224)
    0x29, 0xE7,     # Usage Maximum (231)
    0x15, 0x00,     # Logical Minimum (0)
    0x25, 0x01,     # Logical Maximum (1)
    0x75, 0x01,     # Report Size (1)
    0x95, 0x08,     # Report Count (8)
    0x81, 0x02,     # Input (Data, Variable, Absolute) -> Modifier keys
    0x95, 0x01,     # Report Count (1)
    0x75, 0x08,     # Report Size (8)
    0x81, 0x03,     # Input (Constant) -> Reserved byte
    0x95, 0x06,     # Report Count (6)
    0x75, 0x08,     # Report Size (8)
    0x15, 0x00,     # Logical Minimum (0)
    0x25, 0x65,     # Logical Maximum (101)
    0x05, 0x07,     # Usage Page (Key Codes)
    0x19, 0x00,     # Usage Minimum (0)
    0x29, 0x65,     # Usage Maximum (101)
    0x81, 0x00,     # Input (Data, Array) -> Keycodes
    0xC0,           # End Collection
])

class ToothkeyHandler:

    server_mac_address:str = None
    client_mac_address:str = None
    # Human-readable name advertised by the client (iPhone's device name,
    # e.g. "smartnix"). Populated on InterfacesAdded for the connected
    # device and cleared on disconnect. The tray UI reads this to label
    # the "Disconnect ..." menu item.
    client_display_name:str = None

    client_control_socket:socket = None
    client_interrupt_socket:socket = None

    _control_server_socket:socket = None
    _interrupt_server_socket:socket = None

    _disconnect_event = threading.Event()
    _watcher_thread:threading.Thread = None
    _keepalive_thread:threading.Thread = None
    _lock = threading.Lock()

    _glib_mainloop = None
    _glib_thread:threading.Thread = None
    _bus:SystemBus = None
    _adapter_path:str = None
    _device_signal_match = None

    _reconnect_thread:threading.Thread = None
    _reconnect_wake = threading.Event()
    # Monotonic deadline before which the reconnect watchdog stays
    # idle, used so "Disconnect" from the tray menu sticks for a
    # little while instead of being instantly undone.
    _reconnect_suppressed_until:float = 0.0

    # --- Churn-detection bookkeeping ------------------------------
    # Monotonic timestamp of our last outbound page attempt and the
    # last time the peer's ACL transitioned True->False, as observed
    # via BlueZ Device1.PropertiesChanged signals. The reconnect
    # watchdog correlates these to decide whether a recent ACL drop
    # was "iOS rejecting us within seconds of our page" (= churn) vs.
    # "ACL timed out after a long idle" (= benign).
    _last_page_attempt_at:float = 0.0
    _last_peer_acl_drop_at:float = 0.0

    # Pending outbound HID sockets populated by the peripheral-
    # initiated reconnect path (cls._try_open_hid_outbound). When
    # set, wait_for_client uses these instead of blocking on accept().
    # Cleared after hand-off. Tuple = (control_sock, interrupt_sock, mac).
    _outbound_pending = None
    # Pipe used to wake a select() inside wait_for_client when
    # _outbound_pending becomes available. Populated in initialize().
    _accept_wake_r:int = -1
    _accept_wake_w:int = -1

    # --- Phantom-session detection --------------------------------
    # When we peripheral-initiate the HID L2CAPs (after iOS has aged
    # the bond's HID side out — typically after a long reconnect-
    # storm of "page failed errno=112"), the iOS BT kernel may accept
    # both PSM 0x11 + 0x13 at the link level WITHOUT promoting them
    # to a HID Host session. Result: both sides "look connected", but
    # iOS shows the on-screen keyboard in text fields and discards
    # any HID input reports we send.
    #
    # The signal we use to distinguish a real HID-bound session
    # from a phantom one is whether iOS sends ANY substantive
    # (i.e. non-HANDSHAKE) transaction within the first few
    # seconds. Empirically:
    #
    #   - Healthy session: at least SET_PROTOCOL (0x7X) within
    #     ~100ms; sometimes also SET_IDLE / SET_REPORT depending
    #     on the bond / iOS version. We have observed iPhones in
    #     the wild that send ONLY SET_PROTOCOL on a perfectly
    #     working session — so we cannot require more.
    #   - Phantom session A (silent): no inbound traffic at all.
    #   - Phantom session B (HANDSHAKE-only): iOS replies to our
    #     HID-descriptor write with HANDSHAKE/INVALID_PARAM and
    #     stops there.
    #
    # We deliberately do NOT try to identify the half-engaged
    # "iOS sent SET_PROTOCOL but HID Host still isn't bound" mode
    # from inbound traffic alone — empirically that case is wire-
    # indistinguishable from the healthy SET_PROTOCOL-only case.
    # Trying to detect it is what caused the disconnect/reconnect
    # storm we hit when threshold was set to 2 distinct types.
    #
    # _session_substantive_types_seen accumulates the distinct
    # transaction types (the high nibble) iOS has sent on the
    # control channel in the current session, excluding HANDSHAKE.
    # The phantom watchdog (started for outbound-initiated sessions
    # only) waits _PHANTOM_DETECT_TIMEOUT_S and, if the set is
    # still empty, tears the link down + force-disconnects the
    # ACL so iOS clears its state and a fresh page can re-engage
    # HID properly.
    #
    # Hard cap: the watchdog will only ever execute the teardown
    # action up to _PHANTOM_TEARDOWN_MAX_PER_RUN times for the life
    # of this worker process. Beyond that the watchdog disarms and
    # logs but does not act, so a user-visible "Restart" is needed
    # to re-arm. This protects against feedback loops where the
    # watchdog and iOS get into a teardown-reconnect storm.
    _session_substantive_types_seen:set = None
    _session_started_outbound:bool = False
    _phantom_thread:threading.Thread = None
    _phantom_teardowns_this_run:int = 0
    _PHANTOM_DETECT_TIMEOUT_S:float = 5.0
    _PHANTOM_TEARDOWN_MAX_PER_RUN:int = 2

    connected:bool = False
    _running:bool = False

    @classmethod
    def initialize(cls):
        """One-time setup: DBus, HID profile, listening sockets, adapter state.

        Deliberately does NOT remove existing pairings — that's what lets the
        iPhone reconnect across app restarts without re-pairing.
        """
        # _bdiag writes to logs/worker-diag.log directly, bypassing
        # logging_setup's pipe. Essential for debugging hangs inside
        # this function — the normal print() stream can get stuck
        # when the BlueZ D-Bus service is slow to respond.
        from worker import _wdiag as _bdiag

        _bdiag('init: load_client_mac_address_cache')
        cls.load_client_mac_address_cache()
        _bdiag('init: prepare_adapter')
        cls.prepare_adapter()
        _bdiag('init: prepare_adapter returned')

        _bdiag('init: DBusGMainLoop(set_as_default=True)')
        DBusGMainLoop(set_as_default=True)

        # GLib main loop is required for our D-Bus service objects (the agent
        # and the HID profile) to actually receive incoming method calls from
        # bluetoothd. Without it, pairing callbacks like RequestConfirmation
        # queue up forever and pairing silently times out.
        _bdiag('init: GLib.MainLoop()')
        cls._glib_mainloop = GLib.MainLoop()
        _bdiag('init: starting glib-mainloop thread')
        cls._glib_thread = threading.Thread(target=cls._glib_mainloop.run, daemon=True, name='glib-mainloop')
        cls._glib_thread.start()
        print('[dbus] GLib main loop running')
        _bdiag('init: GLib main loop running')

        _bdiag('init: connecting to SystemBus')
        bus = SystemBus()
        cls._bus = bus
        _bdiag('init: SystemBus connected')

        device_name = build_device_name()
        _bdiag(f'init: device_name={device_name!r}')

        _bdiag('init: find_adapter')
        adapter_path, cls.server_mac_address = cls.find_adapter(bus)
        cls._adapter_path = adapter_path
        _bdiag(f'init: adapter_path={adapter_path!r} mac={cls.server_mac_address!r}')

        if adapter_path is not None:
            # Before anything HCI-dependent (L2CAP bind, CoD write,
            # profile register), make absolutely sure the adapter is
            # powered. A DOWN adapter silently breaks every subsequent
            # step in this function and leaves iOS stuck on "Connection
            # Unsuccessful".
            _bdiag('init: verify_adapter_powered')
            cls._verify_adapter_powered(bus, adapter_path)

            _bdiag('init: set_adapter_alias')
            cls.set_adapter_alias(bus, adapter_path, device_name)

        _bdiag('init: constructing ToothkeyAgent')
        ToothkeyAgent(bus)
        _bdiag('init: constructing ToothkeyProfile')
        ToothkeyProfile(bus, service_name=device_name)
        _bdiag('init: ToothkeyProfile constructed')

        # BlueZ rewrites the adapter's Class of Device whenever profiles
        # register. The authoritative fix is /etc/bluetooth/main.conf's
        # `Class = 0x000540` (applied by start.sh), which makes BlueZ's
        # own recomputes land on Peripheral/Keyboard on their own. We
        # deliberately do NOT re-run set_class_of_device here: btmgmt
        # often wedges for 10s+ directly after a RegisterProfile call,
        # and by the time it unblocks the iPhone has already done its
        # SDP probe and bailed. Instead, just read back what bluez
        # actually wrote and warn if it's wrong.
        _bdiag('init: _verify_class_of_device (read-back only, no set)')
        cls._verify_class_of_device(bus, adapter_path)
        _bdiag('init: _log_adapter_class')
        cls._log_adapter_class(bus, adapter_path)

        _bdiag('init: _log_bluetoothd_cmdline')
        cls._log_bluetoothd_cmdline()
        _bdiag('init: _subscribe_to_device_events')
        cls._subscribe_to_device_events(bus)

        _bdiag(f'init: create control L2CAP socket (PSM 0x{CONTROL_CHANNEL:04x})')
        cls._control_server_socket = cls.create_channel_socket(cls.server_mac_address, CONTROL_CHANNEL)
        _bdiag(f'init: create interrupt L2CAP socket (PSM 0x{INTERRUPT_CHANNEL:04x})')
        cls._interrupt_server_socket = cls.create_channel_socket(cls.server_mac_address, INTERRUPT_CHANNEL)
        _bdiag('init: L2CAP sockets ready')

        # Wake pipe so the reconnect watchdog can inject a peripheral-
        # initiated HID connection into a wait_for_client() that's
        # already blocked in select() on the listening sockets.
        # Writing one byte to _accept_wake_w makes the next select
        # return with _accept_wake_r readable; wait_for_client drains
        # it, sees _outbound_pending populated, and uses those sockets
        # as the client connection. Having this as a pipe (vs. e.g.
        # a threading.Event) is what lets us mix it with the existing
        # accept() sockets in one select() call.
        r, w = os.pipe()
        os.set_blocking(r, False)
        os.set_blocking(w, False)
        cls._accept_wake_r = r
        cls._accept_wake_w = w
        _bdiag(f'init: accept wake pipe r={r} w={w}')

        cls._running = True

        # Imitate a real Bluetooth keyboard: whenever we have no HID
        # client connected, periodically page our last-known paired
        # peer so it can reconnect without the user having to open the
        # phone's BT settings. Runs as a daemon thread for the whole
        # app lifetime.
        _bdiag('init: starting reconnect watchdog thread')
        cls._reconnect_wake.clear()
        cls._reconnect_thread = threading.Thread(
            target=cls._reconnect_watchdog_loop,
            daemon=True, name='toothkey-reconnect',
        )
        cls._reconnect_thread.start()

        print(f'[MAC:{cls.server_mac_address}] advertising as "{device_name}"')
        _bdiag(f'init: done, advertising as {device_name!r}')

    @classmethod
    def wait_for_client(cls) -> bool:
        """Block until a client connects on both channels and the HID descriptor
        handshake completes. Returns True on success, False if aborted/failed.

        Two paths feed this function:
          1. Inbound:  iOS opens PSM 0x11 + 0x13 to us. Our listening
             sockets fire select() readable and we accept().
          2. Outbound: the reconnect watchdog opened both HID L2CAPs
             from our side (see _try_open_hid_outbound). It parks the
             resulting socket pair in _outbound_pending and pokes
             _accept_wake_w — select() returns with the wake pipe
             readable and we adopt those sockets directly.

        Either way we end up with (control_socket, interrupt_socket,
        client_mac) and funnel through the same "send HID descriptor,
        start watcher, start keepalive" tail.
        """
        from worker import _wdiag as _bdiag

        cls._disconnect_event.clear()

        print(f'[MAC:{cls.server_mac_address}] waiting for connections...')
        _bdiag('wait_for_client: select() on control PSM 0x0011 + wake pipe')

        control_socket = None
        interrupt_socket = None
        ccs_addr = cis_addr = None

        try:
            # Loop until we either get an inbound accept or adopt a
            # peripheral-initiated pair from _outbound_pending.
            while cls._running:
                # Peripheral-initiated path may have landed a pair
                # before we even got here; pick it up immediately.
                pending = cls._outbound_pending
                if pending is not None:
                    cls._outbound_pending = None
                    control_socket, interrupt_socket, mac = pending
                    ccs_addr = (mac, CONTROL_CHANNEL)
                    cis_addr = (mac, INTERRUPT_CHANNEL)
                    cls._session_started_outbound = True
                    _bdiag(f'wait_for_client: adopted outbound HID '
                           f'sockets for {mac}')
                    break

                # Block on whichever fires first: an inbound accept on
                # control PSM, or the wake pipe (outbound just landed).
                read_fds = [cls._control_server_socket,
                            cls._accept_wake_r]
                rlist, _, _ = select.select(read_fds, [], [])

                if cls._accept_wake_r in rlist:
                    # Drain any bytes the watchdog wrote — we'll
                    # re-check _outbound_pending on the next loop
                    # iteration.
                    try:
                        os.read(cls._accept_wake_r, 4096)
                    except (BlockingIOError, OSError):
                        pass
                    continue

                if cls._control_server_socket in rlist:
                    control_socket, ccs_addr = (
                        cls._control_server_socket.accept())
                    _bdiag(f'wait_for_client: control accepted from '
                           f'{ccs_addr}')
                    # Now block for the matching interrupt accept.
                    # Real peers open 0x13 within tens of ms of 0x11;
                    # if this accept stalls there's no useful way
                    # forward anyway.
                    _bdiag('wait_for_client: accept() on interrupt PSM 0x0013')
                    interrupt_socket, cis_addr = (
                        cls._interrupt_server_socket.accept())
                    _bdiag(f'wait_for_client: interrupt accepted from '
                           f'{cis_addr}')
                    cls._session_started_outbound = False
                    break

            if control_socket is None or interrupt_socket is None:
                # _running went False during the select — clean shutdown.
                return False

        except OSError as e:
            _bdiag(f'wait_for_client: accept/select failed: {e}')
            if cls._running:
                print(f'accept failed: {e}')
            for s in (control_socket, interrupt_socket):
                if s is not None:
                    try: s.close()
                    except OSError: pass
            return False

        with cls._lock:
            cls.client_control_socket = control_socket
            cls.client_interrupt_socket = interrupt_socket

        print(f'[MAC:{cls.server_mac_address}] control channel connected to {ccs_addr}')
        print(f'[MAC:{cls.server_mac_address}] interrupt channel connected to {cis_addr}')

        try:
            control_socket.send(bytes(HID_REPORT_DESCRIPTOR))
        except OSError as e:
            _bdiag(f'wait_for_client: send HID descriptor failed: {e}')
            print(f'failed to send HID descriptor: {e}')
            cls._drop_client()
            return False

        cls.client_mac_address = ccs_addr[0]
        cls.save_client_mac_address_cache()
        # Reset the per-session "iOS sent us a substantive HID
        # transaction" set BEFORE flipping connected=True so any
        # race with _watch_connection / _handle_control_transaction
        # sees a clean slate. Use a fresh set instance (rather than
        # .clear()) to avoid sharing references across sessions.
        cls._session_substantive_types_seen = set()
        cls.connected = True
        _bdiag(f'wait_for_client: connected=True client_mac={cls.client_mac_address} '
               f'outbound={cls._session_started_outbound}')

        cls._watcher_thread = threading.Thread(target=cls._watch_connection, daemon=True)
        cls._watcher_thread.start()

        # iOS silently drops idle HID links after ~10 minutes of zero
        # interrupt-channel traffic (empirical). Real Bluetooth
        # keyboards don't hit this because their BT stack does HCI-
        # level SNIFF + NULL packets on its own, resetting the timer.
        # We can't do SNIFF easily from userspace, so we just send an
        # "empty" keyboard input report every couple of minutes. It's
        # valid HID (modifier=0, no keys), iOS ignores it, but the
        # traffic is enough to keep the link fresh.
        cls._keepalive_thread = threading.Thread(
            target=cls._keepalive_loop,
            name='toothkey-hid-keepalive',
            daemon=True)
        cls._keepalive_thread.start()

        # Phantom-session watchdog: only relevant when WE initiated
        # the HID L2CAPs. Inbound accept means iOS opened the channels
        # on its side, so its HID Host is by definition already in
        # the loop. Outbound is the only path that can leave us with
        # L2CAPs up but iOS silent on the wire.
        #
        # Also disarm once we've already torn down
        # _PHANTOM_TEARDOWN_MAX_PER_RUN times this process — beyond
        # that the watchdog is more likely a feedback-loop hazard
        # than a useful recovery (we've seen iOS+bond combinations
        # where healthy sessions and phantom sessions are wire-
        # indistinguishable, so we must back off and trust the
        # connection). User can re-arm via tray Restart.
        if (cls._session_started_outbound and
            cls._phantom_teardowns_this_run < cls._PHANTOM_TEARDOWN_MAX_PER_RUN):
            cls._phantom_thread = threading.Thread(
                target=cls._phantom_session_watchdog,
                args=(cls.client_mac_address,),
                name='toothkey-phantom-watch',
                daemon=True)
            cls._phantom_thread.start()
        elif cls._session_started_outbound:
            _bdiag(f'wait_for_client: phantom watchdog disarmed for '
                   f'this run (already tore down '
                   f'{cls._phantom_teardowns_this_run}/'
                   f'{cls._PHANTOM_TEARDOWN_MAX_PER_RUN}) — '
                   f'trusting the connection')

        return True

    # -----------------------------------------------------------------
    # HID control-channel transaction constants (Bluetooth HID Profile
    # 1.1.1, section 4.7). The top 4 bits of byte 0 are the transaction
    # type; the bottom 4 bits are the parameter. We only care about
    # the transactions iOS actually sends, but we HANDSHAKE-ack
    # everything else (unsupported-request) so the host knows we're
    # alive.
    # -----------------------------------------------------------------
    _HID_T_HANDSHAKE    = 0x0
    _HID_T_HID_CONTROL  = 0x1
    _HID_T_GET_REPORT   = 0x4
    _HID_T_SET_REPORT   = 0x5
    _HID_T_GET_PROTOCOL = 0x6
    _HID_T_SET_PROTOCOL = 0x7
    _HID_T_GET_IDLE     = 0x8
    _HID_T_SET_IDLE     = 0x9
    _HID_T_DATA         = 0xA

    _HID_HS_SUCCESSFUL           = 0x0
    _HID_HS_NOT_READY            = 0x1
    _HID_HS_ERR_INVALID_REPORT   = 0x2
    _HID_HS_ERR_UNSUPPORTED_REQ  = 0x3
    _HID_HS_ERR_INVALID_PARAM    = 0x4
    _HID_HS_ERR_UNKNOWN          = 0xE
    _HID_HS_ERR_FATAL            = 0xF

    @classmethod
    def _send_control(cls, payload:bytes):
        """Blind-send a byte string on the HID control channel. Used
        for HANDSHAKE and DATA responses to host transactions.
        Any errors imply the peer is already gone — we'll pick that
        up on the next recv() iteration and tear down there."""
        sock = cls.client_control_socket
        if sock is None:
            return
        try:
            sock.send(payload)
        except OSError as e:
            print(f'[hid] control send failed ({e}); link probably gone')

    @classmethod
    def _handshake(cls, result_code:int):
        cls._send_control(
            bytes([(cls._HID_T_HANDSHAKE << 4) | (result_code & 0x0F)]))

    @classmethod
    def _hid_data_reply(cls, report_type:int, report:bytes):
        """Send a DATA transaction in reply to GET_REPORT / GET_PROTOCOL.
        report_type goes in the low nibble (0x1=input, 0x2=output,
        0x3=feature, or the protocol value for GET_PROTOCOL replies)."""
        cls._send_control(
            bytes([(cls._HID_T_DATA << 4) | (report_type & 0x0F)]) + report)

    # Stock 8-byte empty keyboard input report: no modifiers, no keys.
    # Same shape as DEFAULT_HID_REPORT in keyboard_handler, but kept
    # local so this module stands alone.
    _EMPTY_KBD_INPUT_REPORT = bytes(8)

    @classmethod
    def _handle_control_transaction(cls, data:bytes):
        """Parse one inbound HID control message and respond so iOS
        stays happy. Called from _watch_connection on every recv().

        The big picture: classic-Bluetooth HID requires the device to
        send a HANDSHAKE (or DATA for GET_xxx) back for every host
        transaction. If we stay silent iOS decides we're broken and
        drops the link within ~30-60s — that's what was causing the
        session to die shortly after connecting. We intentionally
        accept everything (LEDs, protocol switches, idle rate) and
        just ack — we have no physical LEDs and the emulated HID
        doesn't change behaviour between boot and report protocol."""
        if not data:
            return

        header = data[0]
        t_type = (header >> 4) & 0x0F
        t_param = header & 0x0F

        # Per-transaction debug log: lets us tell the difference
        # between "iOS is doing real HID setup" (SET_PROTOCOL,
        # SET_IDLE, SET_REPORT, GET_*) and "iOS only ack'd our
        # descriptor write with a HANDSHAKE then went silent"
        # (the phantom-session signature).
        try:
            from worker import _wdiag as _wd
            _wd(f'[hid-rx] header=0x{header:02x} type={t_type:#x} '
                f'param={t_param:#x} len={len(data)}')
        except Exception:
            pass

        # Track the set of distinct non-HANDSHAKE transaction types
        # iOS has sent in this session. The phantom watchdog gates
        # on this set being non-empty — i.e. iOS sent at least one
        # real HID Host transaction. HANDSHAKE alone doesn't count
        # because iOS sends it as an INVALID_PARAM ack to our HID-
        # descriptor write even when its HID Host never actually
        # binds. Storing the full set (rather than a bool) keeps
        # the diagnostic logging useful and lets us tighten the
        # heuristic later without touching this code path.
        if t_type != cls._HID_T_HANDSHAKE:
            seen = cls._session_substantive_types_seen
            if seen is not None:
                seen.add(t_type)

        if t_type == cls._HID_T_HID_CONTROL:
            # SUSPEND / EXIT_SUSPEND / VIRTUAL_CABLE_UNPLUG. Spec says
            # these don't get a HANDSHAKE response, they're one-way —
            # but logging helps trace teardowns.
            names = {0x0: 'NOP', 0x1: 'HARD_RESET', 0x2: 'SOFT_RESET',
                     0x3: 'SUSPEND', 0x4: 'EXIT_SUSPEND',
                     0x5: 'VIRTUAL_CABLE_UNPLUG'}
            print(f'[hid] HID_CONTROL: {names.get(t_param, hex(t_param))}')
            return

        if t_type == cls._HID_T_SET_REPORT:
            # iOS uses this to set the output report (keyboard LEDs:
            # NumLock/CapsLock/ScrollLock bits). We have no LEDs, so
            # we just ack. Spec also allows the host to include the
            # report ID + payload after the header; we don't need it.
            cls._handshake(cls._HID_HS_SUCCESSFUL)
            return

        if t_type == cls._HID_T_SET_PROTOCOL:
            # param 0 = BOOT protocol, 1 = REPORT protocol. Our report
            # layout happens to be the 8-byte boot-keyboard format
            # (1 modifier + 1 reserved + 6 keycodes), so either mode
            # works the same for us. Just ack and move on.
            cls._handshake(cls._HID_HS_SUCCESSFUL)
            return

        if t_type == cls._HID_T_SET_IDLE:
            # Host setting the idle rate. We don't actually auto-
            # repeat reports on idle — we only send on keypress —
            # so the rate is moot. Ack and move on.
            cls._handshake(cls._HID_HS_SUCCESSFUL)
            return

        if t_type == cls._HID_T_GET_REPORT:
            # "Send me your current report." Low nibble bit 0 = Input
            # (0x01), Output (0x02), Feature (0x03). If low-nibble
            # bit 3 is set (0x08) the host also includes a requested
            # size in bytes after the header, but we always reply
            # with the full 8-byte boot report, so we ignore it.
            rtype = t_param & 0x03
            if rtype == 0x01:  # input
                cls._hid_data_reply(0x01, cls._EMPTY_KBD_INPUT_REPORT)
            else:
                # We don't expose Output/Feature reports — NAK the
                # request with INVALID_REPORT_ID.
                cls._handshake(cls._HID_HS_ERR_INVALID_REPORT)
            return

        if t_type == cls._HID_T_GET_PROTOCOL:
            # Reply with DATA: param 1 = Report protocol. We claim
            # Report (matches our descriptor) so the host doesn't try
            # to renegotiate.
            cls._hid_data_reply(0x01, b'')
            return

        if t_type == cls._HID_T_GET_IDLE:
            # We don't track an idle rate. Reply with 0 ("indefinite"
            # / until state changes), which is the sane default for
            # a keyboard that only sends on change anyway.
            cls._hid_data_reply(0x00, b'\x00')
            return

        if t_type == cls._HID_T_DATA:
            # Output report data from host (e.g. LED state sent
            # unreliably). Some hosts use DATA instead of SET_REPORT.
            # No response required per spec.
            return

        # Anything else — unknown transaction. Spec says NAK it.
        print(f'[hid] unknown control transaction type={t_type:#x} '
              f'param={t_param:#x} len={len(data)}')
        cls._handshake(cls._HID_HS_ERR_UNSUPPORTED_REQ)

    # Wire-format keep-alive report. Matches the layout used by
    # ToothkeyKeyboardHandler.states:
    #   [0] 0xA1  DATA | Input handshake type
    #   [1] 0x01  Report ID = keyboard
    #   [2] 0x00  modifier bitmask (none)
    #   [3] 0x00  reserved
    #   [4..9]    up to six pressed keycodes (all zero = no keys)
    _KEEPALIVE_REPORT = bytes([0xA1, 0x01, 0x00, 0x00,
                               0x00, 0x00, 0x00, 0x00, 0x00, 0x00])

    # How often to tickle iOS. iOS's observed drop window is around
    # 10 minutes of zero HID traffic, so sending every ~2 minutes
    # gives us a comfortable safety margin while being essentially
    # free on the wire (10 bytes per tick).
    _KEEPALIVE_INTERVAL_S = 120.0

    @classmethod
    def _keepalive_loop(cls):
        """Send a zeroed keyboard input report every couple of minutes
        while connected, so iOS doesn't age out the HID link.

        Exits promptly when cls.connected flips false or the process is
        shutting down. The send() path itself goes through _safe_send,
        which locks around the socket and calls _drop_client on error
        — so a keep-alive on a dying link simply trips disconnect
        detection slightly earlier, which is fine.
        """
        import time
        while cls._running and cls.connected:
            # Wait first: sending immediately after connect would be
            # noisy (iOS has just paged us; it knows we're alive) and
            # could race with our HID descriptor write.
            deadline = time.monotonic() + cls._KEEPALIVE_INTERVAL_S
            while (cls._running and cls.connected
                   and time.monotonic() < deadline):
                time.sleep(1.0)
            if not (cls._running and cls.connected):
                break
            cls.send_to_interrupt_channel(cls._KEEPALIVE_REPORT)

    @classmethod
    def _phantom_session_watchdog(cls, mac_address:str):
        """Detect and recover from "L2CAP up, HID down" iOS sessions.

        Background: when our reconnect watchdog peripheral-initiates
        the HID L2CAPs (PSM 0x11 + 0x13) after iOS has timed out the
        keyboard side of the bond — typically following a long
        reconnect-storm of "page failed errno=112: Host is down" —
        the iOS BT kernel will accept both PSMs at the link level
        but NOT promote them to a HID Host session. From our side
        every send() succeeds and the socket stays open indefinitely,
        but iOS silently drops every HID input report we transmit
        and the on-screen keyboard pops up in text fields. Both ends
        "look connected" because ACL + L2CAP are up.

        Detection criterion: did iOS send any non-HANDSHAKE
        transaction on the control channel within the detection
        window? If yes → engaged, exit quietly. If no → silent or
        HANDSHAKE-only → phantom, tear down.

        We deliberately accept SET_PROTOCOL-only sessions as
        healthy. Empirically some iOS bonds emit ONLY SET_PROTOCOL
        on a perfectly working session, so requiring more
        diversity caused false-positive teardown loops. The
        watchdog also bumps _phantom_teardowns_this_run so the
        spawn-side guard can disarm us after
        _PHANTOM_TEARDOWN_MAX_PER_RUN actions — that's the
        backstop against feedback loops in case our criterion is
        still wrong on some other bond.
        """
        deadline = time.monotonic() + cls._PHANTOM_DETECT_TIMEOUT_S
        while cls._running and cls.connected and time.monotonic() < deadline:
            seen = cls._session_substantive_types_seen
            if seen:
                try:
                    from worker import _wdiag as _wd
                    types_hex = sorted(f'0x{t:x}' for t in seen)
                    _wd(f'phantom-watch: {mac_address} engaged HID '
                        f'(types={types_hex}) — exiting watchdog')
                except Exception:
                    pass
                return
            time.sleep(0.25)

        if not (cls._running and cls.connected):
            return

        if cls._session_substantive_types_seen:
            return

        cls._phantom_teardowns_this_run += 1
        _tlog(f'[phantom] {mac_address}: no substantive HID Host '
              f'traffic in {cls._PHANTOM_DETECT_TIMEOUT_S:.1f}s '
              f'(silence or HANDSHAKE-only) — iOS HID Host is not '
              f'bound. Tearing link down so a fresh page can '
              f're-engage. (teardown '
              f'{cls._phantom_teardowns_this_run}/'
              f'{cls._PHANTOM_TEARDOWN_MAX_PER_RUN} this run)')

        # Drop our sockets first so iOS sees the L2CAP go away, then
        # ask BlueZ to terminate the ACL. The order matters: closing
        # our side first means iOS gets a clean L2CAP disconnect
        # before we ask for ACL teardown, which gives its stack the
        # best chance of rebuilding a proper HID session on the next
        # page.
        cls._drop_client()
        cls._force_peer_acl_down(mac_address)

    @classmethod
    def _watch_connection(cls):
        """Read host-originated HID control transactions and respond.

        iOS sends SET_PROTOCOL / SET_REPORT / SET_IDLE early in the
        session and expects a HANDSHAKE reply for each. If we stay
        silent it tears the link down in under a minute. Any recv()
        returning b'' / raising means the peer is really gone — fall
        through to _drop_client()."""

        sock = cls.client_control_socket
        if sock is None: return

        try:
            while cls.connected:
                data = sock.recv(1024)
                if not data: break
                cls._handle_control_transaction(data)
        except OSError:
            pass
        finally:
            if cls.connected:
                print('client disconnected')
            cls._drop_client()

    @classmethod
    def _drop_client(cls):

        with cls._lock:
            already_dropped = not cls.connected and cls.client_control_socket is None
            if already_dropped:
                cls._disconnect_event.set()
                return

            cls.connected = False
            cls.client_display_name = None

            for attr in ('client_control_socket', 'client_interrupt_socket'):
                sock = getattr(cls, attr)
                if sock is not None:
                    try: sock.close()
                    except OSError: pass
                    setattr(cls, attr, None)

        cls._disconnect_event.set()
        # Kick the reconnect watchdog so it tries to page the peer
        # right away instead of waiting out its current sleep — this
        # is the same reflex a real BT keyboard has when it loses
        # link: it starts paging the host again immediately.
        cls._reconnect_wake.set()

    @classmethod
    def disconnect_client(cls):
        """Cleanly terminate the current peer connection (ACL + L2CAP) but
        keep our L2CAP server sockets open so we're immediately ready for
        a fresh connection. This is what the tray's "Disconnect" menu
        item calls.

        Also briefly suppresses the auto-reconnect watchdog so the
        disconnect actually sticks (otherwise we'd page the peer again
        within seconds and undo the user's explicit action).
        """
        import time
        cls._reconnect_suppressed_until = time.monotonic() + 60.0
        cls._disconnect_all_devices()
        cls._drop_client()

    @classmethod
    def wait_until_disconnected(cls):
        cls._disconnect_event.wait()

    @classmethod
    def is_connected(cls) -> bool:
        return cls.connected

    @classmethod
    def stop(cls):

        cls._running = False
        # Wake the reconnect watchdog so it notices _running==False
        # and exits instead of staying wedged in a sleep.
        cls._reconnect_wake.set()

        # Before closing our L2CAP sockets, tear down the ACL on any
        # connected peer. Without this, iOS keeps the link open until its
        # 40-second supervision timeout fires — during which the phone
        # still thinks the keyboard is "connected" and REFUSES to open a
        # fresh connection when the app restarts. Disconnecting cleanly
        # here makes instant reconnect-on-next-launch work.
        cls._disconnect_all_devices()

        cls._drop_client()

        for attr in ('_control_server_socket', '_interrupt_server_socket'):
            sock = getattr(cls, attr)
            if sock is not None:
                try: sock.close()
                except OSError: pass
                setattr(cls, attr, None)

        if cls._device_signal_match is not None:
            try: cls._device_signal_match.remove()
            except Exception: pass
            cls._device_signal_match = None

        if cls._glib_mainloop is not None:
            try: cls._glib_mainloop.quit()
            except Exception: pass

        # Intentionally do NOT call `bluetoothctl remove` here.
        # Keeping the bond on the BlueZ side is what lets iOS reconnect to
        # us on next launch without a fresh pairing dance.

    @classmethod
    def _disconnect_all_devices(cls):
        """Ask bluez to close the ACL on every currently-connected peer.

        Called on app shutdown so iOS notices the disconnect immediately
        (instead of waiting out a ~40s supervision timeout) and is ready
        to reconnect as soon as we come back up.
        """
        bus = cls._bus
        if bus is None: return
        try:
            root = bus.get_object(BLUEZ_SERVICE_NAME, '/')
            mgr = Interface(root, DBUS_OBJECT_MAPPER_INTERFACE)
            objects = mgr.GetManagedObjects()
        except Exception as e:
            print(f'[bluez] could not enumerate devices on shutdown: {e}')
            return

        for path, interfaces in objects.items():
            dev = interfaces.get('org.bluez.Device1')
            if not dev: continue
            if not bool(dev.get('Connected', False)): continue
            addr = dev.get('Address', '?')
            try:
                device = bus.get_object(BLUEZ_SERVICE_NAME, path)
                iface = Interface(device, 'org.bluez.Device1')
                print(f'[bluez] disconnecting {addr} for clean shutdown')
                iface.Disconnect()
            except Exception as e:
                print(f'[bluez] failed to disconnect {addr}: {e}')

    @classmethod
    def _is_adapter_powered(cls) -> bool:
        """Cheap D-Bus read of Adapter1.Powered. False on any error
        (bus gone, adapter gone, property missing) so callers can use it
        as a go / no-go gate without try/except at every site."""
        bus = cls._bus
        path = cls._adapter_path
        if bus is None or path is None:
            return False
        try:
            adapter = bus.get_object(BLUEZ_SERVICE_NAME, path)
            props = Interface(adapter, DBUS_PROPERTIES_INTERFACE)
            return bool(props.Get(BLUEZ_ADAPTER_INTERFACE, 'Powered'))
        except Exception:
            return False

    @classmethod
    def _force_peer_acl_down(cls, mac_address:str) -> None:
        """Ask BlueZ to drop the ACL to a paired peer.

        Used by the reconnect watchdog when iOS leaves us stuck in
        "ACL up, HID down" (power-save idle) — tearing the link
        down forces iOS to mark us offline, after which a fresh
        page reliably triggers a full reconnect with HID open.

        Best-effort: errors are logged and swallowed."""
        bus = cls._bus
        if bus is None or not mac_address:
            return
        dev_path = f'/org/bluez/hci0/dev_{mac_address.replace(":", "_")}'
        try:
            device = bus.get_object(BLUEZ_SERVICE_NAME, dev_path)
            iface = Interface(device, 'org.bluez.Device1')
            iface.Disconnect(timeout=10)
        except dbus.exceptions.DBusException as e:
            name = getattr(e, 'get_dbus_name', lambda: '?')()
            msg = getattr(e, 'get_dbus_message', lambda: str(e))()
            # Expected when the ACL is already gone by the time we
            # get here — nothing to do.
            if 'Not Connected' in msg or 'not connected' in msg:
                return
            _tlog(f'[reconnect] {mac_address}: force-disconnect '
                  f'raised {name}: {msg}')
        except Exception as e:
            _tlog(f'[reconnect] {mac_address}: force-disconnect '
                  f'{type(e).__name__}: {e}')

    @classmethod
    def _bluez_last_acl_drop_within(cls, since:float, window:float) -> bool:
        """Was there a peer ACL drop (Connected: True -> False from the
        BlueZ signal) within `window` seconds of the `since` monotonic
        timestamp? Used by the reconnect watchdog to detect iOS's
        "accept then reject in <5s" refusal pattern.
        """
        drop_at = cls._last_peer_acl_drop_at
        if drop_at <= 0.0:
            return False
        return drop_at >= since and (drop_at - since) <= window

    @classmethod
    def _peer_acl_connected(cls, mac_address:str) -> bool:
        """Quick D-Bus read of Device1.Connected for a paired peer.

        Returns True only if BlueZ currently shows an ACL up to the
        MAC. False on any error, or if the peer has never been seen
        (no Device1 object yet). Used by the reconnect watchdog to
        avoid initiating a fresh page while iOS is already mid-way
        through establishing a link."""
        bus = cls._bus
        if bus is None or not mac_address:
            return False
        dev_path = f'/org/bluez/hci0/dev_{mac_address.replace(":", "_")}'
        try:
            device = bus.get_object(BLUEZ_SERVICE_NAME, dev_path)
            props = Interface(device, DBUS_PROPERTIES_INTERFACE)
            return bool(props.Get('org.bluez.Device1', 'Connected'))
        except Exception:
            return False

    @classmethod
    def _try_outbound_reconnect(cls, mac_address:str) -> bool:
        """Page a paired peer and bring up the ACL — and ONLY the ACL.

        This is what a real Bluetooth keyboard does on power-up: it
        issues HCI_Create_Connection to the paired host, the ACL
        comes up, and then the *host* (iPhone) drives everything
        else — SDP, SSP re-auth, HID L2CAP open. We mimic that
        minimally by opening a raw L2CAP socket to the peer's SDP
        PSM and immediately closing it: the kernel pages the peer,
        the ACL comes up, SDP connection succeeds, we close. BlueZ
        keeps the ACL alive a few seconds on idle, which is the
        exact window iOS uses to open HID PSM 0x11 back to us.

        Why this and not Device1.Connect()? BlueZ's Device1.Connect()
        does a *lot* on top of opening the ACL: full SDP browse of
        the peer, attempt to connect every one of the peer's
        advertised profiles (A2DP Source, HFP AG, MAP, PBAP, etc.)
        using whatever matching client drivers bluez has registered.
        When none succeed — and for iOS->Linux HID that is always
        the case, because we're the HID *server*, not a client for
        any iPhone service — BlueZ documented behaviour is to
        "disconnect the device". That drops the ACL within ~1s of
        it coming up, yanked away from iOS before it can open HID.
        Raw L2CAP connect sidesteps all of that.

        Returns True if the ACL is up (either already or after our
        page), False otherwise (peer off / out of range / unpaired).
        """
        if not mac_address:
            return False

        bus = cls._bus
        if bus is None:
            return False

        if not cls._is_adapter_powered():
            return False

        dev_path = f'/org/bluez/hci0/dev_{mac_address.replace(":", "_")}'

        try:
            device = bus.get_object(BLUEZ_SERVICE_NAME, dev_path)
            props = Interface(device, DBUS_PROPERTIES_INTERFACE)
        except dbus.exceptions.DBusException:
            return False

        try:
            paired = bool(props.Get('org.bluez.Device1', 'Paired'))
        except dbus.exceptions.DBusException:
            paired = False
        if not paired:
            # Never bonded (or bond removed on the phone). We can't
            # auto-reconnect — only the user tapping our name on the
            # iPhone's BT screen can fix this.
            return False

        try:
            if bool(props.Get('org.bluez.Device1', 'Connected')):
                return True
        except dbus.exceptions.DBusException:
            pass

        # Page via raw L2CAP SDP connect. PSM 1 is the SDP PSM and is
        # always open on every classic-BT device. Binding to our own
        # adapter MAC keeps us on hci0 even if there's somehow
        # another adapter present.
        #
        # We deliberately DO NOT close the socket here. If we closed
        # it, bluez's L2CAP idle timer would tear the ACL back down
        # within ~1s — far too fast for iOS to notice "my keyboard
        # is back, let me open HID" and complete the L2CAP 0x11
        # handshake. By keeping the SDP socket alive we keep the
        # ACL alive, which gives iOS the 10-20s it often needs to
        # drive the reconnect. A background holder thread closes
        # the socket once HID L2CAP lands on our side (cls.connected
        # flips True) or 30s elapses — whichever first.
        SDP_PSM = 0x0001
        sdp_sock = socket(AF_BLUETOOTH, SOCK_SEQPACKET, BTPROTO_L2CAP)
        try:
            sdp_sock.settimeout(15.0)
            if cls.server_mac_address:
                try:
                    sdp_sock.bind((cls.server_mac_address, 0))
                except OSError:
                    # Bind is best-effort; kernel picks hci0 anyway.
                    pass
            _tlog(f'[reconnect] paging {mac_address}...')
            t0 = time.monotonic()
            sdp_sock.connect((mac_address, SDP_PSM))
            # Back to blocking-mode with no timeout so the holder
            # thread's recv() can park indefinitely (we close the
            # socket from outside to wake it).
            sdp_sock.settimeout(None)
            _tlog(f'[reconnect] {mac_address}: ACL up after '
                  f'{time.monotonic()-t0:.2f}s '
                  f'(holding SDP to keep ACL alive for iOS)')
            import threading as _threading
            holder = _threading.Thread(
                target=cls._hold_sdp_socket,
                args=(mac_address, sdp_sock),
                name='toothkey-sdp-hold',
                daemon=True,
            )
            holder.start()
            return True
        except OSError as e:
            try:
                sdp_sock.close()
            except OSError:
                pass
            # Typical "peer is unavailable" errnos on AF_BLUETOOTH:
            #   111 ECONNREFUSED  (peer refused SDP — rare)
            #   110 ETIMEDOUT     (page timeout, peer silent)
            #   112 EHOSTDOWN     (peer off)
            #   113 EHOSTUNREACH  (peer unreachable)
            #   101 ENETUNREACH   (adapter routing issue)
            #   16  EBUSY         (page already in progress)
            # None of these are log-worthy — the watchdog will just
            # back off and retry.
            if e.errno not in (101, 110, 111, 112, 113, 16):
                _tlog(f'[reconnect] {mac_address}: '
                      f'L2CAP connect errno={e.errno}: {e}')
            else:
                # Log these at the debug level via _wdiag only,
                # so toothkey.log doesn't fill up with "page failed"
                # lines during long disconnected stretches but we
                # still have the evidence in worker-diag.log.
                try:
                    from worker import _wdiag as _wd
                    _wd(f'[reconnect] {mac_address}: page failed '
                        f'errno={e.errno}: {e}')
                except Exception:
                    pass
            return False

    @classmethod
    def _try_open_hid_outbound(cls, mac_address:str) -> bool:
        """Peripheral-initiated HID L2CAP open.

        Classic Bluetooth HID allows EITHER end of an established ACL
        to initiate the HID L2CAP channels (spec: BT HID Profile 1.1.1,
        "5.2.2 Connection Establishment"). A real Bluetooth keyboard
        takes advantage of this: after paging the host, if it has a
        keypress to send, it opens PSM 0x11 (control) and PSM 0x13
        (interrupt) from its side and just writes the HID input report.
        The host's BT stack (always listening on those PSMs on its side
        as a HID Host) accepts the channels and routes the report to
        the focused text field.

        That's the one trick Apple can't really opt out of without
        breaking every keyboard on the market. If we successfully open
        both L2CAPs from our side, we have HID up regardless of
        whether iOS was going to open it on its own or not.

        Caveat: if the peer's HID Host isn't listening (e.g. user
        turned BT off on the phone, or phone is some device that
        isn't a HID host), the connect() will fail with ECONNREFUSED
        and we fall back to the server-side accept() path. That's
        safe — we simply haven't improved anything in that case.

        Returns True on success (both channels open, handed to
        wait_for_client via _accept_wake_w), False otherwise.
        """
        if cls.connected:
            # HID already up via the normal inbound accept() path.
            # Don't race.
            return False

        ctrl = socket(AF_BLUETOOTH, SOCK_SEQPACKET, BTPROTO_L2CAP)
        intr = socket(AF_BLUETOOTH, SOCK_SEQPACKET, BTPROTO_L2CAP)
        try:
            # Match the security level we demand on our listening
            # sockets. iOS's HID Host side refuses to accept an
            # incoming L2CAP at anything less than HIGH (MITM).
            try:
                ctrl.setsockopt(SOL_BLUETOOTH, BT_SECURITY,
                                struct.pack('II', BT_SECURITY_HIGH, 0))
                intr.setsockopt(SOL_BLUETOOTH, BT_SECURITY,
                                struct.pack('II', BT_SECURITY_HIGH, 0))
            except OSError:
                pass

            if cls.server_mac_address:
                try:
                    ctrl.bind((cls.server_mac_address, 0))
                    intr.bind((cls.server_mac_address, 0))
                except OSError:
                    pass

            ctrl.settimeout(10.0)
            intr.settimeout(10.0)

            _tlog(f'[reconnect] {mac_address}: trying peripheral-'
                  f'initiated HID L2CAP 0x11 (control)')
            t0 = time.monotonic()
            ctrl.connect((mac_address, CONTROL_CHANNEL))
            _tlog(f'[reconnect] {mac_address}: HID control connected '
                  f'after {time.monotonic()-t0:.2f}s')

            _tlog(f'[reconnect] {mac_address}: trying peripheral-'
                  f'initiated HID L2CAP 0x13 (interrupt)')
            t1 = time.monotonic()
            intr.connect((mac_address, INTERRUPT_CHANNEL))
            _tlog(f'[reconnect] {mac_address}: HID interrupt connected '
                  f'after {time.monotonic()-t1:.2f}s')

            ctrl.settimeout(None)
            intr.settimeout(None)

            cls._promote_outbound_hid(ctrl, intr, mac_address)
            return True
        except OSError as e:
            _tlog(f'[reconnect] {mac_address}: peripheral-initiated '
                  f'HID failed errno={e.errno}: {e}')
            for s in (ctrl, intr):
                try: s.close()
                except OSError: pass
            return False

    @classmethod
    def _promote_outbound_hid(cls, ctrl, intr, mac_address:str) -> None:
        """Hand a pair of peripheral-initiated HID sockets to
        wait_for_client().

        Stores them in _outbound_pending and pokes the wake pipe so
        any in-flight select() returns immediately. wait_for_client
        sees the flag, adopts the sockets, and proceeds through the
        same "send descriptor, start watcher, start keepalive" path
        that the inbound accept() branch uses.
        """
        cls._outbound_pending = (ctrl, intr, mac_address)
        w = cls._accept_wake_w
        if w > 0:
            try:
                os.write(w, b'x')
            except (BlockingIOError, OSError):
                pass

    @classmethod
    def _hold_sdp_socket(cls, mac_address:str, sdp_sock):
        """Keep the SDP L2CAP socket alive so BlueZ doesn't idle-close
        the ACL out from under iOS.

        This is the companion to _try_outbound_reconnect's page: after
        we page and the ACL comes up, we need the ACL to stay up long
        enough for iOS to run its own reconnect machinery (SDP re-
        browse if needed, then open HID PSM 0x11 back to us).
        Closing the SDP socket immediately drops the ACL in ~1s,
        which is far too fast.

        We hold the socket until whichever comes first:
          - cls.connected goes True (HID L2CAP landed — iOS is in)
          - 30 seconds elapse (iOS didn't bite; let the watchdog
            try again on the next loop iteration)
          - we're shutting down
        """
        import time
        deadline = time.monotonic() + 30.0
        try:
            while cls._running and time.monotonic() < deadline:
                if cls.connected:
                    break
                time.sleep(0.25)
        finally:
            try:
                sdp_sock.close()
            except OSError:
                pass

    @classmethod
    def _reconnect_watchdog_loop(cls):
        """Background thread that emulates a real keyboard's "page the
        host on power-up" behaviour — but carefully.

        iOS re-establishes HID on its own when our adapter comes back
        up; we only need to page when it's *not* trying. Paging too
        eagerly actively breaks reconnect: every page wakes iPhone's
        BT stack and may put it into a "deciding what to do" state
        that briefly accepts the ACL then drops us.

        Cadence in this implementation (measured from the disconnect
        timestamp):

            0-60s   aggressive burst  (every ~15s)
                   Covers "user walked out of range and came back" —
                   the fastest possible reconnect path.
            60-300s moderate         (every ~60s)
                   Covers "phone is nearby but screen is locked".
            >300s   patient          (every ~180s)
                   Covers "phone is in another room / powered off".
                   Paging more often than this only wastes battery.

        On top of that we track "churn": if our page succeeds and the
        ACL comes up, then drops within 5 seconds WITHOUT HID opening,
        iOS is actively refusing the connection. Paging harder makes
        that worse — each repeat just refreshes its refusal timer. We
        detect this and force a one-minute cooldown so iOS's stack
        can actually forget about us enough to reconnect cleanly on
        its next attempt.
        """
        # On startup, give bluez a moment to finish settling after
        # profile registration, then get right to paging. Earlier
        # versions slept 8s here hoping iOS would reconnect on its
        # own first — but on a fresh launch iOS isn't doing
        # anything; our adapter just became available and the phone
        # won't notice until it happens to do its next periodic
        # scan (which can be many tens of seconds away). Just page
        # immediately.
        time.sleep(1.0)

        dead_adapter_notified = False

        # --- Churn detection ---------------------------------------
        # Monotonic time at which we last observed ACL up for the
        # peer (whether via our page or via iOS). Used together with
        # the PropertiesChanged [bluez] log events to detect the
        # "page → ACL up → ACL drops in <5s with no HID" pattern.
        # When we see the peer's Connected flag transition True
        # after _last_page_at_monotonic is populated, we record that
        # time in _last_acl_up_at_monotonic; if Connected then flips
        # False with no HID in between, we know iOS is refusing us
        # and increment _churn_streak.
        # (Those flags are populated from _bluez_device_property_event,
        # wired up further down — see _subscribe_to_device_events.)
        churn_streak = 0
        attempt_count = 0        # total pages since last connected
        session_start = time.monotonic()
        # When did we first notice the peer's ACL up without a matching
        # HID L2CAP channel on our side? iOS in power-save will happily
        # hold an ACL open indefinitely and never open HID on its own.
        stuck_acl_since = None

        while cls._running:
            if cls.connected:
                cls._reconnect_wake.clear()
                cls._reconnect_wake.wait(timeout=60.0)
                # Reset everything — the moment HID is up we're in
                # "connected" state and all the failure counters
                # should start fresh for the next disconnect event.
                dead_adapter_notified = False
                stuck_acl_since = None
                churn_streak = 0
                attempt_count = 0
                session_start = time.monotonic()
                continue

            if not cls._is_adapter_powered():
                if not dead_adapter_notified:
                    _tlog('[reconnect] adapter is not powered; '
                          'trying to bring it up before paging peer')
                    dead_adapter_notified = True
                cls._ensure_adapter_up()
                try:
                    if cls._adapter_path:
                        adapter = cls._bus.get_object(
                            BLUEZ_SERVICE_NAME, cls._adapter_path)
                        props = Interface(adapter, DBUS_PROPERTIES_INTERFACE)
                        props.Set(BLUEZ_ADAPTER_INTERFACE,
                                  'Powered', dbus.Boolean(True))
                except Exception:
                    pass
                cls._reconnect_wake.clear()
                cls._reconnect_wake.wait(timeout=10.0)
                continue

            dead_adapter_notified = False

            mac = cls.client_mac_address
            if not mac:
                cls._reconnect_wake.clear()
                cls._reconnect_wake.wait(timeout=15.0)
                continue

            now = time.monotonic()
            if now < cls._reconnect_suppressed_until:
                cls._reconnect_wake.clear()
                cls._reconnect_wake.wait(
                    timeout=max(1.0, cls._reconnect_suppressed_until - now))
                continue

            # If the peer already has an open ACL to us, give iOS a
            # shot at driving the handshake without interference. But
            # if this "ACL up, HID down" state persists, iOS is
            # almost certainly in power-save / idle with no plan to
            # open HID on its own — the iPhone BT screen will show
            # "Connected" while we show disconnected, and typing is
            # impossible. In that case, cycle the link: tear the ACL
            # down (forcing iOS to mark us offline) and re-page. On
            # a fresh page iOS drives a full reconnect including HID
            # L2CAP open.
            if cls._peer_acl_connected(mac):
                if stuck_acl_since is None:
                    stuck_acl_since = now
                stuck_for = now - stuck_acl_since
                STUCK_GRACE_S = 20.0
                if stuck_for < STUCK_GRACE_S:
                    cls._reconnect_wake.clear()
                    cls._reconnect_wake.wait(
                        timeout=STUCK_GRACE_S - stuck_for)
                    continue

                _tlog(f'[reconnect] {mac}: ACL has been idle '
                      f'({stuck_for:.0f}s) with no HID L2CAP; '
                      f'cycling link to force iOS to re-open HID')
                cls._force_peer_acl_down(mac)
                stuck_acl_since = None
                cls._reconnect_wake.clear()
                cls._reconnect_wake.wait(timeout=2.0)
                continue

            stuck_acl_since = None

            # --- Churn cooldown ----------------------------------------
            # If we observed several page-succeeds-then-ACL-drops-fast
            # cycles, iOS is actively pushing us away. Back off for a
            # full minute so its stack can actually forget about us.
            if churn_streak >= 3:
                wait_s = min(60.0 * churn_streak, 180.0)
                _tlog(f'[reconnect] {mac}: detected {churn_streak} rapid '
                      f'ACL churns — iOS is refusing; cooling down for '
                      f'{wait_s:.0f}s (tap a text field on iPhone to '
                      f'force a fresh HID open, or toggle BT on iPhone)')
                cls._reconnect_wake.clear()
                cls._reconnect_wake.wait(timeout=wait_s)
                # After the cooldown we give the streak one free pass:
                # maybe the phone's ready to accept again, so try one
                # more page at "moderate" cadence.
                churn_streak = 1
                continue

            # --- Pick cadence based on how long we've been disconnected
            # We use `session_start` (monotonic time of disconnect) to
            # decide cadence. Crucially, `session_start` gets reset
            # below whenever a page SUCCEEDS (ACL up) even if HID fails
            # to open — because a successful page is proof the peer is
            # reachable again, which drops us out of "patient" mode and
            # back into aggressive retries. Without that reset we'd
            # sit on a 3-minute wait after the first page-succeeds-
            # but-HID-refused event, which is exactly the failure mode
            # that manifested as "took ~3 extra minutes to reconnect
            # after coming back into range".
            disconnected_for = now - session_start
            if disconnected_for < 60.0:
                retry_after_success = 15.0   # aggressive burst
            elif disconnected_for < 300.0:
                retry_after_success = 60.0   # moderate
            else:
                retry_after_success = 180.0  # patient

            # Snapshot Connected before page so post-page ACL-drop
            # detection (below) can tell iOS accepted-then-rejected
            # vs. never-accepted.
            cls._last_page_attempt_at = now
            ok = cls._try_outbound_reconnect(mac)
            attempt_count += 1

            if not cls._running:
                break

            if ok:
                # Step 1: give iOS a short window (3s) to open HID
                # itself. On a warm reconnect this is usually
                # instantaneous; if HID opens, wait_for_client() will
                # flip cls.connected and we bail out of this whole
                # branch.
                for _ in range(12):
                    if cls.connected or not cls._running:
                        break
                    time.sleep(0.25)

                # Step 2: if iOS didn't drive HID, try to open it
                # ourselves (peripheral-initiated). This is the trick
                # real BT keyboards use to force a reconnect: with the
                # ACL already up, open PSM 0x11 and 0x13 TO the peer.
                # iOS listens on both as a HID Host; if it accepts,
                # we've got HID without needing iOS to "decide" it
                # wants a keyboard.
                if (not cls.connected
                        and cls._running
                        and cls._peer_acl_connected(mac)):
                    cls._try_open_hid_outbound(mac)

                # Peer was reachable this round (ACL came up). Treat
                # this as a *fresh* disconnect window for cadence
                # purposes — we want aggressive 15s retries now, not
                # patient 180s, because the most likely reason this
                # attempt didn't fully succeed is that iOS hadn't
                # committed to reopen HID yet and just needs another
                # nudge in a few seconds.
                if disconnected_for >= 60.0:
                    _tlog(f'[reconnect] {mac}: peer back in range — '
                          f'resetting cadence to aggressive (was '
                          f'{retry_after_success:.0f}s)')
                session_start = time.monotonic()
                disconnected_for = 0.0
                retry_after_success = 15.0

                # Step 3: continue waiting. If wait_for_client adopted
                # the outbound sockets (or accepted a fresh inbound
                # pair), cls.connected will be True by the time we
                # finish this sleep.
                remaining = max(1.0, retry_after_success - 3.0)
                cls._reconnect_wake.clear()
                cls._reconnect_wake.wait(timeout=remaining)

                if cls.connected:
                    pass
                else:
                    # ACL dropped quickly after page, no HID in between
                    # == iOS actively refused us. Bump churn streak.
                    dropped_fast = cls._bluez_last_acl_drop_within(
                        since=now, window=5.0)
                    if dropped_fast:
                        churn_streak += 1
                        _tlog(f'[reconnect] {mac}: ACL dropped '
                              f'<5s after page with no HID '
                              f'(churn={churn_streak})')
                    # ACL stayed up but HID never opened == "phone
                    # on lock screen / user not typing". Not churn.
            else:
                # Page failed (peer unreachable / page timeout). Next
                # page delay scales with how long we've been trying
                # to reach them.
                cls._reconnect_wake.clear()
                cls._reconnect_wake.wait(
                    timeout=min(retry_after_success, 30.0))

    @staticmethod
    def _initiate_pairing(bus:SystemBus, device_path:str):
        """Kick off SSP from our (peripheral) side.

        Classic-HID peripherals traditionally initiate authentication right
        after the ACL comes up, and iOS/iPadOS banks on that. If we just
        sit there, the iPhone opens an ACL, waits ~10s for us to send
        HCI_Authentication_Requested, then gives up with reason 3 /
        status 0x0E — no SDP, no SSP, no agent callbacks, nothing. Calling
        Device1.Pair() here asks bluez to drive the authentication, which
        triggers IO-Capability-Request → Numeric Comparison → User-
        Confirmation through our registered agent. iOS's pairing prompt
        appears immediately once this fires.

        Runs in a background thread because Pair() is a long-running
        (blocking) D-Bus method and the signal-dispatch thread must not
        stall.
        """
        def _do_pair():
            try:
                device = bus.get_object(BLUEZ_SERVICE_NAME, device_path)
                props = Interface(device, DBUS_PROPERTIES_INTERFACE)
                try:
                    already = bool(props.Get('org.bluez.Device1', 'Paired'))
                except Exception:
                    already = False
                if already:
                    print(f'[pair] skipping Pair() on {device_path}: already paired')
                    return
                iface = Interface(device, 'org.bluez.Device1')
                print(f'[pair] initiating Device1.Pair() on {device_path}')
                # dbus-python's default method-call timeout is 25s, but the
                # user may take longer than that to tap "Pair" on the
                # iPhone. Bump to 120s so the PropertiesChanged storm and
                # the Pair() return value stay in the expected order in
                # the log (otherwise we get a spurious NoReply error line
                # even though pairing completes successfully).
                iface.Pair(timeout=120)
                print(f'[pair] Device1.Pair() completed OK on {device_path}')
            except dbus.exceptions.DBusException as e:
                # Common non-fatal outcomes:
                #   org.bluez.Error.AlreadyExists      — already bonded
                #   org.bluez.Error.AuthenticationFailed — user denied / PIN mismatch
                #   org.bluez.Error.ConnectionAttemptFailed — peer walked away
                #   org.bluez.Error.InProgress         — bluez already pairing
                name = getattr(e, 'get_dbus_name', lambda: '?')()
                msg = getattr(e, 'get_dbus_message', lambda: str(e))()
                print(f'[pair] Device1.Pair() on {device_path} failed: {name}: {msg}')
            except Exception as e:
                print(f'[pair] Device1.Pair() on {device_path} failed: {type(e).__name__}: {e}')

        threading.Thread(target=_do_pair, daemon=True,
                         name=f'pair-{device_path[-6:]}').start()

    @staticmethod
    def _reassert_default_agent(bus:SystemBus):
        """Re-call RequestDefaultAgent on the AgentManager.

        On a KDE / GNOME desktop, kded's `bluedevil` module (or gnome-
        shell's) registers its own BT agent and can become the default
        agent. If that happens while we're idle, SSP with iOS resolves
        to whatever capability *that* agent advertises (typically
        NoInputNoOutput -> "Just Works"), which iOS rejects for HID.
        We reassert ourselves every time a new peer appears so we're
        guaranteed to be the one bluez asks for IO capability / numeric
        confirmation.
        """
        try:
            obj = bus.get_object(BLUEZ_SERVICE_NAME, '/org/bluez')
            mgr = Interface(obj, 'org.bluez.AgentManager1')
            mgr.RequestDefaultAgent('/org/bluez/AuthorizeServiceAgent')
            print('[agent] reasserted as default (beat any competing agent like bluedevil)')
        except Exception as e:
            print(f'[agent] could not reassert default: {e}')

    @classmethod
    def _subscribe_to_device_events(cls, bus:SystemBus):
        """Log BlueZ Device1 lifecycle events so pairing failures aren't silent.

        We listen for InterfacesAdded (new peer shows up), InterfacesRemoved,
        and PropertiesChanged on any org.bluez.Device1 (so Paired/Connected
        transitions get logged in real time)."""

        try:
            mgr = Interface(bus.get_object(BLUEZ_SERVICE_NAME, '/'), DBUS_OBJECT_MAPPER_INTERFACE)

            def on_interfaces_added(path, interfaces):
                dev = interfaces.get('org.bluez.Device1')
                if not dev: return
                addr = dev.get('Address')
                name = dev.get('Name') or dev.get('Alias')
                # Cache the name so the tray can label "Disconnect <name>"
                # without having to do its own D-Bus lookup.
                if addr:
                    cls.client_display_name = str(name) if name else str(addr)
                _tlog(f'[bluez] device appeared: {addr} "{name}" at {path}')
                interesting = ('Paired', 'Bonded', 'Connected', 'Trusted', 'Blocked', 'LegacyPairing')
                snapshot = {k: _stringify(dev.get(k)) for k in interesting if k in dev}
                if snapshot:
                    _tlog(f'[bluez] initial state: {snapshot}')
                # A competing agent (e.g. KDE's bluedevil) may have been
                # elected default while we were idle. Race it to the punch
                # before SSP starts: reassert our agent as default the
                # instant any new peer appears.
                cls._reassert_default_agent(bus)
                # iOS waits for peripheral to initiate authentication on
                # HID. Kick it off now so the iPhone sees SSP traffic
                # within its internal timeout window and shows its pairing
                # prompt instead of silently disconnecting.
                #
                # Android, however, drives SSP from its own side when it
                # pages us — calling Pair() here in parallel races the
                # phone's bonding attempt and produces a 30s
                # AUTH_FAILED. Skip the kickoff if the peer is already
                # Connected when we first see it (phone-initiated ACL):
                # only iOS reaches this callback with Connected=False
                # because iOS pages us only after we've shown up in its
                # scan list and we then have to drive auth ourselves.
                already_connected = bool(dev.get('Connected'))
                if already_connected:
                    _tlog(f'[pair] skipping Device1.Pair() — peer paged us (Connected=True at appearance); letting it drive SSP')
                else:
                    cls._initiate_pairing(bus, str(path))

            def on_interfaces_removed(path, interfaces):
                if 'org.bluez.Device1' in interfaces:
                    _tlog(f'[bluez] device removed: {path}')

            mgr.connect_to_signal('InterfacesAdded', on_interfaces_added)
            mgr.connect_to_signal('InterfacesRemoved', on_interfaces_removed)

            def on_props_changed(interface, changed, invalidated, path=None):
                if interface != 'org.bluez.Device1': return
                if changed:
                    # Log everything so we can see exactly what BlueZ is doing
                    # during pairing/encryption setup.
                    pretty = {str(k): _stringify(v) for k, v in changed.items()}
                    _tlog(f'[bluez] {path}: {pretty}')
                    # Track the peer's ACL transitioning True -> False
                    # so the reconnect watchdog can tell "iOS accepted
                    # then rejected us within seconds" (churn) from
                    # a benign drop long after the page.
                    if 'Connected' in changed and not bool(changed['Connected']):
                        cls._last_peer_acl_drop_at = time.monotonic()
                if invalidated:
                    _tlog(f'[bluez] {path}: invalidated={[str(x) for x in invalidated]}')

            cls._device_signal_match = bus.add_signal_receiver(
                on_props_changed,
                dbus_interface='org.freedesktop.DBus.Properties',
                signal_name='PropertiesChanged',
                arg0='org.bluez.Device1',
                path_keyword='path',
            )
        except Exception as e:
            print(f'[bluez] failed to subscribe to device events: {e}')

    @staticmethod
    def find_adapter(bus:SystemBus):
        """Returns (adapter_path, mac_address) for the first BlueZ adapter found."""

        object = bus.get_object(BLUEZ_SERVICE_NAME, '/')
        manager = Interface(object, DBUS_OBJECT_MAPPER_INTERFACE)

        objects = manager.GetManagedObjects()

        for path, interfaces in objects.items():
            if BLUEZ_ADAPTER_INTERFACE in interfaces:
                mac_address = interfaces[BLUEZ_ADAPTER_INTERFACE]['Address']
                return str(path), str(mac_address)

        return None, None

    @staticmethod
    def set_adapter_alias(bus:SystemBus, adapter_path:str, alias:str):
        """Set the adapter's Alias, which is what scanners/iOS display."""

        try:
            adapter = bus.get_object(BLUEZ_SERVICE_NAME, adapter_path)
            props = Interface(adapter, DBUS_PROPERTIES_INTERFACE)
            props.Set(BLUEZ_ADAPTER_INTERFACE, 'Alias', alias)
        except Exception as e:
            print(f'failed to set adapter alias to "{alias}": {e}')

    @classmethod
    def load_client_mac_address_cache(cls):

        try:
            with open(CLIENT_MAC_ADDRESS_CACHE) as file:

                line = file.readline()

                if line is not None:
                    cls.client_mac_address = line.strip()

        except Exception:
            return

    @classmethod
    def save_client_mac_address_cache(cls):

        with open(CLIENT_MAC_ADDRESS_CACHE, 'w') as file:
            file.write(cls.client_mac_address)

    @classmethod
    def _ensure_adapter_up(cls):
        """Physically bring hci0 up. Must run BEFORE we ask bluez to power
        the adapter on — if the kernel interface is DOWN (ENETDOWN 100),
        bluetoothctl's `power on` returns success but does nothing
        because it merely flips bluez's internal state; the radio stays
        unpowered and any subsequent HCI/SDP/pairing traffic silently
        fails with `Input/output error`.

        Symptoms in the wild we guard against here:
          - USB autosuspend on a dongle after many hours idle
          - bluetoothd restart racing the adapter coming back up
          - soft rfkill block from `bluetoothctl power off` on a prior run
        """
        from worker import _wdiag as _bdiag

        # 1. rfkill unblock — cheap, and often the reason hciconfig up
        #    fails with "Operation not permitted" / "Rfkill enabled".
        try:
            _bdiag('prepare_adapter: rfkill unblock bluetooth')
            subprocess.run(['rfkill', 'unblock', 'bluetooth'],
                           timeout=3, capture_output=True, text=True)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        # 2. hciconfig hci0 up — lowest-level way to set IFF_UP on the HCI
        #    interface. Works even when bluez is confused.
        try:
            _bdiag('prepare_adapter: hciconfig hci0 up')
            subprocess.run(['hciconfig', 'hci0', 'up'],
                           timeout=5, capture_output=True, text=True)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    @classmethod
    def _verify_adapter_powered(cls, bus:SystemBus, adapter_path:str,
                                timeout_s:float = 10.0):
        """Wait until BlueZ reports Powered:True on the adapter, forcibly
        setting it if the wait times out.

        Called once after the D-Bus layer is up (in initialize()). A dead
        adapter here is the difference between "working keyboard" and
        "iPhone shows Connection Unsuccessful with no explanation" — it's
        worth being aggressive.
        """
        from worker import _wdiag as _bdiag
        import time
        if adapter_path is None: return

        deadline = time.monotonic() + timeout_s
        last_val = None
        while time.monotonic() < deadline:
            try:
                adapter = bus.get_object(BLUEZ_SERVICE_NAME, adapter_path)
                props = Interface(adapter, DBUS_PROPERTIES_INTERFACE)
                powered = bool(props.Get(BLUEZ_ADAPTER_INTERFACE, 'Powered'))
                if powered:
                    return
                last_val = powered
            except dbus.exceptions.DBusException as e:
                last_val = f'<dbus error: {e}>'

            # Try to kick it up ourselves — bluez sometimes accepts the
            # set where its own auto-power step has silently given up.
            try:
                props.Set(BLUEZ_ADAPTER_INTERFACE, 'Powered', dbus.Boolean(True))
                _bdiag('prepare_adapter: forced Powered=True via D-Bus')
            except dbus.exceptions.DBusException:
                pass

            time.sleep(0.5)

        print(f'[adapter] WARNING: adapter still reports Powered={last_val!r} '
              f'after {timeout_s:.0f}s. The HCI interface is probably DOWN '
              '(ENETDOWN). Try: `sudo rfkill unblock bluetooth && '
              'sudo hciconfig hci0 up && sudo systemctl restart bluetooth` '
              'then rerun ./start.sh.')

    @classmethod
    def prepare_adapter(cls):
        """Make sure the adapter is up and accepting new pairings without
        touching any existing bonds."""
        from worker import _wdiag as _bdiag

        # Bring the kernel HCI interface up FIRST. If it's in IFF_DOWN
        # state then bluetoothctl power on below will fake-succeed
        # (bluez just records its desired state) but the radio will
        # actually stay dead. This causes every downstream symptom:
        # iOS "Connection Unsuccessful", hciconfig class failing with
        # "Network is down (100)", and Device1.Connect() raising
        # org.bluez.Error.Failed: Input/output error forever.
        cls._ensure_adapter_up()

        # All bluetoothctl invocations get a hard timeout: they talk to
        # bluetoothd over D-Bus and if that's wedged we'd hang forever
        # otherwise, blocking the worker before it can even emit a state
        # update. 15s is generous for a single bluetoothctl one-shot.
        for args in (['power', 'on'], ['pairable', 'on'], ['discoverable', 'on']):
            _bdiag(f'prepare_adapter: bluetoothctl {" ".join(args)}')
            try:
                subprocess.run(['bluetoothctl'] + args, timeout=15,
                               capture_output=True, text=True)
            except subprocess.TimeoutExpired:
                _bdiag(f'prepare_adapter: bluetoothctl {" ".join(args)} TIMED OUT')
                print(f'[adapter] bluetoothctl {" ".join(args)} TIMED OUT — continuing anyway')
            _bdiag(f'prepare_adapter: bluetoothctl {" ".join(args)} returned')

        # Bump discoverable-timeout to 0 so the adapter stays discoverable
        # until we explicitly turn it off. Ignored silently on older bluez.
        _bdiag('prepare_adapter: bluetoothctl discoverable-timeout 0')
        try:
            subprocess.run(['bluetoothctl', 'discoverable-timeout', '0'],
                           timeout=15, capture_output=True, text=True)
        except subprocess.TimeoutExpired:
            _bdiag('prepare_adapter: bluetoothctl discoverable-timeout TIMED OUT')
            print('[adapter] bluetoothctl discoverable-timeout TIMED OUT')
        _bdiag('prepare_adapter: bluetoothctl discoverable-timeout returned')

        _bdiag('prepare_adapter: set_class_of_device')
        cls.set_class_of_device(CLASS_OF_DEVICE_PERIPHERAL_KEYBOARD)
        _bdiag('prepare_adapter: set_class_of_device returned')

    @classmethod
    def _verify_class_of_device(cls, bus:SystemBus, adapter_path:str, tries:int = 2, interval_s:float = 0.25):
        """After profile registration, BlueZ may briefly overwrite CoD
        major/minor with whatever /etc/bluetooth/main.conf says. If that
        value isn't Peripheral/Keyboard we detect it here, print a loud
        warning, and reapply via btmgmt a few times to ride out any
        async overwrites. The permanent cure is `Class = 0x000540` in
        main.conf (start.sh sets this up)."""

        if adapter_path is None: return
        import time
        for i in range(tries):
            try:
                adapter = bus.get_object(BLUEZ_SERVICE_NAME, adapter_path)
                props = Interface(adapter, DBUS_PROPERTIES_INTERFACE)
                cod = int(props.Get(BLUEZ_ADAPTER_INTERFACE, 'Class'))
                major = (cod >> 8) & 0x1F
                minor = (cod >> 2) & 0x3F
                # Service class bits (13-23). If ANY of these are set
                # (Audio 0x200, Telephony 0x400, Object Transfer 0x100,
                # Networking 0x800, Capturing/Rendering/Positioning,
                # Information), iOS treats us as a hybrid device and
                # refuses to promote us to HID-only — even though the
                # major/minor already say Keyboard.
                service_bits = (cod >> 13) & 0x7FF
                cod_ok = (major == 0x05 and (minor & 0x10)
                          and service_bits == 0)
                if cod_ok:
                    return
                svc_names = _decode_service_class_bits(service_bits)
                print(f'[adapter] CoD currently 0x{cod:06x} '
                      f'(major=0x{major:02x} minor=0x{minor:02x}, '
                      f'service_bits=0x{service_bits:03x} [{svc_names}]); '
                      f'reapplying keyboard CoD (try {i+1}/{tries})')
            except Exception as e:
                print(f'[adapter] could not read class during verify: {e}')
                return
            cls.set_class_of_device(CLASS_OF_DEVICE_PERIPHERAL_KEYBOARD)
            time.sleep(interval_s)

        # Final read for a definitive post-loop diagnostic.
        try:
            cod_final = int(props.Get(BLUEZ_ADAPTER_INTERFACE, 'Class'))
            svc_bits_final = (cod_final >> 13) & 0x7FF
            if svc_bits_final != 0:
                print(f'[adapter] WARNING: adapter CoD 0x{cod_final:06x} still '
                      f'carries service-class bits [{_decode_service_class_bits(svc_bits_final)}]. '
                      'iOS will see us as a hybrid device and refuse HID. '
                      'This is caused by bluetoothd plugins or bluez-obexd '
                      'registering Audio/Telephony/OBEX UUIDs. Run '
                      '`./start.sh --reset-bluez` to refresh the -P list '
                      'and stop obex services.')
                return
        except Exception:
            pass
        print('[adapter] WARNING: could not pin CoD to Peripheral/Keyboard after retries. '
              'Make sure `Class = 0x000540` is in /etc/bluetooth/main.conf '
              '(run `./start.sh --reset-bluez` once).')

    @staticmethod
    def _log_adapter_class(bus:SystemBus, adapter_path:str):
        """Dump relevant adapter properties after profile registration so we
        can see what iPhone is actually being offered (CoD, advertised UUIDs,
        pairable/discoverable state)."""

        if adapter_path is None: return
        try:
            adapter = bus.get_object(BLUEZ_SERVICE_NAME, adapter_path)
            props = Interface(adapter, DBUS_PROPERTIES_INTERFACE)

            cod = int(props.Get(BLUEZ_ADAPTER_INTERFACE, 'Class'))
            major = (cod >> 8) & 0x1F
            minor = (cod >> 2) & 0x3F
            role = 'peripheral/keyboard' if (major == 0x05 and (minor & 0x10)) else 'other'
            print(f'[adapter] Class reported by BlueZ: 0x{cod:06x} (major=0x{major:02x} minor=0x{minor:02x} -> {role})')

            for name in ('Powered', 'Pairable', 'Discoverable', 'PairableTimeout', 'DiscoverableTimeout'):
                try:
                    print(f'[adapter] {name}: {_stringify(props.Get(BLUEZ_ADAPTER_INTERFACE, name))}')
                except Exception:
                    pass

            try:
                uuids = [str(u) for u in props.Get(BLUEZ_ADAPTER_INTERFACE, 'UUIDs')]
                has_hid = any(u.lower().startswith('00001124') for u in uuids)
                print(f'[adapter] UUIDs advertised ({len(uuids)}): HID present = {has_hid}')
                # Each advertised UUID contributes bits to the adapter's
                # Class-of-Device service-class field (bits 13-23). Dumping
                # them makes it obvious which plugin is responsible when iOS
                # refuses HID pairing because we look like "keyboard + audio
                # + telephony + object transfer".
                for u in sorted(uuids):
                    tag = _uuid_tag(u)
                    print(f'[adapter]   uuid {u}{(" (" + tag + ")") if tag else ""}')
                if not has_hid:
                    print('[adapter] WARNING: HID UUID (0x1124) not in adapter advertisement — '
                          'iPhone will not treat us as a keyboard.')
            except Exception as e:
                print(f'[adapter] could not read UUIDs: {e}')

        except Exception as e:
            print(f'[adapter] could not read properties: {e}')

    @staticmethod
    def _log_bluetoothd_cmdline():
        """Print bluetoothd's command line and verify the input/hostname
        plugins are disabled. Gotcha: bluetoothd's -P/--noplugin only keeps
        the LAST value if passed multiple times — it is NOT additive. So
        `-P input -P hostname` silently loads the input plugin. The correct
        form is a single `-P input,hostname`."""

        try:
            result = subprocess.run(['pidof', 'bluetoothd'], capture_output=True, text=True)
            pid = (result.stdout.strip().split() or [None])[0]
            if not pid:
                print('[bluez] bluetoothd not running?!')
                return
            with open(f'/proc/{pid}/cmdline', 'rb') as f:
                argv = f.read().split(b'\x00')
            argv = [a.decode(errors='replace') for a in argv if a]
            print(f'[bluez] bluetoothd cmdline: {" ".join(argv)}')

            # Extract the effective --noplugin set. -P/--noplugin is
            # last-one-wins; each value can be a comma-separated list.
            noplugin_last = None
            i = 0
            while i < len(argv):
                a = argv[i]
                if a in ('-P', '--noplugin') and i + 1 < len(argv):
                    noplugin_last = argv[i + 1]; i += 2; continue
                if a.startswith('--noplugin='):
                    noplugin_last = a.split('=', 1)[1]; i += 1; continue
                if a.startswith('-P') and len(a) > 2:
                    noplugin_last = a[2:]; i += 1; continue
                i += 1
            disabled = set((noplugin_last or '').split(','))
            disabled.discard('')

            if 'input' not in disabled:
                print('[bluez] WARNING: input plugin is NOT disabled '
                      f'(effective -P list: {sorted(disabled) or "<empty>"}). '
                      'It will claim the HID UUID and make RegisterProfile '
                      'fail with "UUID already registered", and grab PSMs '
                      '0x11/0x13. Run `./start.sh --reset-bluez` to fix.')
            if 'hostname' not in disabled:
                print('[bluez] WARNING: hostname plugin is NOT disabled '
                      f'(effective -P list: {sorted(disabled) or "<empty>"}). '
                      "It will override the adapter's Class of Device to "
                      'Computer/Desktop and iOS will refuse HID pairing. '
                      'Run `./start.sh --reset-bluez` to fix.')
        except Exception as e:
            print(f'[bluez] could not read bluetoothd cmdline: {e}')

    @classmethod
    def set_class_of_device(cls, cod_hex:str):
        """Advertise this adapter as a BT Peripheral Keyboard rather than a
        Computer. Without this, iOS (and Android) frequently refuse to pair
        as HID: they probe SDP, see the wrong CoD, and disconnect without
        ever starting SSP.

        We prefer btmgmt (mgmt API) because bluez recomputes/writes CoD via
        that same API on every UUID/profile change; values written with
        hciconfig get silently clobbered. btmgmt class takes major (5 bits)
        and minor (6 bits, pre-shift) values. For 0x002540:
            major-device-class = 0x05 (Peripheral)
            minor-device-class = 0x10 (Keyboard) -> bits 2-7 of CoD
        """

        from worker import _wdiag as _bdiag
        cod = int(cod_hex, 16)
        major = (cod >> 8) & 0x1F                 # 5-bit major (bits 8..12)
        # btmgmt's minor arg is passed straight through to the kernel's
        # mgmt_cp_set_dev_class.minor field, which occupies bits 0..7 of
        # CoD (NOT bits 2..7). So we feed it the full low byte: 0x40 for a
        # Keyboard, not 0x10. Getting this wrong is what made CoD read as
        # 0x?05?10 / minor=0x04 (Sensing device) instead of Keyboard.
        # Also: btmgmt parses decimal, not hex.
        minor_byte = cod & 0xFC                   # low byte, format bits (0-1) zeroed
        # Keep the btmgmt timeout short. We've observed it wedge for 10 s+
        # right after bluetoothd handles a RegisterProfile — if we wait that
        # long, the iPhone finishes its SDP probe with a stale CoD and gives
        # up on pairing. hciconfig fallback is effectively instantaneous,
        # and /etc/bluetooth/main.conf `Class = 0x000540` is the real
        # persistent authority anyway. 3 s is plenty if btmgmt is healthy.
        try:
            _bdiag(f'set_class_of_device: running btmgmt class {major} {minor_byte}')
            result = subprocess.run(
                ['btmgmt', '--index', '0', 'class', str(major), str(minor_byte)],
                capture_output=True, text=True, timeout=3,
            )
            _bdiag(f'set_class_of_device: btmgmt rc={result.returncode}')
            if result.returncode == 0:
                print(f'[adapter] class of device set to {cod_hex} via btmgmt '
                      f'(major={major} minor={minor_byte})')
                return
            else:
                print(f'[adapter] btmgmt class failed: {result.stderr.strip() or result.stdout.strip()}')
        except FileNotFoundError:
            _bdiag('set_class_of_device: btmgmt not found; falling back to hciconfig')
        except subprocess.TimeoutExpired:
            _bdiag('set_class_of_device: btmgmt TIMED OUT after 3s — falling back')
            print('[adapter] btmgmt class TIMED OUT after 3s — falling back to hciconfig '
                  '(main.conf Class=0x000540 is the persistent authority)')

        # Fallback: hciconfig, only if btmgmt isn't installed. Note this
        # writes to the controller directly and bluez may overwrite it.
        try:
            _bdiag('set_class_of_device: running hciconfig fallback')
            result = subprocess.run(
                ['hciconfig', 'hci0', 'class', cod_hex],
                capture_output=True, text=True, timeout=10,
            )
            _bdiag(f'set_class_of_device: hciconfig rc={result.returncode}')
            if result.returncode == 0:
                print(f'[adapter] class of device set to {cod_hex} via hciconfig '
                      '(may be overwritten by bluez)')
            else:
                print(f'[adapter] hciconfig class failed: {result.stderr.strip() or result.stdout.strip()}')
        except FileNotFoundError:
            print('[adapter] neither btmgmt nor hciconfig available; CoD not set')
        except subprocess.TimeoutExpired:
            _bdiag('set_class_of_device: hciconfig TIMED OUT after 10s')
            print('[adapter] hciconfig class TIMED OUT after 10s')

    @staticmethod
    def create_channel_socket(mac_address, channel):

        sock = socket(AF_BLUETOOTH, SOCK_SEQPACKET, BTPROTO_L2CAP)
        sock.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)

        # Require the kernel to start SSP + encryption on incoming L2CAP
        # Force MITM-protected SSP on every incoming L2CAP connection. Apple
        # refuses Just-Works pairing on HID (a keylogger could MITM it), so
        # if we ask for anything less than HIGH the iPhone connects, sees
        # the link security level is insufficient for HID, and bails with
        # HCI 0x0E "Connection Rejected due to Security Reasons" before
        # IO-Capability-Request is even exchanged. With HIGH + our agent's
        # DisplayYesNo capability, SSP resolves to Numeric Comparison, which
        # satisfies Apple's MITM requirement.
        try:
            sec = struct.pack('BB', BT_SECURITY_HIGH, 0)
            sock.setsockopt(SOL_BLUETOOTH, BT_SECURITY, sec)
        except OSError as e:
            print(f'[l2cap] WARNING: could not set BT_SECURITY on PSM 0x{channel:04x}: {e}')

        sock.bind((mac_address, channel))
        sock.listen(1)

        return sock

    @classmethod
    def send_to_control_channel(cls, data:bytes):
        cls._safe_send('client_control_socket', data)

    @classmethod
    def send_to_interrupt_channel(cls, data:bytes):
        cls._safe_send('client_interrupt_socket', data)

    @classmethod
    def _safe_send(cls, attr:str, data:bytes):

        if data is None: return

        with cls._lock:
            sock = getattr(cls, attr)
            if sock is None or not cls.connected:
                return

        try:
            sock.send(data)
        except (OSError, BrokenPipeError) as e:
            print(f'connection lost during send: {e}')
            cls._drop_client()
