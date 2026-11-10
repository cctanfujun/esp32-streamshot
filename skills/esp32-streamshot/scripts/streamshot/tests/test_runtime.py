"""Synthetic MCP lifecycle tests and localhost TLS; no Bonjour or physical device."""
import copy
import errno
import json
from pathlib import Path
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import streamshot_bridge as bridge
import streamshot_runtime as runtime

NETWORK = {'host': '192.168.50.4', 'originHost': 'fixture-mac.local', 'name': 'Fixture Mac'}
CONFIG = {'version': 1, 'host': NETWORK['host'], 'originHost': NETWORK['originHost'],
          'port': 8765, 'baseURL': 'https://fixture-mac.local:8765',
          'uploadToken': '1' * 64, 'agentToken': '2' * 64}
STATUS = {'v': 1, 'active': False, 'allow_control': False, 'session_id': None,
          'control_protocol': 'desktop-lease-v1',
          'sharing_protocol': bridge.SCREEN_RECOVERY_PROTOCOL,
          'capture_protocol': bridge.CAPTURE_PROTOCOL,
          'has_observation': False, 'input_outcome_unknown': False,
          'pairing_available': True,
          'pairing_protocol': runtime.PAIRING_PROTOCOL,
          'controller': {'available': True, 'armed': False, 'serial_port': '/dev/cu.usbmodemFIXTURE'}}


