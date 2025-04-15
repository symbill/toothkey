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
BLUEZ_PROFILE_PATH = '/org/bluez/bullshit_hid_profile'
BLUEZ_AGENT_PATH = '/org/bluez/AuthorizeServiceAgent'

CAPABILITY = 'NoInputNoOutput'

# sdp service record file name
SERVICE_RECORD_FILE = 'hid_sdp_record.xml'

class BullshitAgent(dbus.service.Object):

    def __init__(self, bus):

        super().__init__(bus, BLUEZ_AGENT_PATH)

        object = bus.get_object(BLUEZ_SERVICE_NAME, BLUEZ_OBJECT_PATH)

        manager = dbus.Interface(object, AGENT_MANAGER_INTERFACE)
        manager.RegisterAgent(BLUEZ_AGENT_PATH, CAPABILITY)
        manager.RequestDefaultAgent(BLUEZ_AGENT_PATH)

    @dbus.service.method(AGENT_INTERFACE, in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        print("AuthorizeService %s %s" % (device, uuid))
        return

class BullshitoothProfile(dbus.service.Object):

    file_descriptor:int|None = None

    def __init__(self, bus):

        super().__init__(bus, BLUEZ_PROFILE_PATH)

        manager = dbus.Interface(
            bus.get_object(BLUEZ_SERVICE_NAME, BLUEZ_OBJECT_PATH),
            PROFILE_MANAGER_INTERFACE
        )

        options = {
            'ServiceRecord': open(SERVICE_RECORD_FILE).read(),
            'Role': 'server',
            'RequireAuthentication': False,
            'RequireAuthorization': False,
        }

        manager.RegisterProfile(BLUEZ_PROFILE_PATH, HID_UUID, options)

    @dbus.service.method(PROFILE_INTERFACE, in_signature='', out_signature='')
    def Release(self):

        print('Profile released')

    @dbus.service.method(PROFILE_INTERFACE, in_signature='', out_signature='')
    def Cancel(self):

        print('Profile canceled')

    @dbus.service.method(PROFILE_INTERFACE, in_signature='oha{sv}', out_signature='')
    def NewConnection(self, path, fd, properties):

        self.file_descriptor = fd.take()

        print(f'New Connection from {path}, fd={self.file_descriptor}')

        for key, value in properties.items():
            print(f'    {key}: {value}')

    @dbus.service.method(PROFILE_INTERFACE, in_signature='o', out_signature='')
    def RequestDisconnection(self, path):

        print(f'Disconnection requested from {path}')

        if self.file_descriptor is not None:

            os.close(self.file_descriptor)
            self.file_descriptor = None
