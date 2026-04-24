#!/usr/bin/env python3
"""Minimal HCI packet monitor for diagnosing pairing failures.

Opens the HCI_CHANNEL_MONITOR raw socket (same data source btmon uses) and
prints a one-line summary for every packet, decoding ONLY the specific HCI
events we care about during pairing:

  - Connection Complete / Connection Request
  - Disconnection Complete              <- reason code tells us WHO hung up
  - IO Capability Request / Response    <- who asked for what auth
  - User Confirmation / Passkey Request
  - Simple Pairing Complete             <- final SSP status
  - Authentication Complete             <- classic BT link-key auth
  - Link Key Request / Notification
  - LE Connection Complete / LE LTK Req <- so we can see if iPhone is doing LE

Everything else is logged as a one-line hex summary (opcode + length + first
bytes). No full decoding, so we avoid the glibc FORTIFY buffer-overflow bug
that kills btmon on Ubuntu 24.04 + bluez 5.72.

Usage (need CAP_NET_RAW):
    sudo ./hci_monitor.py [output.log]
default output file: hci_monitor.log (plus mirror to stdout).
"""

import os, socket, struct, sys, time
from datetime import datetime

# From <bluetooth/bluetooth.h> / <bluetooth/hci.h>:
AF_BLUETOOTH = 31
SOCK_RAW     = 3
BTPROTO_HCI  = 1
HCI_CHANNEL_MONITOR = 2
HCI_DEV_NONE = 0xFFFF

# bluez monitor pseudo-header opcodes.
MON_OPCODES = {
    0: 'NEW_INDEX', 1: 'DEL_INDEX', 2: 'CMD', 3: 'EVENT',
    4: 'ACL_TX', 5: 'ACL_RX', 6: 'SCO_TX', 7: 'SCO_RX',
    8: 'OPEN_INDEX', 9: 'CLOSE_INDEX', 10: 'INDEX_INFO',
    11: 'VENDOR_DIAG', 12: 'SYSTEM_NOTE', 13: 'USER_LOGGING',
    14: 'CTRL_OPEN', 15: 'ISO_TX', 16: 'ISO_RX',
    17: 'CTRL_CLOSE', 18: 'CTRL_COMMAND', 19: 'CTRL_EVENT',
}

# HCI event codes we care about.
HCI_EVT_CONN_COMPLETE       = 0x03
HCI_EVT_CONN_REQUEST        = 0x04
HCI_EVT_DISCONN_COMPLETE    = 0x05
HCI_EVT_AUTH_COMPLETE       = 0x06
HCI_EVT_ENCRYPT_CHANGE      = 0x08
HCI_EVT_LINK_KEY_REQ        = 0x17
HCI_EVT_LINK_KEY_NOTIFY     = 0x18
HCI_EVT_IO_CAPABILITY_REQ   = 0x31
HCI_EVT_IO_CAPABILITY_RSP   = 0x32
HCI_EVT_USER_CONFIRM_REQ    = 0x33
HCI_EVT_USER_PASSKEY_REQ    = 0x34
HCI_EVT_SIMPLE_PAIRING_COMPLETE = 0x36
HCI_EVT_LE_META             = 0x3E

LE_SUBEVT_CONN_COMPLETE     = 0x01
LE_SUBEVT_LTK_REQUEST       = 0x05
LE_SUBEVT_ENH_CONN_COMPLETE = 0x0a

# Common HCI error codes (BT Core spec v5.x vol 1 Part F).
HCI_STATUS = {
    0x00: 'SUCCESS', 0x05: 'AUTH_FAILURE', 0x06: 'PIN_OR_KEY_MISSING',
    0x07: 'MEMORY_CAPACITY_EXCEEDED', 0x08: 'CONN_TIMEOUT',
    0x0e: 'CONN_REJECTED_SECURITY_REASONS',
    0x13: 'REMOTE_USER_TERMINATED', 0x15: 'REMOTE_POWEROFF',
    0x16: 'CONN_TERMINATED_BY_LOCAL_HOST',
    0x22: 'LMP_LL_RESPONSE_TIMEOUT', 0x23: 'LMP_ERROR_TRANSACTION_COLLISION',
    0x25: 'ENCRYPTION_MODE_NOT_ACCEPTABLE', 0x28: 'INSTANT_PASSED',
    0x29: 'PAIRING_WITH_UNIT_KEY_UNSUPPORTED', 0x2f: 'INSUFFICIENT_SECURITY',
    0x3b: 'UNACCEPTABLE_CONN_PARAMETERS', 0x3d: 'MIC_FAILURE',
}

