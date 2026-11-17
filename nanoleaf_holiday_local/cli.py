"""Linux CLI. The controller is selected before any authenticated LAN request."""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import getpass
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import resource
import shutil
import socket
import subprocess
import sys
import warnings

from . import native, scenes
from .state import locked, private_dir, read_private, state_root, write_private

REPO = Path(__file__).resolve().parents[1]
LAN_RANGES = tuple(ipaddress.ip_network(cidr) for cidr in
                   ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '169.254.0.0/16'))


def local_ip(host):
    try:
        address = ipaddress.IPv4Address(host)
    except ValueError:
        raise ValueError('Use a local IPv4 address, not a hostname') from None
    if not any(address in block for block in LAN_RANGES):
        raise ValueError('Refusing a non-LAN address')
    return str(address)


def native_identity(host):
    session = native.Session(local_ip(host), timeout=3)
    with closing(session.connection):
        return session.identify()[1].decode().upper()


def discover():
    try:
        import dbus
        from dbus.mainloop.glib import DBusGMainLoop
        from gi.repository import GLib
    except ImportError:
        raise RuntimeError('Discovery needs python3-dbus, python3-gi and Avahi; use --host if the IP is known') from None
    DBusGMainLoop(set_as_default=True)
    try:
        bus = dbus.SystemBus()
        server = dbus.Interface(bus.get_object('org.freedesktop.Avahi', '/'), 'org.freedesktop.Avahi.Server')
    except dbus.DBusException:
        raise RuntimeError('Avahi discovery is unavailable; use --host if the IP is known') from None
    found, browsers = set(), []
    loop = GLib.MainLoop()

    def item(interface, protocol, name, kind, domain, _flags):
        try:
            result = server.ResolveService(interface, protocol, name, kind, domain, 0, 0, timeout=2)
            found.add(local_ip(result[7]))
        except (dbus.DBusException, ValueError):
            pass

    try:
        for kind in ('_ltpdu._tcp', '_nanoleafapi._tcp'):
            path = server.ServiceBrowserNew(-1, 0, kind, 'local', 0)
            browser = dbus.Interface(bus.get_object('org.freedesktop.Avahi', path),
                                     'org.freedesktop.Avahi.ServiceBrowser')
            browsers.append(browser)
            browser.connect_to_signal('ItemNew', item)
        GLib.timeout_add_seconds(4, lambda: (loop.quit(), False)[1])
        loop.run()
    except dbus.DBusException:
        raise RuntimeError('Avahi discovery is unavailable; use --host if the IP is known') from None
    finally:
        for browser in browsers:
            browser.Free()

    matches = 0
    for host in sorted(found):
        try:
            identity = native_identity(host)
        except (ValueError, RuntimeError, OSError, http.client.HTTPException):
            continue
        print(f'{host}  {native.MODEL}  firmware {native.FIRMWARE}  device ending {identity[-6:]}')
        matches += 1
    if not matches:
        print('No supported lights found. If you know the local IP, use --host.')


def doc_json(path):
    return json.loads(read_private(path))


def select_device(root, host, *, enroll=False):
    record = root/'device.json'
    saved = doc_json(record) if record.exists() else None
    if not host and not saved:
        raise ValueError('Run discover, then specify your light with --host')
    host = local_ip(host or saved['host'])
    identity = native_identity(host)  # Read-only: no password or token yet.
    if saved and identity != saved['identity']:
        raise ValueError('Device ID does not match the selected light; no credentials were sent')
    if not saved:
        if not enroll:
            raise ValueError('Run enable-api first to select the device')
        print(f'Target: {native.MODEL} firmware {native.FIRMWARE} at {host}, device ending {identity[-6:]}')
        if input('Is this your light? Type yes to continue: ').strip() != 'yes':
            raise RuntimeError('Device not selected; no credentials were sent')
    if saved != {'host': host, 'identity': identity}:
        write_private(record, {'host': host, 'identity': identity}, replace=True)
    return host, identity


def hidden(prompt):
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise RuntimeError('Run interactively in a terminal to enter credentials without echo') from None


def setup_code():
    code = hidden('Printed 11-digit Matter setup code: ').replace('-', '').replace(' ', '')
    if not re.fullmatch(r'[0-9]{11}', code):
        raise ValueError('Expected the original 11-digit code printed on the device, not a sharing code')
    return code


