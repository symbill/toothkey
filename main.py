from proc_title import set_title
set_title('toothkey-cli')

import logging_setup
logging_setup.install()

from bluetooth_handler import ToothkeyHandler
from keyboard_handler import ToothkeyKeyboardHandler

def run_connection_session():
    """Block until the BT client disconnects.

    --cli mode can't toggle grab (that's tray-only), so GlobalContext
    .grab_mode stays off here and no keyboard is ever captured. We just
    wait on the disconnect event — exactly what the worker does too.
    """
    ToothkeyHandler.wait_until_disconnected()
    ToothkeyKeyboardHandler.stop_listener()

def main():

    try:
        ToothkeyHandler.initialize()

        while ToothkeyKeyboardHandler.active:
            if not ToothkeyHandler.wait_for_client():
                if not ToothkeyHandler._running:
                    break
                continue
            run_connection_session()
            print('session ended; waiting for reconnect...')

    except KeyboardInterrupt: print()
    except Exception as e: print(f'fatal: {e}')

    finally:
        ToothkeyHandler.stop()

if __name__ == '__main__':
    main()