IO_CAP = {
    0x00: 'DisplayOnly', 0x01: 'DisplayYesNo',
    0x02: 'KeyboardOnly', 0x03: 'NoInputNoOutput',
    0x04: 'KeyboardDisplay',
}
AUTH_REQ = {
    0x00: 'NoMITM_NoBonding', 0x01: 'MITM_NoBonding',
    0x02: 'NoMITM_DedicatedBonding', 0x03: 'MITM_DedicatedBonding',
    0x04: 'NoMITM_GeneralBonding', 0x05: 'MITM_GeneralBonding',
}


def fmt_addr(b: bytes) -> str:
    return ':'.join(f'{x:02X}' for x in reversed(b))


def fmt_status(code: int) -> str:
    name = HCI_STATUS.get(code, '???')
    return f'0x{code:02x} ({name})'


def decode_hci_event(body: bytes) -> str:
    if len(body) < 2:
        return f'EVT malformed len={len(body)}'
    code, plen = body[0], body[1]
    params = body[2:2 + plen]

    if code == HCI_EVT_CONN_COMPLETE and len(params) >= 11:
        status = params[0]
        handle = params[1] | (params[2] << 8)
        addr = fmt_addr(params[3:9])
        link_type = params[9]
        enc = params[10]
        return (f'EVT Connection Complete status={fmt_status(status)} '
                f'handle=0x{handle:04x} bdaddr={addr} link_type={link_type} enc={enc}')
    if code == HCI_EVT_CONN_REQUEST and len(params) >= 10:
        addr = fmt_addr(params[0:6])
        cod = int.from_bytes(params[6:9], 'little')
        link_type = params[9]
        return f'EVT Connection Request bdaddr={addr} cod=0x{cod:06x} link_type={link_type}'
    if code == HCI_EVT_DISCONN_COMPLETE and len(params) >= 4:
        status, h_lo, h_hi, reason = params[0], params[1], params[2], params[3]
        handle = h_lo | (h_hi << 8)
        return (f'EVT Disconnection Complete status={fmt_status(status)} '
                f'handle=0x{handle:04x} reason={fmt_status(reason)}')
    if code == HCI_EVT_AUTH_COMPLETE and len(params) >= 3:
        status = params[0]
        handle = params[1] | (params[2] << 8)
        return f'EVT Authentication Complete status={fmt_status(status)} handle=0x{handle:04x}'
    if code == HCI_EVT_ENCRYPT_CHANGE and len(params) >= 4:
        status, h_lo, h_hi, enc = params[0], params[1], params[2], params[3]
        return (f'EVT Encryption Change status={fmt_status(status)} '
                f'handle=0x{(h_lo|h_hi<<8):04x} enabled={enc}')
    if code == HCI_EVT_LINK_KEY_REQ and len(params) >= 6:
        return f'EVT Link Key Request bdaddr={fmt_addr(params[0:6])}'
    if code == HCI_EVT_LINK_KEY_NOTIFY and len(params) >= 23:
        return f'EVT Link Key Notification bdaddr={fmt_addr(params[0:6])} key_type=0x{params[22]:02x}'
    if code == HCI_EVT_IO_CAPABILITY_REQ and len(params) >= 6:
        return f'EVT IO Capability Request bdaddr={fmt_addr(params[0:6])}'
    if code == HCI_EVT_IO_CAPABILITY_RSP and len(params) >= 9:
        addr = fmt_addr(params[0:6])
        io = IO_CAP.get(params[6], f'0x{params[6]:02x}')
        oob = params[7]
        auth = AUTH_REQ.get(params[8], f'0x{params[8]:02x}')
        return (f'EVT IO Capability Response bdaddr={addr} '
                f'io_cap={io} oob={oob} auth_req={auth}')
    if code == HCI_EVT_USER_CONFIRM_REQ and len(params) >= 10:
        addr = fmt_addr(params[0:6])
        passkey = int.from_bytes(params[6:10], 'little')
        return f'EVT User Confirmation Request bdaddr={addr} passkey={passkey:06d}'
    if code == HCI_EVT_USER_PASSKEY_REQ and len(params) >= 6:
        return f'EVT User Passkey Request bdaddr={fmt_addr(params[0:6])}'
    if code == HCI_EVT_SIMPLE_PAIRING_COMPLETE and len(params) >= 7:
        status = params[0]
        addr = fmt_addr(params[1:7])
        return f'EVT Simple Pairing Complete status={fmt_status(status)} bdaddr={addr}'
    if code == HCI_EVT_LE_META and len(params) >= 1:
        sub = params[0]
        if sub == LE_SUBEVT_CONN_COMPLETE and len(params) >= 19:
            status = params[1]
            handle = params[2] | (params[3] << 8)
            role = params[4]
            addr = fmt_addr(params[6:12])
            return (f'EVT LE Connection Complete status={fmt_status(status)} '
                    f'handle=0x{handle:04x} role={role} peer={addr}')
        if sub == LE_SUBEVT_ENH_CONN_COMPLETE and len(params) >= 30:
            status = params[1]
            handle = params[2] | (params[3] << 8)
            role = params[4]
            addr = fmt_addr(params[6:12])
            return (f'EVT LE Enhanced Connection Complete status={fmt_status(status)} '
                    f'handle=0x{handle:04x} role={role} peer={addr}')
        if sub == LE_SUBEVT_LTK_REQUEST:
            return 'EVT LE LTK Request'
        return f'EVT LE Meta sub=0x{sub:02x} len={plen}'
    return f'EVT code=0x{code:02x} len={plen} raw={params[:16].hex()}'