def matter(root, arguments, data):
    node = shutil.which('node')
    if not node:
        raise RuntimeError('Node.js is needed only for Bluetooth pairing; see README')
    state = root/'matter'
    private_dir(state)
    config_path = state/'no-user-config.json'
    if config_path.exists() or config_path.is_symlink():
        raise RuntimeError('Unexpected Matter configuration; refusing to use it')
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(Path.home()), 'NOBLE_BINDINGS': 'dbus',
           'DBUS_SYSTEM_BUS_ADDRESS': 'unix:socket=/run/dbus/system_bus_socket',
           'MATTER_PATH_CONFIG': str(config_path), 'MATTER_STORAGE_PATH': str(state),
           'MATTER_TRACE_ENABLE': 'false', 'MATTER_DCL_FETCHTESTCERTIFICATES': 'false',
           'MATTER_DCL_FETCHGITHUBCERTIFICATES': 'false'}
    try:
        result = subprocess.run([node, str(REPO/'commission.mjs'), *arguments],
                                input=json.dumps(data), text=True, env=env,
                                capture_output=True, timeout=240,
                                preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_CORE, (0, 0)))
    except subprocess.TimeoutExpired:
        raise RuntimeError('Matter timed out. Look for the light on Wi-Fi before retrying.') from None
    return result.returncode, result.stdout.splitlines()


def pair(root, interface):
    try:
        socket.if_nametoindex(interface)
    except OSError:
        raise ValueError('Unknown network interface; see ip -brief link') from None
    if matter(root, ['check'], {}) != (0, ['matter-ready']):
        raise RuntimeError('Matter runtime not ready; check Node.js and run the npm ci command in README')
    code = setup_code()
    ssid = input('Wi-Fi network name (SSID): ')
    password = hidden('Wi-Fi password: ')
    if not 1 <= len(ssid.encode()) <= 32 or not 8 <= len(password.encode()) <= 63:
        raise ValueError('Expected a Wi-Fi name and an 8–63 byte personal-network password')
    receipt = root/'commissioning.json'
    write_private(receipt, {'stage': 'started'}, replace=True)
    print('Pairing one matching light. This can take up to four minutes.', flush=True)
    error, status = matter(root, [], {'code': code, 'ssid': ssid, 'password': password, 'iface': interface})
    if error or 'matter-wifi-commissioned' not in status:
        details = [s for s in status if re.fullmatch(r'matter-step-\d{1,2}\.\d{1,2}|failed-(input|start|discovery|commission)|production-attestation-rejected', s)]
        write_private(receipt, {'stage': 'not_confirmed', 'details': details}, replace=True)
        raise RuntimeError('Pairing not confirmed. Check Wi-Fi before retrying. ' + ', '.join(details))
    write_private(receipt, {'stage': 'wifi_commissioned'}, replace=True)
    print('Wi-Fi pairing complete. Run discover, then enable-api --host with the reported address.')


def saved_session(host, identity, credentials):
    for attempt in range(2):
        session = native.Session(host, identity)
        try:
            session.identify()
            session.client_id = credentials['client_id']
            session.handshake(credentials['username'], bytes.fromhex(credentials['token']), 2)
            return session
        except BaseException as exc:
            session.connection.close()
            # This exact empty challenge rejection was transient on 5.2.59.
            # Do not retry a proof, a credential allocation or an encrypted write.
            if (attempt or session.auth_stage != 'challenge'
                    or type(exc) is not RuntimeError or str(exc) != 'nlsecure: HTTP 400; 0 response bytes'):
                raise


def openapi_ok(host, token):
    if not re.fullmatch('[A-Za-z0-9]{1,256}', token):
        raise ValueError('Unexpected OpenAPI token format')
    with closing(http.client.HTTPConnection(host, 16021, timeout=10)) as conn:
        conn.request('GET', '/api/v1/'+token+'/')
        response = conn.getresponse()
        body = response.read(65537)
        if len(body) > 65536:
            raise ValueError('Oversized OpenAPI response')
        if response.status == 401:
            return False
        if response.status != 200:
            raise RuntimeError(f'OpenAPI did not return a usable response (HTTP {response.status})')
        if json.loads(body).get('model') != native.MODEL:
            raise ValueError('OpenAPI reported an unexpected device model')
        return True


def new_native(root, session):
    info = session.identify()
    username = info[6].decode().split('-', 1)[0]
    session.handshake(username, bytes(map(int, setup_code())), 1)
    marker = root/'native-allocation-started'
    write_private(marker, b'An uncertain response must not allocate another credential automatically.\n')
    reply = native.fields(session.encrypted('nlsecure', native.tlv(5, b'')))
    if len(reply.get(6, b'')) != 32:
        raise ValueError('Unexpected native credential; no retry attempted')
    creds = {'username': username, 'client_id': session.client_id, 'token': reply[6].hex()}
    write_private(root/'pending-native.json', creds)
    marker.unlink()
    return creds


