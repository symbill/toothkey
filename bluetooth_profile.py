import os
import dbus
import dbus.service

# this is the UUID that means "I'am HID Keyboard"
HID_UUID = '00001124-0000-1000-8000-00805f9b34fb'

# bluez values
PROFILE_MANAGER_INTERFACE = 'org.bluez.ProfileManager1'
PROFILE_INTERFACE = 'org.bluez.Profile1'

AGENT_MANAGER_INTERFACE = 'org.bluez.AgentManager1'
AGENT_INTERFACE = 'org.bluez.Agent1'

BLUEZ_SERVICE_NAME = 'org.bluez'
BLUEZ_OBJECT_PATH = '/org/bluez'
BLUEZ_PROFILE_PATH = '/org/bluez/toothkey_hid_profile'
BLUEZ_AGENT_PATH = '/org/bluez/AuthorizeServiceAgent'

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
        print(f'[agent] AuthorizeService device={device} uuid={uuid} -> authorized')

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
        options = {
            'ServiceRecord': sdp_record,
            'Role': 'server',
            'RequireAuthentication': True,
            'RequireAuthorization': False,
        }

        try:
            manager.RegisterProfile(BLUEZ_PROFILE_PATH, HID_UUID, options)
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
