import os
import dbus
import dbus.service
import dbus.exceptions

# this is the UUID that means "I'am HID Keyboard"
HID_UUID = '00001124-0000-1000-8000-00805f9b34fb'

# 16-bit short form (0x1124) of HID, in case bluez ever passes us the short
# form instead of the fully-qualified base UUID.
HID_UUID_SHORT = '00001124'

# UUIDs we are willing to authorize over an authenticated link with iOS.
# Anything else (Handsfree 0x111e, HandsfreeAudioGateway 0x111f, A2DP, AVRCP,
# PBAP, MAP, OBEX, ...) MUST be rejected — otherwise iOS will happily promote
# the link to a hands-free audio device in addition to a keyboard, which is
# both unexpected and a privacy/UX problem (the iPhone routes calls to us).
ALLOWED_AUTHORIZE_UUIDS = {
    HID_UUID.lower(),
    # SDP itself (0x0001) — bluez sometimes runs AuthorizeService for the SDP
    # discovery channel; refusing that would break HID negotiation.
    '00000001-0000-1000-8000-00805f9b34fb',
}


class Rejected(dbus.exceptions.DBusException):
    """Raised from agent methods to tell bluez (and the peer) to refuse this
    pairing/authorization step. Maps to org.bluez.Error.Rejected on the wire,
    which is the canonical bluez convention used by their own simple-agent
    sample."""

    _dbus_error_name = 'org.bluez.Error.Rejected'


# bluez values
PROFILE_MANAGER_INTERFACE = 'org.bluez.ProfileManager1'
PROFILE_INTERFACE = 'org.bluez.Profile1'

AGENT_MANAGER_INTERFACE = 'org.bluez.AgentManager1'
AGENT_INTERFACE = 'org.bluez.Agent1'

BLUEZ_SERVICE_NAME = 'org.bluez'
BLUEZ_OBJECT_PATH = '/org/bluez'
BLUEZ_PROFILE_PATH = '/org/bluez/toothkey_hid_profile'
BLUEZ_AGENT_PATH = '/org/bluez/AuthorizeServiceAgent'

BLUEZ_ADAPTER_INTERFACE = 'org.bluez.Adapter1'
DBUS_OBJECT_MANAGER_INTERFACE = 'org.freedesktop.DBus.ObjectManager'


class NoAdapterError(RuntimeError):
    """Raised when bluez has no Adapter1 object — i.e. the kernel has no hciN
    device available. We surface this distinctly so the worker can log a
    actionable hint (rfkill / modprobe -r btusb) rather than crashing in
    python-dbus with a confusing TypeError."""
    pass


def _adapter_present(bus) -> bool:
    """Cheap check via ObjectManager to see if at least one org.bluez.Adapter1
    is currently exported. Returns False if bluez is up but has no adapter
    (e.g. controller wedged after kernel firmware crash, rfkill blocked,
    USB dongle unplugged), or if the call raises for any reason."""
    try:
        obj = bus.get_object(BLUEZ_SERVICE_NAME, '/')
        mgr = dbus.Interface(obj, DBUS_OBJECT_MANAGER_INTERFACE)
        for _, interfaces in mgr.GetManagedObjects().items():
            if BLUEZ_ADAPTER_INTERFACE in interfaces:
                return True
    except Exception as e:
        print(f'[profile] could not enumerate bluez adapters: {e}')
    return False


# DisplayYesNo lets us negotiate "Numeric Comparison" SSP with iOS
# (iOS is DisplayYesNo too). That pairing model is what iOS reliably
# accepts for HID keyboards; plain "Just Works" (NoInputNoOutput) tends
# to be refused for HID. We auto-accept the numeric-comparison prompt on
# our side via RequestConfirmation, so the user only taps "Pair" on iOS.
CAPABILITY = 'DisplayYesNo'

# sdp service record file name. Resolved relative to THIS source file so
# it works no matter what cwd the worker was launched with — important
# for systemd, which launches us from "/" by default.
SERVICE_RECORD_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'hid_sdp_record.xml')

# Fixed passkey returned for RequestPinCode / RequestPasskey paths in case
# iOS ever falls back to legacy pairing. 0000 is the Bluetooth convention.
LEGACY_PIN = '0000'
LEGACY_PASSKEY = dbus.UInt32(0)


