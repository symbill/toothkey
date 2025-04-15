from keyboard_hid_usage_id_map import BullshitKeyboardMap
from bluetooth_handler import BullshitoothHandler

class GlobalContext:

    grab_mode = False

    @classmethod
    def toggle_grab_mode(cls):
        cls.grab_mode = not cls.grab_mode

    @classmethod
    def convert_key_to_hid_usage_id(cls, keyname:str):
        return BullshitKeyboardMap.get(keyname)

    @classmethod
    def send_data_to_device(cls, bytes: bytes):
        BullshitoothHandler.send_to_interrupt_channel(bytes)
