import json

KEYBOARD_HID_USAGE_ID_MAP_JSON = 'keyboard_hid_usage_id_map.json'

class ToothkeyKeyboardMap:

    map:dict = None

    @classmethod
    def load(cls):

        with open(KEYBOARD_HID_USAGE_ID_MAP_JSON) as file:
            cls.map = json.load(file)

    @classmethod
    def get(cls, key:str):

        if cls.map is None: cls.load()

        hid_usage_id = None

        if key in cls.map:
            hid_usage_id = cls.map.get(key)

        return hid_usage_id