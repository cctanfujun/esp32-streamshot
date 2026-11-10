"""MCP-owned local bridge and Bonjour lifecycle; no UI, capture or input grants.

Only this process's Popen children are stopped. An existing listener is adopted
only after private-certificate TLS, the agent token and v1 status all validate.
"""
import errno
import fcntl
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import socket
import ssl
import stat
import subprocess
import sys
import time
import urllib.parse
import uuid

import streamshot_bridge as bridge

SERVICE_TYPE = '_streamshot._tcp.'
PAIRING_PROTOCOL = 'explicit-trust-v2'
SAFE_CODES = frozenset(('runtime_network_unavailable', 'runtime_network_changed',
    'runtime_configuration_unavailable', 'runtime_already_running',
    'runtime_port_in_use', 'runtime_service_unavailable',
    'runtime_discovery_unavailable', 'runtime_discovery_upgrade_required',
    'runtime_serial_configuration_conflict', 'runtime_sharing_upgrade_required'))


class RuntimeFailure(Exception):
    def __init__(self, code):
        self.code = code if code in SAFE_CODES else 'runtime_service_unavailable'
        super().__init__(self.code)


def _command(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        raise RuntimeFailure('runtime_network_unavailable') from None
    if result.returncode:
        raise RuntimeFailure('runtime_network_unavailable')
    return result.stdout.strip()


def machine_network():
    """Use an actual default Ethernet/Wi-Fi interface, never a guessed peer IP."""
    route = _command(['/sbin/route', '-n', 'get', 'default'])
    match = re.search(r'^\s*interface:\s*(en[0-9]+)\s*$', route, re.M)
    if not match:
        raise RuntimeFailure('runtime_network_unavailable')
    address = _command(['/usr/sbin/ipconfig', 'getifaddr', match.group(1)])
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        raise RuntimeFailure('runtime_network_unavailable') from None
    if ip.version != 4 or not ip.is_private or ip.is_loopback or ip.is_unspecified:
        raise RuntimeFailure('runtime_network_unavailable')
    label = _command(['/usr/sbin/scutil', '--get', 'LocalHostName']).lower()
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label):
        raise RuntimeFailure('runtime_network_unavailable')
    name = _command(['/usr/sbin/scutil', '--get', 'ComputerName'])
    if not name or any(ord(c) < 32 for c in name):
        raise RuntimeFailure('runtime_network_unavailable')
    # DNS-SD instance labels are bounded UTF-8 strings.
    name = name.encode('utf-8')[:63].decode('utf-8', errors='ignore')
    return {'host': str(ip), 'originHost': label + '.local', 'name': name}


def _private_write(path, value):
    """Create once: never replace connection credentials or an existing identity."""
    data = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, 'wb') as output:
        output.write(data)


def discovery_identity(directory, config, network):
    origin = config.get('originHost')
    if not origin:
        # A legacy IP certificate is kept intact; migration is an explicit step.
        raise RuntimeFailure('runtime_discovery_upgrade_required')
    if origin != network['originHost']:
        raise RuntimeFailure('runtime_network_changed')
    path = Path(directory) / 'discovery.json'
    if not path.exists():
        _private_write(path, {'version': 1, 'id': str(uuid.uuid4()),
                             'name': network['name'], 'originHost': origin})
    try:
        value = bridge.decode(bridge.private_file(path).read_bytes())
        bridge.exact(value, {'version', 'id', 'name', 'originHost'})
        bridge.uid(value['id'])
        if (type(value['version']) is not int or value['version'] != 1
                or value['originHost'] != origin or not isinstance(value['name'], str)
                or not value['name'] or len(value['name'].encode('utf-8')) > 63
                or any(ord(c) < 32 for c in value['name'])):
            raise ValueError()
        return value
    except Exception:
        raise RuntimeFailure('runtime_configuration_unavailable') from None