class Child:
    def __init__(self, events, label, exited=None):
        self.events, self.label, self.exited = events, label, exited
    def poll(self): return self.exited
    def send_signal(self, value): self.events.append((self.label, value))
    def wait(self, timeout): self.events.append((self.label, 'wait')); self.exited = 0
    def terminate(self): self.events.append((self.label, 'terminate')); self.exited = -15


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / 'state'
        self.state.mkdir(mode=0o700)
        self.credential_file = self.state / 'private-fixture'
        self.credential_file.write_text('SYNTHETIC_PRIVATE_DATA')
        self.credential_file.chmod(0o600)
        self.events = []
        self.children = []
        self.commands = []
        self.request_calls = []
        self.config_patch = patch.object(runtime.bridge, 'configuration', return_value=copy.deepcopy(CONFIG))
        self.config_patch.start(); self.addCleanup(self.config_patch.stop)

    def request(self, directory, config, method='GET', path='/v1/status'):
        self.request_calls.append((method, path))
        self.events.append(('request', path))
        return copy.deepcopy(STATUS) if method == 'GET' else {'ok': True}

    def spawn(self, args, **kwargs):
        self.commands.append((args, kwargs))
        child = Child(self.events, 'publisher' if args[0] == '/usr/bin/dns-sd' else 'backend')
        self.children.append(child)
        return child

    def instance(self, **kwargs):
        defaults = dict(network_factory=lambda: copy.deepcopy(NETWORK), request=self.request,
                        popen=self.spawn, available=lambda *_: False, sleep=lambda _: None)
        defaults.update(kwargs)
        return runtime.Runtime(self.state, **defaults)

    def test_authenticated_existing_listener_reused_and_txt_contains_no_secrets(self):
        runner = self.instance(serial_port='/dev/cu.usbmodemFIXTURE')
        self.addCleanup(runner.close)
        before = self.credential_file.read_bytes()
        runner.start()
        self.assertIsNone(runner.backend)
        self.assertEqual(len(self.commands), 1)
        args, kwargs = self.commands[0]
        self.assertEqual(args[:5], ['/usr/bin/dns-sd', '-R', 'Fixture Mac', '_streamshot._tcp.', 'local.'])
        self.assertEqual(args[6:], ['v=1', 'id=' + runner.identity['id'], 'pair=explicit-trust-v2'])
        self.assertNotIn(CONFIG['agentToken'], repr(args))
        self.assertNotIn(CONFIG['uploadToken'], repr(args))
        self.assertNotIn('certificate', repr(args))
        self.assertEqual(kwargs['stdout'], subprocess.DEVNULL)
        self.assertEqual(kwargs['stderr'], subprocess.DEVNULL)
        self.assertEqual(self.credential_file.read_bytes(), before)
        self.assertEqual((self.state / 'discovery.json').stat().st_mode & 0o777, 0o600)
        identity = runner.identity['id']
        runner.close()
        self.assertEqual(self.events[-2:], [('publisher', signal.SIGINT), ('publisher', 'wait')])
        second = self.instance(); self.addCleanup(second.close); second.start()
        self.assertEqual(second.identity['id'], identity)
        self.assertEqual(self.credential_file.read_bytes(), before)

    def test_foreign_occupied_port_is_not_killed_or_rekeyed(self):
        request = Mock(side_effect=runtime.RuntimeFailure('runtime_service_unavailable'))
        runner = self.instance(request=request)
        with self.assertRaisesRegex(runtime.RuntimeFailure, '^runtime_port_in_use$'): runner.start()
        self.assertEqual(self.commands, [])
        self.assertEqual(self.credential_file.read_text(), 'SYNTHETIC_PRIVATE_DATA')
        self.assertEqual(request.call_count, 1)

    def test_network_or_hostname_change_does_not_rewrite_trust(self):
        for field, replacement in [('host', '192.168.9.20'), ('originHost', 'different-mac.local')]:
            network = dict(NETWORK, **{field: replacement})
            request = Mock()
            runner = self.instance(network_factory=lambda: network, request=request)
            with self.assertRaisesRegex(runtime.RuntimeFailure, '^runtime_network_changed$'): runner.start()
            request.assert_not_called()
        self.assertEqual(self.commands, [])
        self.assertEqual(self.credential_file.read_text(), 'SYNTHETIC_PRIVATE_DATA')

    def test_legacy_ip_state_requires_explicit_migration(self):
        legacy = dict(CONFIG); legacy.pop('originHost')
        with patch.object(runtime.bridge, 'configuration', return_value=legacy):
            with self.assertRaisesRegex(runtime.RuntimeFailure, '^runtime_discovery_upgrade_required$'):
                self.instance().start()
        self.assertEqual(self.commands, [])

    def test_new_state_initializes_once_with_dns_origin(self):
        self.credential_file.unlink(); self.state.rmdir()
        def initialize(directory, host, port, *, origin_host, name):
            self.assertEqual((host, port, origin_host, name),
                             (NETWORK['host'], 8765, NETWORK['originHost'], NETWORK['name']))
            directory.mkdir(mode=0o700)
        with patch.object(runtime.bridge, 'init_state', side_effect=initialize) as init:
            runner = self.instance(); self.addCleanup(runner.close); runner.start(); runner.close()
            second = self.instance(); self.addCleanup(second.close); second.start()
        self.assertEqual(init.call_count, 1)

    def test_owned_backend_uses_explicit_serial_and_shutdown_stops_session_first(self):
        count = 0
        def request(*args):
            nonlocal count
            count += 1
            if count == 1: raise runtime.RuntimeFailure('runtime_service_unavailable')
            return self.request(*args)
        runner = self.instance(request=request, available=lambda *_: True,
                               serial_port='/dev/cu.usbmodemFIXTURE')
        self.addCleanup(runner.close); runner.start()
        args = self.commands[0][0]
        self.assertEqual(args[-4:], ['serve', '--discovery', '--serial', '/dev/cu.usbmodemFIXTURE'])
        self.assertNotIn(CONFIG['agentToken'], repr(args))
        self.assertNotIn(CONFIG['uploadToken'], repr(args))
        self.assertTrue(all(kwargs['start_new_session'] for _, kwargs in self.commands))
        self.events.clear(); runner.close()
        self.assertEqual(self.events[0], ('request', '/v1/stop'))
        self.assertIn(('backend', signal.SIGINT), self.events)
        self.assertIn(('publisher', signal.SIGINT), self.events)

    def test_no_serial_means_view_only_and_no_device_is_guessed(self):
        count = 0
        def request(*args):
            nonlocal count
            count += 1
            if count == 1: raise runtime.RuntimeFailure('runtime_service_unavailable')
            return dict(STATUS, controller={'available': False, 'armed': False})
        runner = self.instance(request=request, available=lambda *_: True)
        self.addCleanup(runner.close); runner.start()
        self.assertNotIn('--serial', self.commands[0][0])

    def test_existing_view_only_service_is_not_restarted_to_gain_serial(self):
        request = Mock(return_value=dict(STATUS, controller={'available': False, 'armed': False}))
        runner = self.instance(request=request, serial_port='/dev/cu.usbmodemFIXTURE')
        with self.assertRaisesRegex(runtime.RuntimeFailure, '^runtime_serial_configuration_conflict$'): runner.start()
        self.assertEqual(self.commands, [])
        self.assertEqual(request.call_count, 1)

    def test_legacy_phone_permission_service_is_neither_adopted_nor_stopped(self):
        for capability in (None, 'phone-permission-v1'):
            old = dict(STATUS)
            if capability is None: old.pop('control_protocol')
            else: old['control_protocol'] = capability
            request = Mock(return_value=old)
            runner = self.instance(request=request)
            with self.assertRaisesRegex(runtime.RuntimeFailure, '^runtime_service_unavailable$'):
                runner.start()
            self.assertFalse(runner.ready)
            self.assertEqual(self.commands, [])
            self.assertEqual(request.call_count, 1)

    def test_unbound_screen_recovery_listener_is_not_adopted_or_stopped(self):
        for capability in (None, 'unbound-resume-v1'):
            old = dict(STATUS)
            if capability is None: old.pop('sharing_protocol')
            else: old['sharing_protocol'] = capability
            request = Mock(return_value=old)
            runner = self.instance(request=request)
            with self.assertRaisesRegex(runtime.RuntimeFailure, '^runtime_sharing_upgrade_required$'):
                runner.start()
            self.assertFalse(runner.ready)
            self.assertEqual(self.commands, [])
            self.assertEqual(request.call_count, 1)

    def test_pre_settle_listener_is_not_adopted_or_stopped(self):
        for capability in (None, 'idle-cache-v1'):
            old = dict(STATUS)
            if capability is None: old.pop('capture_protocol')
            else: old['capture_protocol'] = capability
            request = Mock(return_value=old)
            runner = self.instance(request=request)
            with self.assertRaisesRegex(runtime.RuntimeFailure, '^runtime_capture_upgrade_required$'):
                runner.start()
            self.assertFalse(runner.ready)
            self.assertEqual(self.commands, [])
            self.assertEqual(request.call_count, 1)

    def test_authenticated_server_without_pairing_or_with_other_serial_is_not_adopted(self):
        for value, code in [
                (dict(STATUS, pairing_available=False), 'runtime_discovery_unavailable'),
                (dict(STATUS, pairing_protocol='srp6a-sha256-v1'), 'runtime_discovery_unavailable'),
                (dict(STATUS, controller={'available': True, 'armed': False, 'serial_port': '/dev/cu.usbmodemOTHER'}),
                 'runtime_serial_configuration_conflict')]:
            runner = self.instance(request=Mock(return_value=value), serial_port='/dev/cu.usbmodemFIXTURE')
            with self.assertRaisesRegex(runtime.RuntimeFailure, '^' + code + '$'): runner.start()
        self.assertEqual(self.commands, [])

    def test_single_owner_lock_and_cleanup_release(self):
        one = self.instance(); self.addCleanup(one.close); one.start()
        two = self.instance()
        with self.assertRaisesRegex(runtime.RuntimeFailure, '^runtime_already_running$'): two.start()
        one.close()
        two.start(); two.close()

    def test_failed_publisher_stops_only_owned_children(self):
        def spawn(args, **kwargs):
            child = self.spawn(args, **kwargs)
            if args[0] == '/usr/bin/dns-sd': child.exited = 1
            return child
        runner = self.instance(popen=spawn)
        with self.assertRaisesRegex(runtime.RuntimeFailure, '^runtime_discovery_unavailable$'): runner.start()
        self.assertEqual(self.events[-1], ('request', '/v1/stop'))
        self.assertEqual(len(self.children), 1)

    def test_invalid_serial_is_rejected_before_local_or_network_actions(self):
        request = Mock(); network = Mock()
        runner = self.instance(request=request, network_factory=network, serial_port='/dev/cu.Bluetooth-Incoming-Port')
        with self.assertRaisesRegex(runtime.RuntimeFailure, '^runtime_serial_configuration_conflict$'): runner.start()
        request.assert_not_called(); network.assert_not_called()

    def test_discovery_file_extra_fields_or_public_permissions_are_rejected(self):
        p = self.state / 'discovery.json'
        p.write_text(json.dumps({'version': 1, 'id': '11111111-1111-4111-8111-111111111111',
                                  'name': 'Fixture', 'originHost': NETWORK['originHost'], 'pin': 'FORBIDDEN'}))
        p.chmod(0o600)
        with self.assertRaisesRegex(runtime.RuntimeFailure, '^runtime_configuration_unavailable$'): self.instance().start()
        p.unlink()
        p.write_text(json.dumps({'version': 1, 'id': '11111111-1111-4111-8111-111111111111',
                                  'name': 'Fixture', 'originHost': NETWORK['originHost']}))
        p.chmod(0o644)
        with self.assertRaisesRegex(runtime.RuntimeFailure, '^runtime_configuration_unavailable$'): self.instance().start()


