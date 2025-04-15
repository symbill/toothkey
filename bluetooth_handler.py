import subprocess, re

from socket import socket, AF_BLUETOOTH, SOCK_SEQPACKET, BTPROTO_L2CAP, SOL_SOCKET, SO_REUSEADDR
from dbus import SystemBus, Interface
from dbus.mainloop.glib import DBusGMainLoop
from bluetooth_profile import BullshitoothProfile, BullshitAgent

DBUS_OBJECT_MAPPER_INTERFACE = 'org.freedesktop.DBus.ObjectManager'

BLUEZ_ADAPTER_INTERFACE = 'org.bluez.Adapter1'
BLUEZ_SERVICE_NAME = 'org.bluez'

CONTROL_CHANNEL = 0x0011
INTERRUPT_CHANNEL = 0x0013

CLIENT_MAC_ADDRESS_CACHE = 'client_mac_address.cache'

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

    # ---------------- Mouse ----------------
    0x05, 0x01,     # Usage Page (Generic Desktop)
    0x09, 0x02,     # Usage (Mouse)
    0xA1, 0x01,     # Collection (Application)
    0x85, 0x02,     # Report ID (2)
    0x09, 0x01,     # Usage (Pointer)
    0xA1, 0x00,     # Collection (Physical)
    0x05, 0x09,     # Usage Page (Buttons)
    0x19, 0x01,     # Usage Minimum (1)
    0x29, 0x03,     # Usage Maximum (3)
    0x15, 0x00,     # Logical Minimum (0)
    0x25, 0x01,     # Logical Maximum (1)
    0x95, 0x03,     # Report Count (3)
    0x75, 0x01,     # Report Size (1)
    0x81, 0x02,     # Buttons
    0x95, 0x01,     # Report Count (1)
    0x75, 0x05,     # Report Size (5)
    0x81, 0x03,     # Padding
    0x05, 0x01,     # Usage Page (Moves)
    0x09, 0x30,     # Usage X
    0x09, 0x31,     # Usage Y
    0x09, 0x38,     # Usage Wheel (Vertical)
    0x15, 0x81,     # Logical Minimum (-127)
    0x25, 0x7F,     # Logical Maximum (127)
    0x75, 0x08,     # Report Size (8)
    0x95, 0x03,     # Report Count (3)
    0x81, 0x06,     # Input: Data, Variable, Relative
    0xC0,           # End Physical Collection
    0xC0            # End Application Collection
])

class BullshitoothHandler:

    client_mac_address:str = None
    client_control_socket:socket = None
    client_interrupt_socket:socket = None

    @classmethod
    def start(cls):

        cls.load_client_mac_address_cache()
        cls.clean_up_bluetooth_status()

        DBusGMainLoop(set_as_default=True)

        bus = SystemBus()

        # register auto-authorize agent
        BullshitAgent(bus)

        # register HID profile
        BullshitoothProfile(bus)

        # check controller mac address
        mac_address = cls.get_server_mac_address(bus)

        # init control/interrupt sockets
        control_channel_socket = cls.create_channel_socket(mac_address, CONTROL_CHANNEL)
        interrupt_channel_socket = cls.create_channel_socket(mac_address, INTERRUPT_CHANNEL)

        # accept connections
        print(f'[MAC:{mac_address}] waiting for connections...')

        cls.client_control_socket, ccs_addr = control_channel_socket.accept()
        cls.client_interrupt_socket, cis_addr = interrupt_channel_socket.accept()

        print(f'[MAC:{mac_address}] control channel connected to {ccs_addr}')
        print(f'[MAC:{mac_address}] interrupt channel connected to {cis_addr}')

        # hand shake using control socket
        cls.send_to_control_channel(bytes(HID_REPORT_DESCRIPTOR))

        # save client_mac_address
        cls.client_mac_address = ccs_addr[0]
        cls.save_client_mac_address_cache()

    @classmethod
    def stop(cls):

        if cls.client_control_socket is not None:
            cls.client_control_socket.close()

        if cls.client_interrupt_socket is not None:
            cls.client_interrupt_socket.close()

        if cls.client_mac_address is not None:
            print(f'prepare exit...hold up...')
            subprocess.call(['bluetoothctl', 'remove', cls.client_mac_address])

    @staticmethod
    def get_server_mac_address(bus:SystemBus):

        object = bus.get_object(BLUEZ_SERVICE_NAME, '/')
        manager = Interface(object, DBUS_OBJECT_MAPPER_INTERFACE)

        objects = manager.GetManagedObjects()

        for path, interfaces in objects.items():
            if BLUEZ_ADAPTER_INTERFACE in interfaces:
                mac_address = interfaces[BLUEZ_ADAPTER_INTERFACE]['Address']
                return str(mac_address)

        return None

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
    def clean_up_bluetooth_status(cls):

        if cls.client_mac_address is not None:
            subprocess.call(['bluetoothctl', 'remove', cls.client_mac_address])

        # if cache is not found, then we wipe out all shits.
        else:
            result = subprocess.run(["bluetoothctl", "devices"], capture_output=True, text=True)
            macs = re.findall(r'Device\s+([0-9A-F:]{17})', result.stdout)
            for mac in macs:
                subprocess.run(['bluetoothctl', 'remove', mac])

        subprocess.call(['bluetoothctl', 'power', 'on'])
        subprocess.call(['bluetoothctl', 'pairable', 'on'])
        subprocess.call(['bluetoothctl', 'discoverable', 'on'])

    @staticmethod
    def create_channel_socket(mac_address, channel):

        bullshit_socket = socket(AF_BLUETOOTH, SOCK_SEQPACKET, BTPROTO_L2CAP)
        bullshit_socket.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
        bullshit_socket.bind((mac_address, channel))
        bullshit_socket.listen(1)

        return bullshit_socket

    @classmethod
    def send_to_control_channel(cls, bytes:bytes):

        if bytes is None: return

        if cls.client_control_socket is not None:
            cls.client_control_socket.send(bytes)

    @classmethod
    def send_to_interrupt_channel(cls, bytes:bytes):

        if bytes is None: return

        if cls.client_interrupt_socket is not None:
            cls.client_interrupt_socket.send(bytes)