def decode_packet(monop: int, body: bytes) -> str:
    name = MON_OPCODES.get(monop, f'op{monop}')
    if monop == 3:  # EVENT
        return decode_hci_event(body)
    if monop == 2:  # CMD
        if len(body) >= 3:
            ocf = body[0] | ((body[1] & 0x03) << 8)
            ogf = (body[1] >> 2) & 0x3F
            return f'CMD ogf=0x{ogf:02x} ocf=0x{ocf:04x} plen={body[2]} raw={body[3:3+body[2]].hex()[:32]}'
    return f'{name} len={len(body)} raw={body[:24].hex()}'


def main(out_path: str):
    # Rename the process so it shows up in `ps aux | grep toothkey`
    # and `pgrep toothkey`. Best-effort \u2014 never fatal.
    try:
        from proc_title import set_title
        set_title('toothkey-hcimon')
    except Exception:
        pass

    # Open the output file FIRST (before anything that can fail). That way,
    # even socket / bind failures land in the log file, so debug.sh's
    # post-mortem can tell us what went wrong.
    out = open(out_path, 'w', buffering=1)

    def log(msg: str):
        ts = datetime.now().astimezone().isoformat(timespec='milliseconds')
        line = f'{ts} {msg}'
        print(line, flush=True)
        out.write(line + '\n')

    def die(msg: str):
        log(f'[hci_monitor] FATAL: {msg}')
        out.close()
        sys.exit(1)

    log(f'[hci_monitor] starting (pid={os.getpid()}, euid={os.geteuid()})')

    try:
        s = socket.socket(AF_BLUETOOTH, SOCK_RAW, BTPROTO_HCI)
    except PermissionError as e:
        die(f'socket() PermissionError: {e} — need CAP_NET_RAW (run with sudo)')
    except OSError as e:
        die(f'socket() OSError: {e} — kernel BT module not loaded?')

    try:
        # sockaddr_hci on Linux is {family:2, dev:2, channel:2} = 6 bytes.
        addr = struct.pack('HHH', AF_BLUETOOTH, HCI_DEV_NONE, HCI_CHANNEL_MONITOR)
        s.bind(addr)
    except OSError as e:
        die(f'bind(HCI_CHANNEL_MONITOR) OSError: {e}')

    log(f'[hci_monitor] bound to HCI monitor channel; writing {out_path}')

    # SIGTERM should behave like Ctrl-C: flush and exit cleanly. debug.sh
    # kills us via `sudo kill -INT`, but depending on how sudo is configured
    # that may arrive as SIGTERM instead of SIGINT, so handle both.
    import signal
    def _term(signum, _frame):
        raise KeyboardInterrupt(f'signal {signum}')
    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGHUP, _term)

    try:
        while True:
            data = s.recv(4096)
            if len(data) < 6:
                continue
            monop, hindex, plen = struct.unpack_from('<HHH', data, 0)
            body = data[6:6 + plen]
            try:
                summary = decode_packet(monop, body)
            except Exception as e:
                summary = f'decode_error={e} raw={body[:32].hex()}'
            log(f'hci{hindex} {summary}')
    except KeyboardInterrupt as e:
        log(f'[hci_monitor] stopped ({e})')
    except Exception as e:
        log(f'[hci_monitor] unexpected error: {type(e).__name__}: {e}')
        raise
    finally:
        out.close()


if __name__ == '__main__':
    path = sys.argv[1] if len(sys.argv) > 1 else 'hci_monitor.log'
    main(path)
