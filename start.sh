#!/bin/bash

function install_dependencies() {
    echo "Installing dependencies..."
    sudo apt update
    sudo apt install -y xterm bluez bluez-firmware bluez-obexd bluez-tools python3 python3-evdev python3-pynput python3-dbus python3-bluez python3-pip
}

function init_bluez() {
    echo "Initializing BlueZ..."
    sudo sed -i.bak 's|^ExecStart=\(/usr/libexec/bluetooth/bluetoothd.*\)|ExecStart=\1 -P input|' /usr/lib/systemd/system/bluetooth.service
    sudo systemctl daemon-reload
    sudo systemctl restart bluetooth
}

if [ ! -f ".initiated" ] || [ "$1" == "--reset-all" ]; then

    echo "initiating..."

    install_dependencies
    init_bluez

    touch .initiated

elif [ "$1" == "--reset-bluez" ]; then
    init_bluez
fi

sudo xterm -e "python3 main.py"
