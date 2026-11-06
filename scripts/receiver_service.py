#!/usr/bin/env python3
# Managed by Magi receiver runtime v2
"""Unprivileged local ADS-B supervisor with live, unambiguous USB selection.

Installed beside the decoder so application release swaps do not stop recovery.
No package installs, permission changes, driver detachment or network discovery.
"""
import argparse
import json
from pathlib import Path
import re
import signal
import subprocess
import threading
import time

PRODUCTS = frozenset({'2832', '2838'})
COMPETITORS = ('dump1090-fa.service', 'dump1090-mutability.service',
               'rtl_tcp.service', 'dump978-fa.service')


class SelectionError(ValueError):
    def __init__(self, state, message):
        super().__init__(message)
        self.state = state


def discover(root=Path('/sys/bus/usb/devices')):
    """Include receivers with missing serials: they still make auto ambiguous."""
    receivers = []
    usb_count = 0
    for device in sorted(Path(root).iterdir()):
        try:
            vendor = (device/'idVendor').read_text().strip()
            product = (device/'idProduct').read_text().strip()
            usb_count += 1
        except OSError:
            continue
        if vendor != '0bda' or product not in PRODUCTS:
            continue
        try:
            serial = (device/'serial').read_text().strip()
            # devnum changes when a device re-enumerates on the same USB port.
            device_number = (device/'devnum').read_text().strip()
        except OSError:
            serial, device_number = '', ''
        receivers.append({'path': device.name, 'product': product,
                          'serial': serial, 'device_number': device_number})
    return receivers, usb_count


def select(receivers, usb_count, selection='auto', serial=None):
    if selection not in ('auto', 'pinned'):
        raise SelectionError('invalid_selection', 'Receiver selection must be auto or pinned.')
    candidates = receivers
    if selection == 'pinned':
        if not serial:
            raise SelectionError('invalid_selection', 'Pinned receiver selection needs a serial.')
        candidates = [device for device in receivers if device['serial'] == serial]
    if not candidates:
        raise SelectionError('device_absent', 'Waiting for the selected receiver.' if selection == 'pinned'
                             else 'Waiting for one supported RTL-SDR receiver.')
    if len(candidates) != 1:
        raise SelectionError('ambiguous_devices', 'Multiple receivers match; connect one or explicitly pin a unique serial.')
    chosen = candidates[0]
    identity = chosen['serial']
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', identity) or not chosen['device_number']:
        raise SelectionError('identity_unavailable', 'Receiver identity is missing or unusable; not selecting by USB index.')
    # readsb treats canonical small integers as device indexes before trying an
    # exact serial. The USB inventory is an upper bound on its device count.
    # Zero-prefixed serials (other than "0") are exact serials in readsb.
    if identity == '0' or (identity.isdecimal() and not identity.startswith('0')
                           and int(identity) < usb_count):
        raise SelectionError('ambiguous_serial', 'Receiver serial could be interpreted as a device index; use a unique non-index serial.')
    return chosen


def check_ownership(run=subprocess.run):
    try:
        result = run(['systemctl', 'is-active', *COMPETITORS], capture_output=True,
                     text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SelectionError('ownership_unavailable', 'Cannot verify receiver ownership.') from error
    states = result.stdout.split()
    if result.returncode == 0 or any(state in ('active', 'activating', 'reloading', 'deactivating') for state in states):
        raise SelectionError('device_busy', 'Another decoder manages receiver hardware; waiting without taking it over.')
    if (result.returncode not in (3, 4) or len(states) != len(COMPETITORS)
            or any(state not in ('inactive', 'failed', 'unknown') for state in states)):
        raise SelectionError('ownership_unavailable', 'Cannot verify receiver ownership.')


def publish(directory, state, selection, device=None, detail=None):
    data = {'schema': 1, 'state': state, 'selection': selection,
            'updated_unix': time.time()}
    if device:
        data.update(serial=device['serial'], product=device['product'])
    if detail:
        data['detail'] = str(detail)
    temporary = directory/'receiver-status.tmp'
    temporary.write_text(json.dumps(data)+'\n')
    temporary.replace(directory/'receiver-status.json')


def clear_feed(directory):
    # Never leave a recent feed from the previous device during replacement.
    (directory/'aircraft.json').unlink(missing_ok=True)


def stop_decoder(child):
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


def supervise(binary, options, directory, selection, serial, stop, *,
              discover_fn=discover, ownership_fn=check_ownership,
              launch=subprocess.Popen, publish_fn=publish):
    child = None
    last_state = None

    def status(state, device=None, detail=None):
        nonlocal last_state
        publish_fn(directory, state, selection, device, detail)
        if state != last_state:
            print(f'RTL-SDR: {state}' + (f': {detail}' if detail else ''), flush=True)
            last_state = state

    try:
        while not stop.is_set():
            clear_feed(directory)
            try:
                ownership_fn()
                chosen = select(*discover_fn(), selection, serial)
                command = [str(binary), '--device-type', 'rtlsdr', '--device='+chosen['serial'],
                           '--write-json', str(directory), '--write-json-every', '1', '--quiet', *options]
                child = launch(command)
                status('starting', chosen)
                while not stop.wait(2):
                    if child.poll() is not None:
                        raise SelectionError('decoder_exited', 'Decoder stopped; rediscovering attached hardware.')
                    ownership_fn()
                    current = select(*discover_fn(), selection, serial)
                    if current != chosen:
                        raise SelectionError('device_changed', 'Receiver changed; restarting with its current identity.')
                    # A running decoder alone does not prove reception.
                    status('running', chosen)
            except SelectionError as error:
                status(error.state, detail=error)
            except (OSError, ValueError) as error:
                status('device_unavailable', detail=error)
            finally:
                if child is not None:
                    stop_decoder(child)
                    child = None
                clear_feed(directory)
            stop.wait(5)
    finally:
        if child is not None:
            stop_decoder(child)
        clear_feed(directory)
        status('stopped')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--readsb', required=True)
    parser.add_argument('--selection', choices=('auto', 'pinned'), default='auto')
    parser.add_argument('--serial')
    parser.add_argument('--json-directory', default='/run/readsb')
    parser.add_argument('decoder_options', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    options = args.decoder_options
    if options[:1] == ['--']:
        options = options[1:]
    # Selection and output are owned by this supervisor, not forwarded options.
    if any(option.split('=')[0] in ('--device', '--device-type', '--write-json', '--write-json-every')
           for option in options):
        parser.error('Decoder device and JSON options are managed by the supervisor.')
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    supervise(args.readsb, options, Path(args.json_directory), args.selection, args.serial, stop)


if __name__ == '__main__':
    main()
