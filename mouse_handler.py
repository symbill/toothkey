from pynput import mouse
from evdev import InputDevice, ecodes, list_devices
from common import GlobalContext

X = 0
Y = 1

class BullshitMouseHandler:

    mouse_button_bitmasks = {
        ecodes.BTN_LEFT:    1 << 0,
        ecodes.BTN_RIGHT:   1 << 1,
        ecodes.BTN_MIDDLE:  1 << 2,
    }

    states = bytearray([
        0xA1,   # Report Type
        0x02,   # Report ID
        0x00,   # buttons (bit mask)
        0x00,   # move dx
        0x00,   # move dy
        0x00,   # wheel dx (horizontal)
    ])

    states_buttons_index = 2
    states_move_dx_index = 3
    states_move_dy_index = 4
    states_wheel_dy_index = 5

    input_button_set = set()

    device = None
    listener = None

    @classmethod
    def clamp_signed_byte(cls, val):
        return max(-127, min(127, val)) & 0xFF

    @classmethod
    def find_mouse_device(cls):

        for path in list_devices():

            device = InputDevice(path)

            try:
                capabilities = device.capabilities()

                has_rels = ecodes.EV_REL in capabilities
                has_keys = ecodes.EV_KEY in capabilities

                if has_rels and has_keys:

                    rels = [code for code in capabilities[ecodes.EV_REL]]
                    has_movement = ecodes.REL_X in rels and ecodes.REL_Y in rels

                    keys = [code for code in capabilities[ecodes.EV_KEY]]
                    has_buttons = all(key in keys for key in cls.mouse_button_bitmasks)

                    if has_movement and has_buttons: return device

            except: pass

        return None

    @classmethod
    async def start(cls):

        cls.device = cls.find_mouse_device()

        if cls.device is None:
            print("[WARNING] No pieces of mouse shit found")
            return

        # prevent spreading mouse event shits to os
        cls.listener = mouse.Listener(suppress=True)
        cls.listener.start()

        dx = dy = sy = 0

        async for event in cls.device.async_read_loop():
            if event.type == ecodes.EV_REL:
                if event.code == ecodes.REL_X:
                    dx += event.value
                elif event.code == ecodes.REL_Y:
                    dy += event.value
                elif event.code == ecodes.REL_WHEEL:
                    sy += event.value
                # elif event.code == ecodes.REL_HWHEEL:
                #     sx += event.value

            elif event.type == ecodes.EV_KEY:
                if event.code in cls.mouse_button_bitmasks:
                    if event.value: # pressed
                        cls.input_button_set.add(event.code)
                    else:           # released
                        cls.input_button_set.discard(event.code)

            # EV_SYN means a full event packet is complete
            elif event.type == ecodes.EV_SYN:
                cls.update_states(dx, dy, sy)
                if GlobalContext.grab_mode:
                    GlobalContext.send_data_to_device(bytes(cls.states))
                dx = dy = sy = 0

    @classmethod
    def update_states(cls, dx, dy, sy):

        # buttons
        cls.states[cls.states_buttons_index] = 0x00
        for code in cls.input_button_set:
            cls.states[cls.states_buttons_index] |= cls.mouse_button_bitmasks.get(code, 0)

        # movement
        cls.states[cls.states_move_dx_index] = cls.clamp_signed_byte(dx)
        cls.states[cls.states_move_dy_index] = cls.clamp_signed_byte(dy)

        # scroll
        cls.states[cls.states_wheel_dy_index] = cls.clamp_signed_byte(sy)