class LocalTLSTests(unittest.TestCase):
    def test_port_probe_allows_real_time_wait_restart_but_rejects_live_listener(self):
        host = '127.0.0.1'
        server = bridge.Server((host, 0), bridge.Session(), 'synthetic-phone', 'synthetic-agent', '')
        port = server.server_address[1]
        try:
            self.assertFalse(runtime.port_available(host, port))
            with socket.create_connection((host, port), timeout=3) as client:
                connection, _ = server.socket.accept()
                with connection:
                    connection.settimeout(3)
                    # The server sends FIN first and receives the peer's FIN,
                    # leaving its local port in real TIME_WAIT after close.
                    connection.shutdown(socket.SHUT_WR)
                    self.assertEqual(client.recv(1), b'')
                    client.shutdown(socket.SHUT_WR)
                    self.assertEqual(connection.recv(1), b'')
        finally:
            server.server_close()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as ordinary_probe:
            with self.assertRaises(OSError) as caught:
                ordinary_probe.bind((host, port))
            self.assertEqual(caught.exception.errno, errno.EADDRINUSE)
        self.assertTrue(runtime.port_available(host, port))
        restarted = bridge.Server((host, port), bridge.Session(), 'synthetic-phone', 'synthetic-agent', '')
        try:
            self.assertFalse(runtime.port_available(host, port))
            with socket.create_connection((host, port), timeout=3):
                connection, _ = restarted.socket.accept()
                connection.close()
        finally:
            restarted.server_close()

    def test_certificate_and_agent_token_are_both_required_without_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); state = root / 'state'
            with socket.socket() as free:
                free.bind(('127.0.0.1', 0)); port = free.getsockname()[1]
            bridge.init_state(state, '127.0.0.1', port)
            config = bridge.configuration(state)
            session = bridge.Session()
            server = bridge.Server(('127.0.0.1', port), session, config['uploadToken'], config['agentToken'], '127.0.0.1:' + str(port))
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(state / 'certificate.pem', state / 'private-key.pem')
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                result = runtime.authenticated_request(state, config)
                runtime.validate_status(result)
                self.assertFalse(result['active']); self.assertIsNone(session.frame)
                with self.assertRaises(runtime.RuntimeFailure):
                    runtime.authenticated_request(state, dict(config, agentToken='0' * 64))
                wrong = root / 'wrong'; bridge.init_state(wrong, '127.0.0.1', port)
                with self.assertRaises(runtime.RuntimeFailure): runtime.authenticated_request(wrong, config)
                self.assertIsNone(session.frame)
            finally:
                server.shutdown(); server.server_close(); thread.join(3)


class NetworkTests(unittest.TestCase):
    def result(self, outputs):
        def run(args, **kwargs):
            self.assertEqual(kwargs['timeout'], 5)
            return subprocess.CompletedProcess(args, 0, outputs.pop(0), '')
        return run

    def test_machine_local_hostname_and_private_physical_interface(self):
        with patch.object(runtime.subprocess, 'run', side_effect=self.result([
                'interface: en0\n', '192.168.50.4\n', 'Fixture-Mac\n', 'Fixture Mac\n'])):
            self.assertEqual(runtime.machine_network(), NETWORK)

    def test_vpn_public_or_malformed_hostname_is_not_advertised(self):
        for outputs in [
                ['interface: utun4\n'],
                ['interface: en0\n', '8.8.8.8\n'],
                ['interface: en0\n', '127.0.0.1\n'],
                ['interface: en0\n', '192.168.50.4\n', 'bad.local.attacker\n']]:
            with patch.object(runtime.subprocess, 'run', side_effect=self.result(outputs)):
                with self.assertRaisesRegex(runtime.RuntimeFailure, '^runtime_network_unavailable$'):
                    runtime.machine_network()


if __name__ == '__main__': unittest.main()
