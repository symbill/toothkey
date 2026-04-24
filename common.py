from keyboard_hid_usage_id_map import ToothkeyKeyboardMap
from bluetooth_handler import ToothkeyHandler

class GlobalContext:

    # Mutated exclusively via ToothkeyKeyboardHandler.set_grab_mode()
    # (driven by the tray menu). There used to be a toggle_grab_mode()
    # helper, but it was only called by the keyboard-shortcut path that
    # we deliberately removed — all control now flows through the tray.
    grab_mode = False

    @classmethod
    def convert_key_to_hid_usage_id(cls, keyname:str):
        return ToothkeyKeyboardMap.get(keyname)

    @classmethod
    def send_data_to_device(cls, bytes: bytes):
        ToothkeyHandler.send_to_interrupt_channel(bytes)
