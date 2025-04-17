import os, sys, time

from pynput import keyboard
from common import GlobalContext

class BullshitKeyboardHandler:

    modifier_key_bitmasks = {
        keyboard.Key.ctrl_l:    1 << 0,
        keyboard.Key.shift_l:   1 << 1,
        keyboard.Key.alt_l:     1 << 2,
        keyboard.Key.cmd_l:     1 << 3,
        keyboard.Key.ctrl_r:    1 << 4,
        keyboard.Key.shift_r:   1 << 5,
        keyboard.Key.alt_r:     1 << 6,
        keyboard.Key.cmd_r:     1 << 7,
    }

    toggle_grab_mode_key_set = { keyboard.Key.alt, keyboard.Key.shift }
    toggle_grab_mode_key_names = None

    shut_down_key_set = { keyboard.Key.ctrl, keyboard.KeyCode.from_char('c') }
    shut_down_key_names = None

    states = bytearray([
        0xA1,   # Report Type
        0x01,   # Report ID
        0x00,   # Modifier keys (bit mask shits)
        0x00,   # Reserved
        0x00,   # key 1
        0x00,   # key 2
        0x00,   # key 3
        0x00,   # key 4
        0x00,   # key 5
        0x00    # key 6
    ])

    states_size = len(states)
    states_modifier_keys_index = 2
    states_input_key_start_index = 4
    states_input_key_limit = states_size - states_input_key_start_index

    input_key_set = set()
    input_modifier_key_set = set()

    listener = None

    event_handlers = None

    active = True

    @classmethod
    def get_event_handlers(cls):

        if cls.event_handlers is not None: return cls.event_handlers

        cls.event_handlers = {
            name: method.__func__.__get__(cls,cls)
            for name, method in cls.__dict__.items()
            if type(method) == classmethod and name.startswith('on_')
        }

        return cls.event_handlers

    @classmethod
    def start(cls):

        if cls.listener is not None and cls.listener.is_alive(): return

        cls.clear_screen()

        event_handlers = cls.get_event_handlers()

        cls.listener = keyboard.Listener(**event_handlers, suppress=GlobalContext.grab_mode)
        cls.listener.start()
        cls.listener.join()

    @classmethod
    def on_press(cls, key: (keyboard.Key | keyboard.KeyCode | None)):

        cls.clear_screen()

        pressed_common_key_count = len(cls.input_key_set) - len(cls.input_modifier_key_set)

        if pressed_common_key_count >= cls.states_input_key_limit:
            return

        cls.input_key_set.add(key)

        if key in cls.modifier_key_bitmasks:
            cls.input_modifier_key_set.add(key)

        if cls.contains_toggle_grab_mode_keys():
            GlobalContext.toggle_grab_mode()
            cls.input_key_set.clear()
            cls.listener.stop()

        if cls.contains_shut_down_keys():
            if not GlobalContext.grab_mode:
                cls.active = False
                sys.exit(0)

        cls.update_states()

        if GlobalContext.grab_mode:
            GlobalContext.send_data_to_device(bytes(cls.states))

    @classmethod
    def on_release(cls, key: (keyboard.Key | keyboard.KeyCode | None)):

        cls.clear_screen()

        cls.input_key_set.discard(key)
        cls.input_modifier_key_set.discard(key)

        cls.update_states()

        if GlobalContext.grab_mode:
            GlobalContext.send_data_to_device(bytes(cls.states))

    @classmethod
    def contains_toggle_grab_mode_keys(cls):
        return all(key in cls.input_key_set for key in cls.toggle_grab_mode_key_set)

    @classmethod
    def contains_shut_down_keys(cls):
        return all(key in cls.input_key_set for key in cls.shut_down_key_set)

    @classmethod
    def parse_key_name(cls, key: (keyboard.Key | keyboard.KeyCode | None)):

        keyname = None

        if key is None: return keyname
        if isinstance(key, keyboard.KeyCode): keyname = key.char
        if isinstance(key, keyboard.Key): keyname = key.name

        return keyname

    @classmethod
    def update_states(cls):

        cls.states[cls.states_modifier_keys_index] = 0x00

        for key in cls.input_modifier_key_set:
            cls.states[cls.states_modifier_keys_index] |= cls.modifier_key_bitmasks[key]

        index = cls.states_input_key_start_index

        for key in cls.input_key_set:

            if key in cls.input_modifier_key_set: continue

            keyname = cls.parse_key_name(key)
            hid_usage_id = GlobalContext.convert_key_to_hid_usage_id(keyname.lower() if keyname else keyname)
            if hid_usage_id is None: continue

            cls.states[index] = hid_usage_id
            index += 1

        cls.states[index:] = [0] * (cls.states_size - index)

    @classmethod
    def clear_screen(cls):

        os.system('cls' if os.name == 'nt' else 'clear')

        status = 'Grab mode' if GlobalContext.grab_mode else 'Ungrab mode'

        if cls.toggle_grab_mode_key_names is None:
            cls.toggle_grab_mode_key_names = [
                cls.parse_key_name(key) for key in cls.toggle_grab_mode_key_set
            ]

        if cls.shut_down_key_names is None:
            cls.shut_down_key_names = [
                cls.parse_key_name(key) for key in cls.shut_down_key_set
            ]

        print(f'Current Status: {status}')
        print(f'Press {cls.toggle_grab_mode_key_names} to turn on/off grab mode.')
        print(f'Or Press {cls.shut_down_key_names} to shut down this piece of shits. (Only available in Ungrab mode)')
