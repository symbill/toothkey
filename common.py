from keyboard_hid_usage_id_map import ToothkeyKeyboardMap
from bluetooth_handler import ToothkeyHandler

class GlobalContext:

    # Mutated exclusively via ToothkeyKeyboardHandler.set_grab_mode()
    # (driven by the tray menu).
    #
    # This means "grab is armed", not "grab is held right now". The two
    # differ while the Bluetooth link is down: there is nowhere to
    # forward keystrokes, so the grab is released, but grab_mode stays
    # True so it resumes by itself when the peer comes back. The tray
    # renders that state as "not connected (grab ON)".
    #
    # ToothkeyKeyboardHandler.is_running() is the question "is the
    # keyboard captured at this instant". set_grab_mode leaves this
    # False whenever the grab could not be taken at all.
    grab_mode = False

    @classmethod
    def convert_key_to_hid_usage_id(cls, keyname:str):
        return ToothkeyKeyboardMap.get(keyname)

    @classmethod
    def send_data_to_device(cls, bytes: bytes):
        ToothkeyHandler.send_to_interrupt_channel(bytes)

    @classmethod
    def is_peer_connected(cls) -> bool:
        """Is there a Bluetooth peer to forward keystrokes to?

        The keyboard layer asks before taking a grab, so we never hold
        the user's keyboard exclusively while having nowhere to send
        what we capture.
        """
        return bool(ToothkeyHandler.is_connected())