def enable_api(root, host, identity, fresh):
    pending = root/'pending-native.json'
    credentials = root/'native.json'
    if pending.exists():
        creds = doc_json(pending)
        session = saved_session(host, identity, creds)
    elif credentials.exists() and not fresh:
        creds = doc_json(credentials)
        session = saved_session(host, identity, creds)
    elif (root/'native-allocation-started').exists():
        raise RuntimeError('Previous native registration has an uncertain result; not allocating another credential')
    else:
        session = native.Session(host, identity)
        try:
            creds = new_native(root, session)
        except BaseException:
            session.connection.close()
            raise
    with closing(session.connection):
        enabled = session.request('openapi/enable')
        if enabled == b'\0':
            session.request('openapi/enable', b'\1', 1)
            enabled = session.request('openapi/enable')
        if enabled != b'\1':
            raise ValueError('The API did not report enabled after readback')
        token_path = root/'openapi-token'
        pending_token = root/'pending-openapi-token'
        marker = root/'openapi-allocation-started'
        if pending_token.exists():
            token = read_private(pending_token).decode().strip()
        elif token_path.exists() and openapi_ok(host, saved := read_private(token_path).decode().strip()):
            token = saved
        elif marker.exists():
            raise RuntimeError('Previous API token allocation has an uncertain result; not allocating another token')
        else:
            write_private(marker, b'API token allocation started.\n')
            token = session.request('openapi/token_gen').rstrip(b'\0').decode('ascii')
            write_private(pending_token, token.encode())
            marker.unlink()
        if not openapi_ok(host, token):
            raise RuntimeError('API token not accepted; staged state kept and no further token allocated')
        write_private(credentials, creds, replace=True)
        write_private(token_path, token.encode(), replace=True)
        pending.unlink(missing_ok=True)
        pending_token.unlink(missing_ok=True)
        print('OpenAPI enabled and verified. The token was saved privately, not printed.')


def save_scene(root, host, identity, preset):
    if preset is None:
        preset = REPO/'classic-christmas.json'
    pattern = json.loads(preset.read_text(encoding='utf-8'))
    if not isinstance(pattern.get('name'), str) or not 1 <= len(pattern['name'].encode()) <= 64 or not pattern['name'].isprintable():
        raise ValueError('Scene name must be 1–64 printable UTF-8 bytes')
    session = saved_session(host, identity, doc_json(root/'native.json'))
    with closing(session.connection):
        before = scenes.snapshot(session)
        name = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f') + '.json'
        write_private(root/'scene-backups'/name, before)
        scenes.store_static(session, pattern, before, allow_existing=True)
    print('Pattern saved and verified; existing scenes were not overwritten.')


def arguments(argv):
    parser = argparse.ArgumentParser(description='Set up and add static patterns to NL71K2 Holiday String Lights.')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('doctor', help='check local dependencies; no pairing or device writes')
    sub.add_parser('discover', help='find supported lights on the LAN without credentials')
    pairing = sub.add_parser('pair', help='join an unpaired or already-reset light to Wi-Fi over Bluetooth')
    pairing.add_argument('--interface', required=True, help='LAN interface name from ip -brief link')
    for name, help_text in [('enable-api', 'authorize local control of a light on Wi-Fi'),
                            ('install-scene', 'save the bundled Christmas scene or a palette JSON file'),
                            ('link-ha', 'open a 30-second authorization window for Home Assistant')]:
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument('--host', type=local_ip, help='light’s local IPv4 address; defaults to the selected light')
        if name == 'enable-api':
            cmd.add_argument('--fresh', action='store_true', help='register again after a physical reset')
        if name == 'install-scene':
            cmd.add_argument('--palette', type=Path, help='JSON file in the same format as the example')
    return parser.parse_args(argv)


def main(argv=None):
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    args = arguments(argv)
    try:
        if args.command == 'discover':
            discover()
            return 0
        root = state_root()
        with locked(root):
            if args.command == 'doctor':
                print('Python and cryptography ready. Private state directory checked.')
                if shutil.which('node') and matter(root, ['check'], {}) == (0, ['matter-ready']):
                    print('Optional Matter.js runtime ready. No Bluetooth pairing attempted.')
                else:
                    print('Optional Matter.js runtime missing; see Pair a new light in README.')
            elif args.command == 'pair':
                pair(root, args.interface)
            else:
                host, identity = select_device(root, args.host, enroll=args.command == 'enable-api')
                if args.command == 'enable-api':
                    enable_api(root, host, identity, args.fresh)
                elif args.command == 'install-scene':
                    save_scene(root, host, identity, args.palette)
                else:
                    session = saved_session(host, identity, doc_json(root/'native.json'))
                    with closing(session.connection):
                        session.request('openapi/pair', b'\1', 1)
                    print('Authorization window open for 30 seconds. Complete pairing in Home Assistant.')
    except KeyboardInterrupt:
        print('Stopped. Check the light before retrying any interrupted write.', file=sys.stderr)
        return 130
    except (ValueError, RuntimeError) as err:
        print(f'Stopped: {err}', file=sys.stderr)
        return 1
    except Exception as err:
        print(f'Stopped: {type(err).__name__}. Check prerequisites and local state.', file=sys.stderr)
        return 1
    return 0