class ToothkeyAgent(dbus.service.Object):

    def __init__(self, bus):

        super().__init__(bus, BLUEZ_AGENT_PATH)

        object = bus.get_object(BLUEZ_SERVICE_NAME, BLUEZ_OBJECT_PATH)

        manager = dbus.Interface(object, AGENT_MANAGER_INTERFACE)

        try:
            manager.RegisterAgent(BLUEZ_AGENT_PATH, CAPABILITY)
            print(f'[agent] registered with capability {CAPABILITY}')
        except Exception as e:
            print(f'[agent] RegisterAgent failed: {e}')
            raise

        try:
            manager.RequestDefaultAgent(BLUEZ_AGENT_PATH)
            print('[agent] set as default')
        except Exception as e:
            print(f'[agent] RequestDefaultAgent failed: {e}')

    @dbus.service.method(AGENT_INTERFACE, in_signature='', out_signature='')
    def Release(self):
        print('[agent] Release')

    @dbus.service.method(AGENT_INTERFACE, in_signature='os', out_signature='')
    def AuthorizeService(self, device, uuid):
        # iOS, after pairing an HID keyboard over an authenticated link, will
        # opportunistically try to AuthorizeService for *every* profile the
        # adapter happens to advertise — Handsfree (0x111e), HandsfreeAudioGw
        # (0x111f), A2DP, AVRCP, PBAP, ... If we blindly authorize them the
        # iPhone will treat us as a multi-role device (keyboard + hands-free
        # audio); incoming calls and Siri audio can then route to this Linux
        # box, which is definitely not what the user wants.
        #
        # We are exclusively a HID keyboard, so we authorize *only* the HID
        # UUID (and the harmless SDP UUID) and reject everything else. The
        # cleanest fix would be to stop bluez from advertising those audio
        # UUIDs in the first place (start.sh --reset-bluez tries to do that
        # via -P), but rejecting here is a belt-and-suspenders guarantee
        # that survives any bluetoothd plugin that sneaks back in.
        u = str(uuid).lower()
        if u in ALLOWED_AUTHORIZE_UUIDS or u.startswith(HID_UUID_SHORT):
            print(f'[agent] AuthorizeService device={device} uuid={uuid} -> authorized (HID)')
            return
        print(f'[agent] AuthorizeService device={device} uuid={uuid} -> REJECTED '
              f'(non-HID; toothkey is keyboard-only)')
        raise Rejected(
            f'toothkey only authorizes HID; refusing service uuid={uuid} '
            f'for device={device}')

    @dbus.service.method(AGENT_INTERFACE, in_signature='o', out_signature='s')
    def RequestPinCode(self, device):
        print(f'[agent] RequestPinCode device={device} -> {LEGACY_PIN}')
        return LEGACY_PIN

    @dbus.service.method(AGENT_INTERFACE, in_signature='o', out_signature='u')
    def RequestPasskey(self, device):
        print(f'[agent] RequestPasskey device={device} -> {int(LEGACY_PASSKEY)}')
        return LEGACY_PASSKEY

    @dbus.service.method(AGENT_INTERFACE, in_signature='ouq', out_signature='')
    def DisplayPasskey(self, device, passkey, entered):
        print(f'[agent] DisplayPasskey device={device} passkey={passkey:06d} entered={entered}')

    @dbus.service.method(AGENT_INTERFACE, in_signature='os', out_signature='')
    def DisplayPinCode(self, device, pincode):
        print(f'[agent] DisplayPinCode device={device} pincode={pincode}')

    @dbus.service.method(AGENT_INTERFACE, in_signature='ou', out_signature='')
    def RequestConfirmation(self, device, passkey):
        # Numeric-comparison SSP: iOS shows this 6-digit code and asks the
        # user to tap Pair. We auto-confirm on our side.
        print(f'[agent] RequestConfirmation device={device} passkey={passkey:06d} -> auto-confirm')

    @dbus.service.method(AGENT_INTERFACE, in_signature='o', out_signature='')
    def RequestAuthorization(self, device):
        print(f'[agent] RequestAuthorization device={device} -> authorized')

    @dbus.service.method(AGENT_INTERFACE, in_signature='', out_signature='')
    def Cancel(self):
        print('[agent] Cancel')


DEFAULT_SERVICE_NAME = 'Toothkey Keyboard'


