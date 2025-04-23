# Bullshitooth Emulator

A program that emulates a Bluetooth keyboard on an Ubuntu (24.04 LTS) machine to control an iPhone.

This project was inspired by [this great project](https://github.com/Alkaid-Benetnash/EmuBTHID).

It was created by a frustrated and sneaky office worker who owns an expensive mechanical keyboard without Bluetooth functionality, works on an Ubuntu company computer, and wanted to secretly play game on his iPhone while pretending to work.

# Usage

1. Run the Bullshitooth program:
    ```bash
    . start.sh [--reset-all | --reset-bluez ]
    ```
    Run the above command in the terminal without any arguments, and it will execute automatically.

    You probably won't need to use any arguments, but if something doesn't work, try running it with one of the options.

    ### Arguments
    - reset-all: Reinstalls all necessary dependencies.
    - reset-bluez: Resets Bluetooth settings.

2. Control your iPhone:

    Go to Settings > Bluetooth > Pair with the computer.

    (The Bullshitooth program will automatically authenticate and complete the pairing process.)

3. Use `alt + shift` to toggle between grab/ungrab mode and enjoy using your iPhone while pretending to work!  

    (Press `ctrl + shift + c` to exit this bullshit in ungrab mode)

# Notes

- Unfortunately, if you ever stop or restart the program, you must completely remove the Bluetooth pairing between your iPhone and computer and redo the process of this Bullshitooth Emulator
- If a certain input field in a program—such as the address bar in Chrome or Code windows in vscode—is focused by the mouse cursor, it may capture the mouse scroll events, preventing them from being delivered to your iPhone. If all other features are working normally but mouse scrolling doesn't, try changing the focus on your computer to a different, more neutral input field in another program, then try again.
- \+ 2025.04.21
  - In grab mode, add a suppress feature to prevent keyboard and mouse events from propagating to the computer.
  - Fixed a bug where special characters above the number keys were not being transmitted.
  - Also, fix the issue where the window closes immediately after running `start.sh`.
- \+ 2025.04.23
  - Enhanced internal keyboard input handling
  - Changed shut down key set from {`ctrl` + `c`} to {`ctrl` + `shift` + `c`}
