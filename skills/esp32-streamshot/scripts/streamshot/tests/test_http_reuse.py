"""Real loopback TLS fixtures; no phone, private configuration or serial I/O."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import ssl
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import streamshot_mcp as mcp


class FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args): pass

    def do_GET(self): self.respond()
    def do_POST(self): self.respond()

    def respond(self):
        length = int(self.headers.get('Content-Length', '0'))
        value = json.loads(self.rfile.read(length)) if length else None
        self.server.requests.append((self.path, self.client_address[1], value))
        mode = self.server.mode
        if mode == 'drop':
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        if mode == 'redirect':
            self.send_response(307)
            self.send_header('Location', 'https://unexpected.invalid/v1/status')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        answer = {'v': 1, 'ok': True}
        if self.path == '/v1/actions':
            answer = {'ok': True, 'id': value['id'], 'status': 'acknowledged_not_visual_confirmation'}
        data = json.dumps(answer).encode()
        if mode == 'invalid-json': data = b'not-json'
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain' if mode == 'wrong-type' else 'application/json')
        self.send_header('Content-Length', str(len(data)))
        if mode == 'close':
            self.send_header('Connection', 'close')
            self.close_connection = True
        self.end_headers()
        self.wfile.write(data)


class FixtureServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address):
        self.mode, self.requests, self.accepted = 'normal', [], []
        super().__init__(address, FixtureHandler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(3)
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.accepted.append(connection)
        return connection, address

    def handle_error(self, *args): pass


class TLSFixture:
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.temporary.name) / 'state'
        mcp.bridge.init_state(cls.directory, '127.0.0.1', 8765, origin_host='reuse-fixture.local')
        cls.initial = mcp.bridge.configuration(cls.directory)

    @classmethod
    def tearDownClass(cls): cls.temporary.cleanup()

    def setUp(self):
        self.server = FixtureServer(('127.0.0.1', 0))
        self.start_server()

    def start_server(self):
        self.sni = []
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.directory / 'certificate.pem', self.directory / 'private-key.pem')
        context.set_servername_callback(lambda connection, name, context: self.sni.append(name))
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.clients = []
        self.addCleanup(self.close_server)
        port = self.server.server_address[1]
        self.config = dict(self.initial, port=port, baseURL=f'https://reuse-fixture.local:{port}')

    def close_server(self):
        for client in self.clients: client.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def client(self, **changes):
        with patch.object(mcp.bridge, 'configuration', return_value=dict(self.config, **changes)):
            client = mcp.Client(self.directory)
        self.clients.append(client)
        return client

    def action(self):
        return dict(v=1, id=str(uuid.uuid4()), session_id=str(uuid.uuid4()),
                    frame_id=str(uuid.uuid4()), op='click', button='left')


class HTTPReuseTests(TLSFixture, unittest.TestCase):
    def test_successful_requests_reuse_one_tls_connection_and_fixed_origin(self):
        client = self.client()
        original = socket.getaddrinfo
        addresses = []
        def lookup(host, *args, **kwargs):
            addresses.append(host)
            self.assertEqual(host, '127.0.0.1')
            return original(host, *args, **kwargs)
        with patch.object(socket, 'getaddrinfo', side_effect=lookup), \
             patch.dict('os.environ', {'https_proxy': 'http://unexpected.invalid:9',
                                       'HTTPS_PROXY': 'http://unexpected.invalid:9'}):
            self.assertTrue(client.request('GET', '/v1/status')['ok'])
            action = self.action()
            self.assertEqual(client.request('POST', '/v1/actions', action)['id'], action['id'])
            self.assertTrue(client.request('POST', '/v1/stop', {})['ok'])
        self.assertEqual(len(self.server.accepted), 1)
        self.assertEqual(len({entry[1] for entry in self.server.requests}), 1)
        self.assertEqual(addresses, ['127.0.0.1'])
        self.assertEqual(self.sni, ['reuse-fixture.local'])
        self.assertEqual(client.transport.connection.timeout, 12)
        self.assertNotEqual(client.transport.connection.sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY), 0)

    def test_lost_action_response_is_unknown_and_never_retried(self):
        client = self.client()
        client.request('GET', '/v1/status')
        self.server.mode = 'drop'
        with self.assertRaisesRegex(mcp.SafeFailure, '^bridge_request_outcome_unknown$'):
            client.request('POST', '/v1/actions', self.action())
        self.assertEqual([entry[0] for entry in self.server.requests], ['/v1/status', '/v1/actions'])
        self.assertEqual(len(self.sni), 1)
        self.assertIsNone(client.transport.connection)
        # A subsequent explicit request reconnects; the lost action is absent.
        self.server.mode = 'normal'
        self.assertTrue(client.request('GET', '/v1/status')['ok'])
        self.assertEqual(len(self.sni), 2)
        self.assertEqual(sum(entry[0] == '/v1/actions' for entry in self.server.requests), 1)

    def test_stale_idle_socket_is_not_reconnected_within_the_failed_action(self):
        client = self.client()
        client.request('GET', '/v1/status')
        self.server.accepted[0].shutdown(socket.SHUT_RDWR)
        with self.assertRaisesRegex(mcp.SafeFailure, '^bridge_request_outcome_unknown$'):
            client.request('POST', '/v1/actions', self.action())
        self.assertEqual(len(self.sni), 1)
        self.assertEqual([entry[0] for entry in self.server.requests], ['/v1/status'])
        self.assertIsNone(client.transport.connection)

    def test_idle_connection_is_retired_before_sending_a_new_explicit_action(self):
        client = self.client()
        clock = [100.0]
        client.transport.clock = lambda: clock[0]
        client.request('GET', '/v1/status')
        self.assertEqual(client.transport.completed_at, 100.0)
        clock[0] = 109.9
        client.request('GET', '/v1/status')
        self.assertEqual(len(self.sni), 1)
        self.assertEqual(client.transport.completed_at, 109.9)
        clock[0] = 119.9
        action = self.action()
        self.assertEqual(client.request('POST', '/v1/actions', action)['id'], action['id'])
        self.assertEqual(len(self.sni), 2)
        self.assertEqual(sum(entry[0] == '/v1/actions' for entry in self.server.requests), 1)
        self.assertEqual(client.transport.completed_at, 119.9)

    def test_redirect_is_not_followed_and_connection_is_discarded(self):
        client = self.client()
        self.server.mode = 'redirect'
        with self.assertRaisesRegex(mcp.SafeFailure, '^bridge_redirect_rejected$'):
            client.request('GET', '/v1/status')
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(self.sni, ['reuse-fixture.local'])
        self.assertIsNone(client.transport.connection)
        self.server.mode = 'normal'
        self.assertTrue(client.request('GET', '/v1/status')['ok'])
        self.assertEqual(len(self.sni), 2)

    def test_unread_or_invalid_body_discards_connection(self):
        for mode in ('wrong-type', 'invalid-json'):
            with self.subTest(mode=mode):
                client = self.client()
                self.server.mode = mode
                with self.assertRaises(mcp.SafeFailure):
                    client.request('GET', '/v1/status')
                self.assertIsNone(client.transport.connection)
                self.server.mode = 'normal'
                self.assertTrue(client.request('GET', '/v1/status')['ok'])

    def test_server_requested_close_allows_only_next_explicit_request_to_reconnect(self):
        client = self.client()
        self.server.mode = 'close'
        self.assertTrue(client.request('GET', '/v1/status')['ok'])
        self.assertIsNone(client.transport.connection)
        self.server.mode = 'normal'
        self.assertTrue(client.request('GET', '/v1/status')['ok'])
        self.assertEqual(len(self.sni), 2)

    def test_changed_origin_does_not_reuse_connection_or_bypass_hostname_verification(self):
        client = self.client()
        client.request('GET', '/v1/status')
        request = urllib.request.Request('https://unexpected.invalid/v1/status')
        request.timeout = 12
        with self.assertRaisesRegex(mcp.SafeFailure, '^request_not_allowed$'):
            client.transport.https_open(request)
        self.assertEqual(len(self.sni), 1)
        bad = self.client(baseURL=f'https://uncovered-fixture.local:{self.config["port"]}')
        with self.assertRaisesRegex(mcp.SafeFailure, '^bridge_unavailable$'):
            bad.request('GET', '/v1/status')
        self.assertEqual(self.sni, ['reuse-fixture.local', 'uncovered-fixture.local'])
        self.assertIsNone(bad.transport.connection)


class ActualBridgeHTTPReuseTests(TLSFixture, unittest.TestCase):
    """Exercise the production HTTP/1.1 server without a controller or phone."""
    def setUp(self):
        self.server = mcp.bridge.Server(('127.0.0.1', 0), mcp.bridge.Session(),
                                       self.initial['uploadToken'], self.initial['agentToken'], '')
        self.start_server()
        self.server.authority = urllib.parse.urlsplit(self.config['baseURL']).netloc

    def test_production_status_reuses_tls_and_has_no_screen_or_controller(self):
        client = self.client()
        for _ in range(4):
            status = client.request('GET', '/v1/status')
            self.assertFalse(status['active'])
            self.assertFalse(status['controller']['available'])
        self.assertEqual(self.sni, ['reuse-fixture.local'])
        self.assertIsNone(self.server.session.pending)
        self.assertIsNone(self.server.session.frame)

if __name__ == '__main__': unittest.main()