class ToothkeyProfile(dbus.service.Object):

    file_descriptor:int|None = None

    def __init__(self, bus, service_name:str = DEFAULT_SERVICE_NAME):

        # Pre-flight: if there's no adapter under /org/bluez, RegisterProfile
        # will fail in a confusing way (introspection of ProfileManager1
        # returns no signature for the call, and python-dbus then can't
        # auto-derive `a{sv}` from our options dict and raises a generic
        # "TypeError: Expected a string or unicode object"). Detect that
        # condition up front and surface a clean, actionable error.
        if not _adapter_present(bus):
            print('[profile] FATAL: no bluez Adapter1 object present '
                  '(hci0 missing). The Bluetooth controller is likely '
                  'wedged or rfkill\'d. Try:  '
                  '`sudo rfkill unblock bluetooth && '
                  'sudo systemctl restart bluetooth`  or, if that fails, '
                  '`sudo modprobe -r btusb && sudo modprobe btusb`.')
            raise NoAdapterError(
                'bluez has no Adapter1 object; cannot register HID profile')

        super().__init__(bus, BLUEZ_PROFILE_PATH)

        manager = dbus.Interface(
            bus.get_object(BLUEZ_SERVICE_NAME, BLUEZ_OBJECT_PATH),
            PROFILE_MANAGER_INTERFACE
        )

        with open(SERVICE_RECORD_FILE) as f:
            sdp_record = f.read()

        if service_name != DEFAULT_SERVICE_NAME:
            sdp_record = sdp_record.replace(DEFAULT_SERVICE_NAME, service_name)

        # HID channels MUST be authenticated + encrypted on iOS. Setting
        # RequireAuthentication=True tells bluez to advertise the profile
        # accordingly and to co-operate with the kernel when it triggers
        # SSP on our incoming L2CAP sockets (we additionally set
        # BT_SECURITY on those sockets in bluetooth_handler.py).
        #
        # Wrap each value in an explicit dbus type and the dict itself in
        # dbus.Dictionary(signature='sv'). Without explicit types, python-dbus
        # tries to introspect ProfileManager1.RegisterProfile to discover the
        # `a{sv}` signature; if that introspection ever fails (e.g. bluez in
        # a half-up state), we hit
        #   TypeError: Expected a string or unicode object
        # at message-append time, which masks the real problem. Explicit
        # types make the call work regardless of introspection state.
        options = dbus.Dictionary({
            'ServiceRecord': dbus.String(sdp_record),
            'Role': dbus.String('server'),
            'RequireAuthentication': dbus.Boolean(True),
            'RequireAuthorization': dbus.Boolean(False),
        }, signature='sv')

        # Same belt-and-suspenders treatment for the positional args.
        # RegisterProfile's real signature is `osa{sv}` — the first arg is
        # an *object path* (`o`), not a string. Normally python-dbus
        # introspects ProfileManager1 and learns to marshal a Python str
        # as `o` here, but on a freshly-booted system bluez can be in a
        # half-up state where introspection returns nothing. python-dbus
        # then falls back to inferring types from the Python objects, a
        # `str` becomes `s`, and the call goes out as `ssa{sv}` — which
        # bluez rejects with
        #   org.freedesktop.DBus.Error.UnknownMethod: Method "RegisterProfile"
        #   with signature "ssa{sv}" on interface "org.bluez.ProfileManager1"
        #   doesn't exist
        # Wrapping the path in dbus.ObjectPath (and the UUID in dbus.String
        # for symmetry) makes the call survive whatever introspection state
        # bluez happens to be in at boot.
        try:
            manager.RegisterProfile(
                dbus.ObjectPath(BLUEZ_PROFILE_PATH),
                dbus.String(HID_UUID),
                options,
            )
            print(f'[profile] registered HID profile as "{service_name}"')
        except Exception as e:
            print(f'[profile] RegisterProfile failed: {e}')
            raise

    @dbus.service.method(PROFILE_INTERFACE, in_signature='', out_signature='')
    def Release(self):
        print('[profile] Release')

    @dbus.service.method(PROFILE_INTERFACE, in_signature='', out_signature='')
    def Cancel(self):
        print('[profile] Cancel')

    @dbus.service.method(PROFILE_INTERFACE, in_signature='oha{sv}', out_signature='')
    def NewConnection(self, path, fd, properties):

        self.file_descriptor = fd.take()

        print(f'[profile] NewConnection from {path}, fd={self.file_descriptor}')

        for key, value in properties.items():
            print(f'    {key}: {value}')

    @dbus.service.method(PROFILE_INTERFACE, in_signature='o', out_signature='')
    def RequestDisconnection(self, path):

        print(f'[profile] RequestDisconnection from {path}')

        if self.file_descriptor is not None:

            os.close(self.file_descriptor)
            self.file_descriptor = None