def authenticated_request(directory, config, method='GET', path='/v1/status', timeout=1.5):
    """Connect to the bound IP but verify its configured stable DNS origin.

    Neither discovery TXT nor the system CA store is a trust source. No redirects,
    proxies, image requests, response-body logs or automatic action retries exist.
    """
    if (method, path) not in {('GET', '/v1/status'), ('POST', '/v1/stop')}:
        raise RuntimeFailure('runtime_service_unavailable')
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    context.load_verify_locations(cafile=str(bridge.private_file(Path(directory) / 'certificate.pem')))
    origin = urllib.parse.urlsplit(config['baseURL'])
    connection = None
    try:
        raw = socket.create_connection((config['host'], config['port']), timeout=timeout)
        try:
            secure = context.wrap_socket(raw, server_hostname=origin.hostname)
        except Exception:
            raw.close()
            raise
        connection = http.client.HTTPConnection(config['host'], config['port'], timeout=timeout)
        connection.sock = secure
        connection.request(method, path, body=b'{}' if method == 'POST' else None,
                           headers={'Host': origin.netloc,
                                    'Authorization': 'Bearer ' + config['agentToken'],
                                    'Accept': 'application/json', 'Content-Type': 'application/json'})
        response = connection.getresponse()
        if response.status != 200 or response.getheader('Content-Type', '').split(';')[0] != 'application/json':
            raise ValueError()
        body = response.read(16385)
        if len(body) > 16384:
            raise ValueError()
        value = bridge.decode(body)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except Exception:
        raise RuntimeFailure('runtime_service_unavailable') from None
    finally:
        if connection is not None:
            connection.close()


def validate_status(value, serial_port=None, require_discovery=False):
    flags = ('active', 'allow_control', 'has_observation', 'input_outcome_unknown')
    if (not isinstance(value, dict) or type(value.get('v')) is not int or value['v'] != 1
            or any(type(value.get(k)) is not bool for k in flags)):
        raise RuntimeFailure('runtime_service_unavailable')
    if value['active'] != (value.get('session_id') is not None):
        raise RuntimeFailure('runtime_service_unavailable')
    if value.get('control_protocol') != 'desktop-lease-v1':
        # Never adopt a listener whose phone-side permission semantics differ.
        # The caller may explicitly restart its own old service; no foreign
        # listener or saved phone trust is silently replaced here.
        raise RuntimeFailure('runtime_service_unavailable')
    if value.get('sharing_protocol') != bridge.SCREEN_RECOVERY_PROTOCOL:
        # An older listener cannot atomically bind resume_from or distinguish
        # a heartbeat interruption from an explicit screen revocation.
        raise RuntimeFailure('runtime_sharing_upgrade_required')
    if value['active']:
        try: bridge.uid(value['session_id'])
        except bridge.Failure: raise RuntimeFailure('runtime_service_unavailable') from None
    controller = value.get('controller')
    if (not isinstance(controller, dict) or type(controller.get('available')) is not bool
            or type(controller.get('armed')) is not bool):
        raise RuntimeFailure('runtime_service_unavailable')
    if serial_port is not None and (not controller['available'] or controller.get('serial_port') != serial_port):
        raise RuntimeFailure('runtime_serial_configuration_conflict')
    if require_discovery and (value.get('pairing_available') is not True
                              or value.get('pairing_protocol') != PAIRING_PROTOCOL):
        raise RuntimeFailure('runtime_discovery_unavailable')
    return value


def port_available(host, port):
    try:
        with socket.socket(socket.AF_INET6 if ':' in host else socket.AF_INET, socket.SOCK_STREAM) as probe:
            # Match Server.bind: a closed owned connection may leave TIME_WAIT,
            # which does not prevent the bridge from binding with address reuse.
            if bridge.Server.allow_reuse_address:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, port))
        return True
    except OSError as error:
        if error.errno == errno.EADDRINUSE:
            return False
        raise RuntimeFailure('runtime_network_changed') from None


