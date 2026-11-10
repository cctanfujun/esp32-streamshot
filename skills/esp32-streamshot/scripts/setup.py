#!/usr/bin/env python3
"""Inspect prerequisites or print MCP configuration; never open a device."""
import argparse
import glob
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import sys


def configuration(port=None, state=None):
    entry = Path(__file__).resolve().parent / 'streamshot' / 'streamshot_bridge.py'
    if not entry.is_file():
        raise ValueError('bundled_runtime_missing')
    if port is not None:
        if not re.fullmatch(r'/dev/cu\.(?:usbmodem|usbserial)[A-Za-z0-9_.-]*', port):
            raise ValueError('select_a_macos_usb_serial_port')
        if not stat.S_ISCHR(os.stat(port).st_mode):
            raise ValueError('serial_port_not_character_device')
    state = state or str(Path.home() / 'Library/Application Support/esp32-streamshot')
    if not Path(state).is_absolute():
        raise ValueError('state_directory_must_be_absolute')
    server = {'command': sys.executable, 'args': [str(entry), '--state', state, 'mcp']}
    if port:
        server['env'] = {'STREAMSHOT_SERIAL_PORT': port}
    return {'mcpServers': {'streamshot': server}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('doctor')
    config = sub.add_parser('mcp-config')
    config.add_argument('--port')
    config.add_argument('--state')
    args = parser.parse_args()
    try:
        if args.action == 'doctor':
            result = {'platform': platform.system(), 'supported_host': sys.platform == 'darwin',
                      'python_supported': sys.version_info >= (3, 10),
                      'commands': {name: shutil.which(name) is not None for name in ('openssl', 'dns-sd', 'scutil')},
                      'candidate_ports': sorted(glob.glob('/dev/cu.usbmodem*') + glob.glob('/dev/cu.usbserial*')),
                      'device_opened': False, 'configuration_changed': False}
        else:
            result = configuration(args.port, args.state)
        print(json.dumps(result, indent=2))
    except (OSError, ValueError) as error:
        print(json.dumps({'ok': False, 'error': str(error) if isinstance(error, ValueError) else 'selected_port_unavailable'}))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
