import asyncio

from bluetooth_handler import BullshitoothHandler
from keyboard_handler import BullshitKeyboardHandler
from mouse_handler import BullshitMouseHandler

async def start_device_handlers():

    asyncio.create_task(BullshitMouseHandler.start())

    loop = asyncio.get_running_loop()

    while BullshitKeyboardHandler.active:
        await loop.run_in_executor(None, BullshitKeyboardHandler.start)
        if BullshitKeyboardHandler.active:
            asyncio.create_task(BullshitMouseHandler.toggle_suppres_mouse_event())

def main():

    try:
        BullshitoothHandler.start()
        asyncio.run(start_device_handlers())

    except KeyboardInterrupt: print()
    except Exception: print()

    finally:
        BullshitoothHandler.stop()

if __name__ == '__main__':
    main()