class Runtime:
    def __init__(self, state_dir=bridge.STATE, serial_port=None, *,
                 network_factory=machine_network, request=authenticated_request,
                 popen=subprocess.Popen, available=port_available, sleep=time.sleep,
                 clock=time.monotonic):
        self.directory = Path(state_dir).expanduser().absolute()
        self.serial_port = serial_port
        self.network_factory, self.request, self.popen = network_factory, request, popen
        self.available, self.sleep, self.clock = available, sleep, clock
        self.backend = self.publisher = None
        self.config = self.identity = None
        self.lock_fd = None
        self.ready = False

    def _lock(self):
        self.directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        identity = hashlib.sha256(str(self.directory).encode()).hexdigest()[:24]
        path = self.directory.parent / ('.streamshot-runtime-' + identity + '.lock')
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                os.close(fd); raise RuntimeFailure('runtime_configuration_unavailable')
            try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd); raise RuntimeFailure('runtime_already_running') from None
            self.lock_fd = fd
        except RuntimeFailure: raise
        except OSError: raise RuntimeFailure('runtime_configuration_unavailable') from None

    def start(self):
        try:
            if self.serial_port is not None and not re.fullmatch(
                    r'/dev/(?:cu\.(?:usbmodem|usbserial)[A-Za-z0-9_.-]*|tty(?:ACM|USB)[0-9]+)', self.serial_port):
                raise RuntimeFailure('runtime_serial_configuration_conflict')
            self._lock()
            network = self.network_factory()
            if not self.directory.exists():
                bridge.init_state(self.directory, network['host'], 8765,
                                  origin_host=network['originHost'], name=network['name'])
            try:
                self.config = bridge.configuration(self.directory)
            except Exception:
                raise RuntimeFailure('runtime_configuration_unavailable') from None
            if self.config['host'] != network['host']:
                raise RuntimeFailure('runtime_network_changed')
            self.identity = discovery_identity(self.directory, self.config, network)
            try:
                response = self.request(self.directory, self.config)
            except RuntimeFailure:
                if not self.available(self.config['host'], self.config['port']):
                    raise RuntimeFailure('runtime_port_in_use') from None
                adopted = False
            else:
                validate_status(response, self.serial_port, require_discovery=True)
                adopted = True
            if not adopted:
                args = [sys.executable, str(Path(bridge.__file__).resolve()), '--state', str(self.directory),
                        'serve', '--discovery']
                if self.serial_port is not None:
                    args += ['--serial', self.serial_port]
                self.backend = self.popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL, start_new_session=True)
                deadline = self.clock() + 8
                while True:
                    if self.backend.poll() is not None:
                        raise RuntimeFailure('runtime_service_unavailable')
                    try:
                        validate_status(self.request(self.directory, self.config), self.serial_port, require_discovery=True)
                        break
                    except RuntimeFailure as error:
                        if error.code == 'runtime_serial_configuration_conflict' or self.clock() >= deadline:
                            raise
                        self.sleep(.15)
            self.ready = True
            self.publisher = self.popen(['/usr/bin/dns-sd', '-R', self.identity['name'], SERVICE_TYPE,
                'local.', str(self.config['port']), 'v=1', 'id=' + self.identity['id'],
                'pair=' + PAIRING_PROTOCOL], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            self.sleep(.1)
            if self.publisher.poll() is not None:
                raise RuntimeFailure('runtime_discovery_unavailable')
            return self
        except RuntimeFailure:
            self.close(); raise
        except Exception:
            self.close(); raise RuntimeFailure('runtime_service_unavailable') from None

    @staticmethod
    def _stop_child(child):
        if child is None or child.poll() is not None:
            return
        try:
            child.send_signal(signal.SIGINT)
            child.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            if child.poll() is None:
                child.terminate()
                try: child.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired): pass

    def close(self):
        if self.ready and self.config is not None:
            try: self.request(self.directory, self.config, 'POST', '/v1/stop')
            except Exception: pass
        self.ready = False
        # Only Popen handles created here can be signalled; adopted listeners stay.
        self._stop_child(self.backend)
        self._stop_child(self.publisher)
        self.backend = self.publisher = None
        if self.lock_fd is not None:
            try: fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            finally: os.close(self.lock_fd); self.lock_fd = None
