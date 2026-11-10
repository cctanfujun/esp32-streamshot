"""MCP tests with mock HTTPS, synthetic JPEG markers and real loopback TLS."""
import base64
import copy
from email.message import Message
import io
import json
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, Mock, patch
import urllib.error
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import streamshot_mcp as mcp

SESSION = '12345678-1234-1234-1234-123456789abc'
FRAME = '22345678-1234-1234-1234-123456789abc'
# Structural SOF fixture, not a decoded photograph or codec-quality test.
JPEG = bytes.fromhex('ffd8ffc00008080003000200ffd9')
METADATA = {'frame_id': FRAME, 'session_id': SESSION, 'width': 2, 'height': 3,
            'orientation': 1, 'geometry_epoch': 1}


class FakeClient:
    def __init__(self):
        self.calls = []
        self.fail_action = False
        self.unknown_action = False
        self.action_error = 'PRIVATE_SERVER_ERROR'
        self.active = True
        self.next_frame = FRAME

    def request(self, method, path, value=None, image=False):
        self.calls.append((method, path, copy.deepcopy(value), image))
        if path == '/v1/screen':
            timings = dict(capture_wait_ms=value['settle_ms'], encode_ms=0,
                           source_callback_age_ms=0, observation_age_ms=0) if value.get('settle_ms', 0) else {}
            return dict(METADATA, frame_id=self.next_frame, **timings), JPEG
        if path == '/v1/status':
            return {'v': 1, 'active': self.active, 'allow_control': True,
                    'control_protocol': 'desktop-lease-v1',
                    'sharing_protocol': mcp.bridge.SCREEN_RECOVERY_PROTOCOL,
                    'capture_protocol': mcp.bridge.CAPTURE_PROTOCOL,
                    'session_id': SESSION if self.active else None, 'has_observation': True,
                    'input_outcome_unknown': False, 'pairing_available': True,
                    'controller': {'available': True, 'armed': False, 'serial_port': '/PRIVATE_LOCAL_DEVICE'},
                    'agentToken': 'PRIVATE_TOKEN_NOT_FORWARDED'}
        if path == '/v1/stop':
            self.active = False
            return {'ok': True}
        if path == '/v1/control/reset':
            mcp.bridge.reset_arguments(value)
            return {'ok': True, 'status': 'control_reset'}
        if path == '/v1/actions':
            if self.fail_action:
                raise RuntimeError('PRIVATE_TOKEN_AND_HTTP_BODY')
            return {'ok': not self.unknown_action, 'id': value['id'],
                    'status': 'outcome_unknown' if self.unknown_action else 'acknowledged_not_visual_confirmation',
                    **({'error': self.action_error} if self.unknown_action else {})}
        raise AssertionError('unexpected fake request')


class FakeRuntime:
    def __init__(self, state, serial_port=None): self.started = self.closed = False
    def start(self): self.started = True
    def close(self): self.closed = True


def request(method, identity=1, params=None):
    return {'jsonrpc': '2.0', 'id': identity, 'method': method, 'params': params or {}}


def initialize(server, version='2025-03-26'):
    result = server.handle(request('initialize', params={'protocolVersion': version,
        'capabilities': {}, 'clientInfo': {'name': 'offline-test', 'version': '1'}}))
    server.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
    return result


def tool(server, name, args=None, identity=10):
    return server.handle(request('tools/call', identity, {'name': name, 'arguments': args if args is not None else {}}))


def control(**changes):
    return dict({'session_id': SESSION, 'frame_id': FRAME, 'op': 'click', 'button': 'left'}, **changes)


def act(**changes):
    return dict({'session_id': SESSION, 'frame_id': FRAME,
                 'actions': [{'op': 'click', 'button': 'left'}]}, **changes)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.factory = Mock(return_value=self.client)
        self.clock = Mock(return_value=100)
        self.tools = mcp.Tools(Path('/unused-private-fixture'), self.factory, self.clock)
        self.tools.sleep = Mock()
        self.server = mcp.MCP(self.tools)

    def test_both_versions_negotiate_without_credentials_or_network(self):
        for version in mcp.VERSIONS:
            server = mcp.MCP(self.tools)
            reply = initialize(server, version)
            self.assertEqual(reply['result']['protocolVersion'], version)
            self.assertEqual(reply['result']['capabilities'], {'tools': {'listChanged': False}})
        self.factory.assert_not_called()
        self.assertEqual(self.client.calls, [])
        self.assertEqual(initialize(mcp.MCP(self.tools), '2099-01-01')['result']['protocolVersion'], '2025-03-26')

    def test_initialization_notification_and_tools_list(self):
        self.assertEqual(self.server.handle(request('tools/list'))['error']['message'], 'not_initialized')
        initialize(self.server)
        names = [item['name'] for item in self.server.handle(request('tools/list'))['result']['tools']]
        self.assertEqual(names, ['streamshot_get_screen', 'streamshot_control', 'streamshot_act', 'streamshot_status', 'streamshot_stop', 'streamshot_reset_control'])
        self.assertNotIn('arm', names)
        self.factory.assert_not_called()

    def test_screen_image_only_in_tools_call_and_metadata_is_exact(self):
        initialize(self.server)
        reply = tool(self.server, 'streamshot_get_screen')['result']
        self.assertFalse(reply['isError'])
        self.assertEqual(reply['content'][0]['mimeType'], 'image/jpeg')
        self.assertEqual(base64.b64decode(reply['content'][0]['data']), JPEG)
        self.assertEqual(json.loads(reply['content'][1]['text']), METADATA)
        self.assertEqual(self.client.calls, [('POST', '/v1/screen', {}, True)])
        self.assertEqual(self.server.control_calls, {})
        self.assertEqual(self.tools.observation, METADATA)
        self.assertNotIn('jpeg', self.tools.observation)

    def test_status_filters_secrets_and_does_not_capture(self):
        initialize(self.server)
        reply = tool(self.server, 'streamshot_status')
        self.assertNotIn('PRIVATE_TOKEN', json.dumps(reply))
        self.assertNotIn('serial_port', json.dumps(reply))
        self.assertNotIn('pairing_available', json.dumps(reply))
        self.assertNotIn('PRIVATE_LOCAL_DEVICE', json.dumps(reply))
        self.assertNotIn('phone_source', json.dumps(reply))
        self.assertNotIn('source_trust', json.dumps(reply))
        self.assertEqual([item[1] for item in self.client.calls], ['/v1/status'])

    def test_status_requires_bound_sharing_protocol_with_explicit_upgrade_error(self):
        initialize(self.server)
        baseline = self.client.request('GET', '/v1/status')
        for capability in (None, 'unbound-resume-v1'):
            old = dict(baseline)
            if capability is None: old.pop('sharing_protocol')
            else: old['sharing_protocol'] = capability
            self.client.request = Mock(return_value=old)
            result = tool(self.server, 'streamshot_status')['result']
            self.assertTrue(result['isError'])
            self.assertEqual(json.loads(result['content'][0]['text'])['error'], 'runtime_sharing_upgrade_required')
            self.assertNotIn('PRIVATE', json.dumps(result))

    def test_status_diagnostics_are_validated_small_values_not_untrusted_text(self):
        initialize(self.server)
        baseline = self.client.request('GET', '/v1/status')
        for reason in (None, *sorted(mcp.bridge.SCREEN_INACTIVE_REASONS)):
            value = dict(baseline, last_inactive_reason=reason, heartbeat_age_seconds=19,
                         screen_request_timeouts=3)
            self.client.request = Mock(return_value=value)
            result = tool(self.server, 'streamshot_status')['result']
            self.assertFalse(result['isError'])
            status = json.loads(result['content'][0]['text'])
            self.assertEqual(status['last_inactive_reason'], reason)
            self.assertEqual(status['heartbeat_age_seconds'], 19)
            self.assertEqual(status['screen_request_timeouts'], 3)
            self.assertNotIn('PRIVATE', json.dumps(status))
        invalid = [('last_inactive_reason', x) for x in ('PRIVATE_UNVALIDATED_REASON', {}, [], False, 1)]
        invalid += [('heartbeat_age_seconds', x) for x in (-1, 1801, True, 1.5, '19')]
        invalid += [('screen_request_timeouts', x) for x in (None, -1, True, 1.5, mcp.bridge.MAX_SEQUENCE + 1)]
        for key, value in invalid:
            self.client.request = Mock(return_value=dict(baseline, **{key: value}))
            result = tool(self.server, 'streamshot_status')['result']
            self.assertTrue(result['isError'])
            self.assertEqual(json.loads(result['content'][0]['text'])['error'], 'invalid_bridge_response')
            self.assertNotIn('PRIVATE', json.dumps(result))
        self.client.request = Mock(return_value=dict(baseline, heartbeat_age_seconds=None,
                                                    screen_request_timeouts=mcp.bridge.MAX_SEQUENCE))
        self.assertFalse(tool(self.server, 'streamshot_status')['result']['isError'])

    def test_status_exposes_registration_source_without_credentials(self):
        initialize(self.server)
        baseline = self.client.request('GET', '/v1/status')
        for source, trust in ((FRAME, 'local-network-registration'),
                              ('legacy', 'private-configuration'), (None, None)):
            with self.subTest(source=source):
                self.client.request = Mock(return_value=dict(baseline, phone_source=source,
                    source_trust=trust, clientSecret='PRIVATE_CLIENT_SECRET', uploadToken='PRIVATE_PHONE_TOKEN'))
                result = tool(self.server, 'streamshot_status')['result']
                self.assertFalse(result['isError'])
                value = json.loads(result['content'][0]['text'])
                self.assertEqual((value['phone_source'], value['source_trust']), (source, trust))
                self.assertNotIn('PRIVATE', json.dumps(value))
                self.assertIsNone(self.tools.observation)
        description = next(item['description'] for item in mcp.tool_definitions()
                           if item['name'] == 'streamshot_status')
        self.assertIn('does not independently authenticate the phone owner', description)

    def test_status_source_fields_are_optional_and_validated_without_false_attribution(self):
        initialize(self.server)
        baseline = self.client.request('GET', '/v1/status')
        for fields in ({'phone_source': FRAME}, {'source_trust': 'local-network-registration'}):
            self.client.request = Mock(return_value=dict(baseline, **fields))
            result = tool(self.server, 'streamshot_status')['result']
            self.assertFalse(result['isError'])
            value = json.loads(result['content'][0]['text'])
            self.assertEqual({key: value[key] for key in ('phone_source', 'source_trust') if key in value}, fields)
        invalid = [
            {'phone_source': 'PRIVATE_CLIENT_SECRET'}, {'phone_source': True}, {'phone_source': {}},
            {'source_trust': 'authenticated-owner'}, {'source_trust': {}},
            {'phone_source': FRAME, 'source_trust': 'private-configuration'},
            {'phone_source': 'legacy', 'source_trust': 'local-network-registration'},
            {'phone_source': None, 'source_trust': 'local-network-registration'},
        ]
        for fields in invalid:
            with self.subTest(fields=list(fields)):
                self.client.request = Mock(return_value=dict(baseline, **fields))
                result = tool(self.server, 'streamshot_status')['result']
                self.assertTrue(result['isError'])
                self.assertEqual(json.loads(result['content'][0]['text'])['error'], 'invalid_bridge_response')
                self.assertNotIn('PRIVATE', json.dumps(result))

    def test_removed_pairing_tool_is_rejected_without_http_or_phone_grants(self):
        initialize(self.server)
        result = tool(self.server, 'streamshot_pair_phone')
        self.assertEqual(result['error']['code'], -32602)
        self.assertEqual(result['error']['message'], 'unknown_tool')
        self.assertEqual(self.client.calls, [])
        self.factory.assert_not_called()
        self.assertIsNone(self.tools.observation)
        self.assertFalse(self.tools.input_unknown)

    def test_control_requires_local_fresh_frame_and_generates_transport_id(self):
        initialize(self.server)
        reply = tool(self.server, 'streamshot_control', control())
        self.assertTrue(reply['result']['isError'])
        self.assertEqual(self.client.calls, [])
        tool(self.server, 'streamshot_get_screen', identity=11)
        reply = tool(self.server, 'streamshot_control', control(), identity=12)
        self.assertFalse(reply['result']['isError'])
        sent = self.client.calls[-1][2]
        self.assertEqual(sent['v'], 1)
        self.assertEqual(str(uuid.UUID(sent['id'])), sent['id'])
        self.assertEqual(set(sent), {'v', 'id', 'session_id', 'frame_id', 'op', 'button'})
        self.clock.return_value = 131
        self.assertTrue(tool(self.server, 'streamshot_control', control(), identity=13)['result']['isError'])
        self.assertEqual(len([item for item in self.client.calls if item[1] == '/v1/actions']), 1)

    def test_strict_arguments_reject_extra_url_shell_ids_bools_and_ranges(self):
        initialize(self.server)
        bad = [control(url='https://evil.invalid'), control(shell='anything'), control(id=FRAME),
               control(v=1), {'frame_id': FRAME, 'session_id': SESSION, 'op': 'arm'},
               {'frame_id': FRAME, 'session_id': SESSION, 'op': 'move', 'dx': True, 'dy': 0},
               {'frame_id': FRAME, 'session_id': SESSION, 'op': 'move', 'dx': 2049, 'dy': 0},
               {'frame_id': FRAME, 'session_id': SESSION, 'op': 'scroll', 'dy': 17},
               {'frame_id': FRAME, 'session_id': SESSION, 'op': 'key', 'usage': 3, 'modifiers': 0},
               control(button='double')]
        for index, args in enumerate(bad):
            self.assertEqual(tool(self.server, 'streamshot_control', args, 20 + index)['error']['code'], -32602)
        for name in ('streamshot_status', 'streamshot_get_screen', 'streamshot_stop', 'streamshot_reset_control'):
            self.assertEqual(tool(self.server, name, {'url': 'https://evil.invalid'})['error']['code'], -32602)
        self.assertEqual(self.client.calls, [])
        self.factory.assert_not_called()

    def test_unknown_input_not_retried_until_explicit_reset_and_fresh_screen(self):
        initialize(self.server)
        tool(self.server, 'streamshot_get_screen')
        self.client.fail_action = True
        first = tool(self.server, 'streamshot_control', control(), 31)
        self.assertTrue(first['result']['isError'])
        self.assertNotIn('PRIVATE', json.dumps(first))
        self.assertEqual(tool(self.server, 'streamshot_control', control(), 31), first)
        tool(self.server, 'streamshot_control', control(), 32)
        self.assertEqual(len([item for item in self.client.calls if item[1] == '/v1/actions']), 1)
        self.client.fail_action = False
        self.client.next_frame = str(uuid.uuid4())
        tool(self.server, 'streamshot_get_screen', identity=33)
        result = tool(self.server, 'streamshot_control', control(frame_id=self.client.next_frame), 34)
        self.assertTrue(result['result']['isError'])
        self.assertEqual(len([item for item in self.client.calls if item[1] == '/v1/actions']), 1)
        self.assertFalse(tool(self.server, 'streamshot_reset_control', identity=35)['result']['isError'])
        self.assertIsNone(self.tools.observation)
        self.client.next_frame = str(uuid.uuid4())
        tool(self.server, 'streamshot_get_screen', identity=36)
        result = tool(self.server, 'streamshot_control', control(frame_id=self.client.next_frame), 37)
        self.assertFalse(result['result']['isError'])
        self.assertEqual(len([item for item in self.client.calls if item[1] == '/v1/actions']), 2)

    def test_reused_input_id_with_changed_payload_and_cache_limit_fail_closed(self):
        initialize(self.server)
        tool(self.server, 'streamshot_get_screen')
        tool(self.server, 'streamshot_control', control(), 40)
        changed = tool(self.server, 'streamshot_control', control(button='right'), 40)
        self.assertEqual(changed['error']['message'], 'control_request_id_reused')
        with patch.object(mcp, 'MAX_CONTROL_CALLS', 2):
            for identity in range(41, 50):
                tool(self.server, 'streamshot_control', control(), identity)
            self.assertEqual(len(self.server.control_calls), 2)
        self.assertEqual(len([item for item in self.client.calls if item[1] == '/v1/actions']), 2)

    def test_act_expands_ascii_in_order_and_returns_one_fresh_screen_with_small_replay_receipt(self):
        initialize(self.server)
        tool(self.server, 'streamshot_get_screen', identity=100)
        self.client.next_frame = str(uuid.uuid4())
        args = act(actions=[{'op': 'move', 'dx': 30, 'dy': -2}, {'op': 'type', 'text': 'Hi!'},
                            {'op': 'key', 'usage': 40, 'modifiers': 0}])
        reply = tool(self.server, 'streamshot_act', args, 101)
        result = reply['result']
        self.assertFalse(result['isError'])
        summary = json.loads(result['content'][0]['text'])
        self.assertEqual((summary['requested'], summary['acknowledged']), (5, 5))
        self.assertEqual(summary['status'], 'acknowledged_not_visual_confirmation')
        self.assertFalse(summary['retryInput'])
        self.assertEqual(summary['screen_status'], 'captured')
        self.assertEqual(summary['screen']['frame_id'], self.client.next_frame)
        self.assertEqual(base64.b64decode(result['content'][1]['data']), JPEG)
        self.tools.sleep.assert_not_called()
        self.assertEqual(self.client.calls[-1], ('POST', '/v1/screen', {'settle_ms': 250}, True))
        sent = [item[2] for item in self.client.calls if item[1] == '/v1/actions']
        self.assertEqual([item['op'] for item in sent], ['move', 'key', 'key', 'key', 'key'])
        self.assertEqual([(item['usage'], item['modifiers']) for item in sent[1:]],
                         [(11, 2), (12, 0), (30, 2), (40, 0)])
        self.assertEqual(len({item['id'] for item in sent}), 5)
        self.assertTrue(all((item['frame_id'], item['session_id']) == (FRAME, SESSION) for item in sent))
        before = copy.deepcopy(self.client.calls)
        replay = tool(self.server, 'streamshot_act', args, 101)['result']
        cached = json.loads(replay['content'][0]['text'])
        self.assertEqual(cached['results'], summary['results'])
        self.assertTrue(cached['replayed'])
        self.assertTrue(cached['image_omitted_on_replay'])
        self.assertEqual(len(replay['content']), 1)
        self.assertEqual(self.client.calls, before)
        cache = json.dumps(list(self.server.control_calls.values()))
        self.assertNotIn('"type": "image"', cache)
        self.assertNotIn(base64.b64encode(JPEG).decode(), cache)
        self.assertEqual(tool(self.server, 'streamshot_act', act(), 101)['error']['message'], 'control_request_id_reused')
        self.assertEqual(tool(self.server, 'streamshot_control', control(), 101)['error']['message'], 'control_request_id_reused')

    def test_act_validates_the_entire_expanded_plan_before_input_or_capture(self):
        initialize(self.server)
        invalid = [
            act(actions=[]), act(actions=[{'op': 'click', 'button': 'left'}] * 17),
            act(actions=[{'op': 'click', 'button': 'left'}, {'op': 'type', 'text': 'a' * 16}]),
            act(actions=[{'op': 'move', 'dx': 0, 'dy': 0}, {'op': 'key', 'usage': True, 'modifiers': 0}]),
            act(actions=[{'op': 'click', 'button': 'left', 'frame_id': FRAME}]),
            act(actions=[{'op': 'click', 'button': 'left', 'session_id': SESSION}]),
        ]
        invalid += [act(actions=[{'op': 'type', 'text': text}]) for text in ('', 'a' * 17, '你好', 'a\nb', '\t', '\x7f')]
        invalid += [act(capture=1), act(settle_ms=True), act(settle_ms=-1), act(settle_ms=1001),
                    act(settle_ms=1.5), act(url='https://evil.invalid')]
        for identity, args in enumerate(invalid, 110):
            self.assertEqual(tool(self.server, 'streamshot_act', args, identity)['error']['code'], -32602)
        self.assertEqual(self.client.calls, [])
        self.factory.assert_not_called()
        self.assertEqual(self.server.control_calls, {})

    def test_act_accepts_at_most_sixteen_printable_ascii_taps_and_never_implicit_submit(self):
        printable = ''.join(chr(code) for code in range(32, 127))
        for offset in range(0, len(printable), 16):
            keys = mcp.ascii_keys(printable[offset:offset + 16])
            self.assertTrue(all(4 <= item['usage'] <= 56 and item['usage'] not in (40, 43) for item in keys))
        initialize(self.server)
        tool(self.server, 'streamshot_get_screen', identity=130)
        result = tool(self.server, 'streamshot_act', act(actions=[{'op': 'type', 'text': '0123456789abcdef'}],
                                                       capture=False), 131)['result']
        self.assertEqual(json.loads(result['content'][0]['text'])['acknowledged'], 16)
        self.assertIsNone(self.tools.observation)
        self.tools.sleep.assert_not_called()
        self.assertEqual(len([item for item in self.client.calls if item[1] == '/v1/screen']), 1)

    def test_act_stops_when_the_original_observation_ages_out_between_steps(self):
        initialize(self.server)
        tool(self.server, 'streamshot_get_screen', identity=140)
        original = self.client.request
        def age_after_first(method, path, value=None, image=False):
            result = original(method, path, value, image)
            if path == '/v1/actions': self.clock.return_value = 131
            return result
        self.client.request = age_after_first
        args = act(actions=[{'op': 'click', 'button': 'left'}] * 2, capture=False)
        result = tool(self.server, 'streamshot_act', args, 141)['result']
        summary = json.loads(result['content'][0]['text'])
        self.assertTrue(result['isError'])
        self.assertEqual(summary['acknowledged'], 1)
        self.assertEqual(summary['results'][-1]['status'], 'not_sent')
        self.assertEqual(summary['results'][-1]['error'], 'observe_again')
        self.assertEqual(len([item for item in self.client.calls if item[1] == '/v1/actions']), 1)
        self.assertFalse(self.tools.control_reset_required)
        self.assertIsNone(self.tools.observation)
        self.assertEqual(json.loads(tool(self.server, 'streamshot_act', args, 142)['result']['content'][0]['text'])['error'], 'observe_again')

    def test_act_rejects_expired_or_mismatched_initial_observations_without_http(self):
        initialize(self.server)
        tool(self.server, 'streamshot_get_screen', identity=150)
        before = copy.deepcopy(self.client.calls)
        for identity, args in enumerate((act(frame_id=str(uuid.uuid4())), act(session_id=str(uuid.uuid4()))), 151):
            result = tool(self.server, 'streamshot_act', args, identity)['result']
            self.assertEqual(json.loads(result['content'][0]['text'])['error'], 'observe_again')
        self.clock.return_value = 131
        result = tool(self.server, 'streamshot_act', act(), 153)['result']
        self.assertEqual(json.loads(result['content'][0]['text'])['error'], 'observe_again')
        self.assertEqual(self.client.calls, before)

    def test_act_unknown_second_input_stops_the_rest_and_screen_does_not_unlock_control(self):
        initialize(self.server)
        tool(self.server, 'streamshot_get_screen', identity=160)
        original = self.client.request
        actions = 0
        def uncertain_second(method, path, value=None, image=False):
            nonlocal actions
            if path == '/v1/actions':
                actions += 1
                self.client.unknown_action = actions == 2
            return original(method, path, value, image)
        self.client.request = uncertain_second
        self.client.next_frame = str(uuid.uuid4())
        args = act(actions=[{'op': 'click', 'button': 'left'}] * 3, settle_ms=0)
        result = tool(self.server, 'streamshot_act', args, 161)['result']
        summary = json.loads(result['content'][0]['text'])
        self.assertEqual((summary['requested'], summary['acknowledged'], len(summary['results'])), (3, 1, 2))
        self.assertEqual(summary['results'][-1]['error'], 'control_reset_required')
        self.assertEqual(summary['screen_status'], 'captured')
        self.assertEqual(result['content'][1]['type'], 'image')
        self.assertTrue(self.tools.control_reset_required)
        self.assertNotIn('PRIVATE', json.dumps(result))
        before = copy.deepcopy(self.client.calls)
        tool(self.server, 'streamshot_act', args, 161)
        self.assertEqual(self.client.calls, before)
        blocked = tool(self.server, 'streamshot_act', act(frame_id=self.client.next_frame), 162)['result']
        self.assertEqual(json.loads(blocked['content'][0]['text'])['error'], 'control_reset_required')
        self.assertEqual(actions, 2)

    def test_act_transport_unknown_cannot_replay_or_continue(self):
        initialize(self.server)
        tool(self.server, 'streamshot_get_screen', identity=170)
        original = self.client.request
        def lose_ack(method, path, value=None, image=False):
            if path == '/v1/actions':
                self.client.calls.append((method, path, copy.deepcopy(value), image))
                raise mcp.SafeFailure('bridge_request_outcome_unknown')
            return original(method, path, value, image)
        self.client.request = lose_ack
        args = act(actions=[{'op': 'key', 'usage': 4, 'modifiers': 0}] * 2, capture=False)
        first = tool(self.server, 'streamshot_act', args, 171)['result']
        summary = json.loads(first['content'][0]['text'])
        self.assertEqual((summary['acknowledged'], len(summary['results'])), (0, 1))
        self.assertEqual(summary['results'][0]['status'], 'outcome_unknown')
        self.assertTrue(self.tools.control_reset_required)
        tool(self.server, 'streamshot_act', args, 171)
        tool(self.server, 'streamshot_act', args, 172)
        self.assertEqual(len([item for item in self.client.calls if item[1] == '/v1/actions']), 1)

    def test_act_screen_timeout_keeps_ack_receipt_and_does_not_mark_input_unknown(self):
        initialize(self.server)
        tool(self.server, 'streamshot_get_screen', identity=180)
        original = self.client.request
        def missing_screen(method, path, value=None, image=False):
            if path == '/v1/screen':
                self.client.calls.append((method, path, value, image))
                raise mcp.SafeFailure('screen_not_received')
            return original(method, path, value, image)
        self.client.request = missing_screen
        args = act(actions=[{'op': 'type', 'text': 'ok'}], settle_ms=1000)
        first = tool(self.server, 'streamshot_act', args, 181)['result']
        summary = json.loads(first['content'][0]['text'])
        self.assertTrue(first['isError'])
        self.assertTrue(summary['ok'])
        self.assertEqual(summary['acknowledged'], 2)
        self.assertEqual(summary['screen_status'], 'unavailable')
        self.assertEqual(summary['screen_error'], 'screen_not_received')
        self.assertFalse(summary['retryInput'])
        self.assertFalse(self.tools.control_reset_required)
        self.assertFalse(self.tools.input_unknown)
        self.assertIsNone(self.tools.observation)
        self.tools.sleep.assert_not_called()
        self.assertEqual(self.client.calls[-1][2], {'settle_ms': 1000})
        before = copy.deepcopy(self.client.calls)
        tool(self.server, 'streamshot_act', args, 181)
        self.assertEqual(self.client.calls, before)
        self.assertTrue(tool(self.server, 'streamshot_act', args, 182)['result']['isError'])
        self.assertEqual(len([item for item in self.client.calls if item[1] == '/v1/actions']), 2)

    def test_act_requests_ticket_immediately_and_reports_measured_bounded_times(self):
        initialize(self.server)
        tool(self.server, 'streamshot_get_screen', identity=190)
        self.tools.timer = Mock(side_effect=[1, 2, 2, 4, 5])
        result = tool(self.server, 'streamshot_act', act(settle_ms=500), 191)['result']
        summary = json.loads(result['content'][0]['text'])
        self.assertEqual({key: summary[key] for key in ('input_ms', 'capture_ms', 'total_ms')},
                         dict(input_ms=1000, capture_ms=2000, total_ms=4000))
        self.assertEqual(summary['screen']['capture_wait_ms'], 500)
        self.assertEqual([item[1] for item in self.client.calls], ['/v1/screen', '/v1/actions', '/v1/screen'])
        self.assertEqual(self.client.calls[-1][2], {'settle_ms': 500})
        self.tools.sleep.assert_not_called()
        before = copy.deepcopy(self.client.calls)
        replay = tool(self.server, 'streamshot_act', act(settle_ms=500), 191)['result']
        self.assertEqual(json.loads(replay['content'][0]['text'])['total_ms'], 4000)
        self.assertEqual(self.client.calls, before)
        self.assertEqual(self.tools._elapsed_ms(0, 1000000), mcp.MAX_TIMING_MS)
        self.assertEqual(self.tools._elapsed_ms(2, 1), 0)

    def test_new_capture_metrics_anchor_freshness_to_request_not_upload_completion(self):
        initialize(self.server)
        original = self.client.request
        def slow_capture(method, path, value=None, image=False):
            metadata, jpeg = original(method, path, value, image)
            self.clock.return_value = 102
            return dict(metadata, capture_wait_ms=900, encode_ms=50, source_callback_age_ms=100,
                        observation_age_ms=1000), jpeg
        self.client.request = slow_capture
        screen = tool(self.server, 'streamshot_get_screen', identity=195)['result']
        self.assertFalse(screen['isError'])
        self.assertEqual(self.tools.observed_at, 100)
        self.clock.return_value = 131
        result = tool(self.server, 'streamshot_act', act(capture=False), 196)['result']
        self.assertEqual(json.loads(result['content'][0]['text'])['error'], 'observe_again')
        self.assertEqual(len(self.client.calls), 1)

    def test_nonzero_settle_cannot_claim_success_with_legacy_metadata(self):
        initialize(self.server)
        tool(self.server, 'streamshot_get_screen', identity=197)
        original = self.client.request
        def legacy_capture(method, path, value=None, image=False):
            result = original(method, path, value, image)
            return (dict(METADATA), JPEG) if path == '/v1/screen' else result
        self.client.request = legacy_capture
        result = tool(self.server, 'streamshot_act', act(), 198)['result']
        summary = json.loads(result['content'][0]['text'])
        self.assertTrue(summary['ok'])
        self.assertEqual(summary['acknowledged'], 1)
        self.assertEqual(summary['screen_error'], 'invalid_frame_metadata')
        self.assertFalse(self.tools.control_reset_required)
        self.assertIsNone(self.tools.observation)

    def test_stop_has_no_frame_requirement_and_old_session_cannot_resume(self):
        initialize(self.server)
        tool(self.server, 'streamshot_get_screen')
        reply = tool(self.server, 'streamshot_stop', identity=50)
        self.assertFalse(reply['result']['isError'])
        self.assertIsNone(self.tools.observation)
        self.assertEqual(self.client.calls[-1], ('POST', '/v1/stop', {}, False))
        # Even a broken mock server returning the old session cannot revive input.
        tool(self.server, 'streamshot_get_screen', identity=51)
        self.assertTrue(tool(self.server, 'streamshot_control', control(), 52)['result']['isError'])

    def test_server_unknown_body_is_not_forwarded(self):
        initialize(self.server)
        tool(self.server, 'streamshot_get_screen')
        self.client.unknown_action = True
        result = tool(self.server, 'streamshot_control', control())
        self.assertTrue(result['result']['isError'])
        self.assertNotIn('PRIVATE_SERVER', json.dumps(result))

    def test_serial_unknown_requires_desktop_reset_even_after_a_new_image(self):
        initialize(self.server)
        tool(self.server, 'streamshot_get_screen')
        self.client.unknown_action = True
        self.client.action_error = 'controller_outcome_unknown'
        result = tool(self.server, 'streamshot_control', control(), 60)['result']
        self.assertEqual(json.loads(result['content'][0]['text'])['error'], 'control_reset_required')
        self.client.unknown_action = False
        self.client.next_frame = str(uuid.uuid4())
        tool(self.server, 'streamshot_get_screen', identity=61)
        result = tool(self.server, 'streamshot_control', control(frame_id=self.client.next_frame), 62)['result']
        self.assertTrue(result['isError'])
        self.assertEqual(len([item for item in self.client.calls if item[1] == '/v1/actions']), 1)

    def test_reset_is_screen_preserving_requires_new_observation_and_is_rpc_idempotent(self):
        initialize(self.server)
        tool(self.server, 'streamshot_get_screen')
        reset = tool(self.server, 'streamshot_reset_control', identity=70)
        self.assertFalse(reset['result']['isError'])
        self.assertTrue(self.client.active)
        self.assertIsNone(self.tools.observation)
        method, path, value, image = self.client.calls[-1]
        self.assertEqual((method, path, image), ('POST', '/v1/control/reset', False))
        self.assertEqual(set(value), {'v', 'id', 'session_id'})
        self.assertEqual(value['session_id'], SESSION)
        self.assertEqual(str(uuid.UUID(value['id'])), value['id'])
        self.assertTrue(tool(self.server, 'streamshot_control', control(), 71)['result']['isError'])
        self.client.next_frame = str(uuid.uuid4())
        tool(self.server, 'streamshot_get_screen', identity=72)
        args = control(frame_id=self.client.next_frame)
        self.assertFalse(tool(self.server, 'streamshot_control', args, 73)['result']['isError'])
        before = copy.deepcopy(self.client.calls)
        self.assertEqual(tool(self.server, 'streamshot_reset_control', identity=70), reset)
        self.assertEqual(self.client.calls, before)
        changed = tool(self.server, 'streamshot_control', args, 70)
        self.assertEqual(changed['error']['message'], 'control_request_id_reused')

    def test_uncertain_reset_remains_latched_and_same_rpc_does_not_repeat(self):
        initialize(self.server)
        original = self.client.request
        def fail(method, path, value=None, image=False):
            if path == '/v1/control/reset':
                self.client.calls.append((method, path, value, image))
                raise RuntimeError('PRIVATE_RESET_DIAGNOSTIC')
            return original(method, path, value, image)
        self.client.request = fail
        first = tool(self.server, 'streamshot_reset_control', identity=80)
        self.assertTrue(first['result']['isError'])
        self.assertNotIn('PRIVATE', json.dumps(first))
        self.assertEqual(tool(self.server, 'streamshot_reset_control', identity=80), first)
        tool(self.server, 'streamshot_get_screen', identity=81)
        self.assertTrue(tool(self.server, 'streamshot_control', control(), 82)['result']['isError'])
        self.assertEqual(len([x for x in self.client.calls if x[1] == '/v1/control/reset']), 1)

    def test_stdio_only_protocol_bad_lines_notifications_and_batch(self):
        messages = [request('initialize', params={'protocolVersion': '2024-11-05', 'capabilities': {},
                    'clientInfo': {'name': 'fixture', 'version': '1'}}),
                    {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                    request('tools/list', 2),
                    [request('ping', 3), {'jsonrpc': '2.0', 'method': 'notifications/cancelled'}],
                    request('tools/call', 4, {'name': 'streamshot_get_screen', 'arguments': {}})]
        source = io.StringIO('\n'.join(json.dumps(item) for item in messages) + '\nPRIVATE_INVALID_JSON\n{"x":NaN}\n')
        output = io.StringIO()
        mcp.run_mcp(Path('/unused-fixture'), source, output, self.factory, runtime_factory=FakeRuntime)
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(replies), 6)
        self.assertEqual(replies[2], [{'jsonrpc': '2.0', 'id': 3, 'result': {}}])
        self.assertIn('image', replies[3]['result']['content'][0]['type'])
        self.assertNotIn('PRIVATE_INVALID_JSON', output.getvalue())
        self.assertTrue(all(isinstance(item, list) or item['jsonrpc'] == '2.0' for item in replies))

    def test_missing_configuration_is_lazy_and_redacted_tool_error(self):
        self.factory.side_effect = RuntimeError('PRIVATE_PATH_AND_TOKEN')
        initialize(self.server)
        self.server.handle(request('tools/list'))
        self.factory.assert_not_called()
        result = tool(self.server, 'streamshot_status')
        self.assertTrue(result['result']['isError'])
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_core_cli_stdio_entry_handshake_is_offline_and_has_no_banner(self):
        commands = [request('initialize', params={'protocolVersion': '2025-03-26', 'capabilities': {},
                    'clientInfo': {'name': 'offline-smoke', 'version': '1'}}),
                    {'jsonrpc': '2.0', 'method': 'notifications/initialized'}, request('tools/list', 2)]
        setup = ('import sys, runpy; sys.path.insert(0, ' + repr(str(Path(mcp.__file__).parent)) + '); '
                 'import streamshot_runtime; '
                 'streamshot_runtime.Runtime = type("OfflineRuntime", (), {"__init__": lambda *a, **k: None, "start": lambda s: None, "close": lambda s: None}); '
                 'sys.argv = [' + repr(str(Path(mcp.bridge.__file__))) + ', "--state", "/unused-offline-fixture", "mcp"]; '
                 'runpy.run_path(' + repr(str(Path(mcp.bridge.__file__))) + ', run_name="__main__")')
        result = subprocess.run([sys.executable, '-c', setup],
            input='\n'.join(json.dumps(item) for item in commands) + '\n', capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(replies), 2)
        self.assertTrue(all(item['jsonrpc'] == '2.0' for item in replies))
        self.assertEqual(len(replies[1]['result']['tools']), 6)
        self.assertEqual(result.stderr, '')

    def test_runtime_starts_and_closes_with_stdio_and_explicit_serial_only(self):
        lifetime = FakeRuntime(Path('/fixture'))
        factory = Mock(return_value=lifetime)
        output = io.StringIO()
        mcp.run_mcp(Path('/fixture'), io.StringIO(''), output,
                    self.factory, runtime_factory=factory, serial_port='/dev/cu.usbmodemOWNER')
        factory.assert_called_once_with(Path('/fixture'), serial_port='/dev/cu.usbmodemOWNER')
        self.assertTrue(lifetime.started); self.assertTrue(lifetime.closed)
        self.assertEqual(output.getvalue(), '')
        self.factory.assert_not_called()

    def test_runtime_failure_is_returned_inside_mcp_not_as_banner_or_exception(self):
        lifetime = FakeRuntime(Path('/fixture'))
        lifetime.start = Mock(side_effect=mcp.runtime.RuntimeFailure('runtime_port_in_use'))
        commands = [request('initialize', params={'protocolVersion': '2025-03-26', 'capabilities': {},
                     'clientInfo': {'name': 'fixture', 'version': '1'}}),
                    {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                    request('tools/call', 4, {'name': 'streamshot_status', 'arguments': {}})]
        source = io.StringIO('\n'.join(json.dumps(c) for c in commands) + '\n')
        output = io.StringIO()
        mcp.run_mcp(Path('/fixture'), source, output, self.factory, runtime_factory=lambda *a, **k: lifetime)
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(replies), 2)
        self.assertEqual(json.loads(replies[-1]['result']['content'][0]['text'])['error'], 'runtime_port_in_use')
        self.assertTrue(lifetime.closed)
        self.factory.assert_not_called()

    def test_sigterm_handler_uses_graceful_runtime_cleanup_and_restores_handler(self):
        lifetime = FakeRuntime(Path('/fixture'))
        previous = mcp.signal.getsignal(mcp.signal.SIGTERM)
        source = Mock()
        def interrupt(*args):
            mcp.signal.getsignal(mcp.signal.SIGTERM)(mcp.signal.SIGTERM, None)
        source.readline.side_effect = interrupt
        mcp.run_mcp(Path('/fixture'), source, io.StringIO(), self.factory,
                    runtime_factory=lambda *a, **k: lifetime)
        self.assertTrue(lifetime.closed)
        self.assertEqual(mcp.signal.getsignal(mcp.signal.SIGTERM), previous)


class TransportTests(unittest.TestCase):
    def client(self, response=None, error=None):
        client = object.__new__(mcp.Client)
        client.config = {'baseURL': 'https://127.0.0.1:8765', 'agentToken': 'PRIVATE_TOKEN'}
        client.opener = Mock()
        if error is not None:
            client.opener.open.side_effect = error
        else:
            client.opener.open.return_value = response
        return client

    def response(self, path, data, content_type='application/json', headers=None):
        response = MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = 'https://127.0.0.1:8765' + path
        response.headers = Message()
        response.headers['Content-Type'] = content_type
        for key, value in (headers or {}).items():
            response.headers[key] = str(value)
        response.read.return_value = data
        return response

    def test_private_tls_store_and_proxy_redirect_handlers(self):
        context = Mock()
        config = {'host': '127.0.0.1', 'port': 8765,
                  'baseURL': 'https://127.0.0.1:8765', 'agentToken': 'PRIVATE_TOKEN'}
        with patch.object(mcp.bridge, 'configuration', return_value=config), \
             patch.object(mcp.bridge, 'private_file', return_value=Path('/synthetic/certificate.pem')), \
             patch.object(mcp.ssl, 'SSLContext', return_value=context) as factory, \
             patch.object(mcp.urllib.request, 'build_opener') as build:
            mcp.Client(Path('/synthetic'))
        factory.assert_called_once_with(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations.assert_called_once_with(cafile='/synthetic/certificate.pem')
        context.load_default_certs.assert_not_called()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        handlers = build.call_args.args
        self.assertIsInstance(handlers[0], mcp.urllib.request.ProxyHandler)
        self.assertEqual(handlers[0].proxies, {})
        self.assertIsInstance(handlers[-1], mcp.NoRedirect)
        self.assertIsNone(handlers[-1].redirect_request(None, None, 302, '', {}, 'https://evil.invalid'))

    def test_screen_header_case_jpeg_and_fixed_endpoint_timeout(self):
        headers = {'x-streamshot-' + key.replace('_', '-'): value for key, value in METADATA.items()}
        response = self.response('/v1/screen', JPEG, 'image/jpeg', headers)
        client = self.client(response)
        metadata, data = client.request('POST', '/v1/screen', {}, image=True)
        self.assertEqual(metadata, METADATA)
        self.assertEqual(data, JPEG)
        sent = client.opener.open.call_args.args[0]
        self.assertEqual(sent.full_url, 'https://127.0.0.1:8765/v1/screen')
        self.assertEqual(sent.data, b'{}')
        self.assertEqual(client.opener.open.call_args.kwargs, {'timeout': 12})
        self.assertNotIn('PRIVATE_TOKEN', sent.full_url)

    def test_capture_timing_metadata_is_all_or_none_and_strictly_bounded(self):
        headers = {'X-StreamShot-' + key.replace('_', '-'): str(value) for key, value in METADATA.items()}
        metrics = {'capture-wait-ms': '250', 'encode-ms': '12', 'source-callback-age-ms': '80', 'observation-age-ms': '300'}
        headers.update({'X-StreamShot-' + key: value for key, value in metrics.items()})
        self.assertEqual(mcp.header_metadata(headers, JPEG)['source_callback_age_ms'], 80)
        for value in ('-1', '5001', '1.5', 'True', '+1'):
            with self.assertRaisesRegex(mcp.SafeFailure, '^invalid_frame_metadata$'):
                mcp.header_metadata(dict(headers, **{'X-StreamShot-encode-ms': value}), JPEG)
        partial = dict(headers)
        partial.pop('X-StreamShot-observation-age-ms')
        with self.assertRaisesRegex(mcp.SafeFailure, '^invalid_frame_metadata$'):
            mcp.header_metadata(partial, JPEG)

    def test_http_errors_do_not_read_body_or_retry(self):
        body = Mock()
        error = urllib.error.HTTPError('https://synthetic.invalid', 401, 'PRIVATE_HTTP_BODY', {}, body)
        client = self.client(error=error)
        with self.assertRaisesRegex(mcp.SafeFailure, '^bridge_unauthorized$'):
            client.request('GET', '/v1/status')
        body.read.assert_not_called()
        self.assertEqual(client.opener.open.call_count, 1)
        client.opener.open.side_effect = TimeoutError('PRIVATE_TOKEN')
        with self.assertRaisesRegex(mcp.SafeFailure, '^bridge_request_outcome_unknown$'):
            client.request('POST', '/v1/actions', mcp.control_payload(control()))
        self.assertEqual(client.opener.open.call_count, 2)

    def test_pairing_endpoints_are_unavailable_to_mcp_http_client(self):
        client = self.client()
        for path in ('/v1/pairing/open', '/v1/pairing/start', '/v1/pairing/finish'):
            with self.assertRaisesRegex(mcp.SafeFailure, '^request_not_allowed$'):
                client.request('POST', path, {})
        client.opener.open.assert_not_called()

    def test_redirects_limits_malformed_frames_and_other_paths_rejected(self):
        response = self.response('/v1/status', b'{}')
        client = self.client(response)
        response.geturl.return_value = 'https://evil.invalid/v1/status'
        with self.assertRaisesRegex(mcp.SafeFailure, '^bridge_redirect_rejected$'):
            client.request('GET', '/v1/status')
        with self.assertRaisesRegex(mcp.SafeFailure, '^request_not_allowed$'):
            client.request('POST', '/v1/session', {})
        response = self.response('/v1/screen', b'x' * (mcp.bridge.MAX_IMAGE + 1), 'image/jpeg')
        client = self.client(response)
        with self.assertRaisesRegex(mcp.SafeFailure, '^bridge_response_too_large$'):
            client.request('POST', '/v1/screen', {}, image=True)
        headers = {'x-streamshot-' + key.replace('_', '-'): str(value) for key, value in METADATA.items()}
        headers['x-streamshot-width'] = '99'
        with self.assertRaisesRegex(mcp.SafeFailure, '^invalid_frame_metadata$'):
            mcp.header_metadata(headers, JPEG)
        headers['X-StreamShot-Width'] = '2'
        with self.assertRaisesRegex(mcp.SafeFailure, '^invalid_frame_metadata$'):
            mcp.header_metadata(headers, JPEG)


class StableOriginTLSTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve() / 'state'
        mcp.bridge.init_state(self.directory, '127.0.0.1', 8765, origin_host='synthetic-mac.local')
        self.config = mcp.bridge.configuration(self.directory)
        self.session = mcp.bridge.Session()
        self.server = mcp.bridge.Server(('127.0.0.1', 0), self.session,
                                      self.config['uploadToken'], self.config['agentToken'], '')
        port = self.server.server_address[1]
        self.authority = 'synthetic-mac.local:' + str(port)
        self.server.authority = self.authority
        self.config.update(port=port, baseURL='https://' + self.authority)
        (self.directory / 'bridge.json').write_text(json.dumps(self.config))
        self.sni = []
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.directory / 'certificate.pem', self.directory / 'private-key.pem')
        context.set_servername_callback(lambda socket, name, context: self.sni.append(name))
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True,
                                                 do_handshake_on_connect=False)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(3)

    def numeric_addresses_only(self, host, *args, **kwargs):
        # No request may depend on DNS for the advertised .local hostname.
        self.assertEqual(host, '127.0.0.1')
        return self.real_getaddrinfo(host, *args, **kwargs)

    def test_mcp_status_uses_configured_ip_and_verifies_stable_dns_origin(self):
        self.real_getaddrinfo = socket.getaddrinfo
        with patch.object(socket, 'getaddrinfo', side_effect=self.numeric_addresses_only) as addresses:
            server = mcp.MCP(mcp.Tools(self.directory))
            initialize(server)
            result = tool(server, 'streamshot_status')['result']
        self.assertFalse(result['isError'])
        self.assertFalse(json.loads(result['content'][0]['text'])['active'])
        self.assertGreater(addresses.call_count, 0)
        self.assertEqual(self.sni, ['synthetic-mac.local'])
        self.assertEqual(server.tools.client.config['baseURL'], 'https://' + self.authority)
        self.assertIsNone(self.session.pending)
        self.assertIsNone(self.session.frame)

    def test_trusted_leaf_with_wrong_origin_hostname_fails_tls_verification(self):
        changed = dict(self.config, originHost='uncovered-mac.local',
                       baseURL='https://uncovered-mac.local:' + str(self.config['port']))
        (self.directory / 'bridge.json').write_text(json.dumps(changed))
        self.real_getaddrinfo = socket.getaddrinfo
        with patch.object(socket, 'getaddrinfo', side_effect=self.numeric_addresses_only):
            client = mcp.Client(self.directory)
            with self.assertRaisesRegex(mcp.SafeFailure, '^bridge_unavailable$'):
                client.request('GET', '/v1/status')
        self.assertEqual(self.sni, ['uncovered-mac.local'])
        self.assertIsNone(self.session.frame)

    def test_act_over_real_tls_uses_one_lease_and_stops_if_screen_session_changes(self):
        for change_session in (False, True):
            with self.subTest(change_session=change_session):
                controller = Mock()
                controller.status.return_value = {'available': True, 'armed': False}
                self.session = mcp.bridge.Session(controller)
                self.server.session = self.session
                old, new = str(uuid.uuid4()), str(uuid.uuid4())
                self.session.session(dict(v=1, session_id=old, state='start'), principal='legacy')
                performed = []
                def perform(lease, identity, operation, values):
                    performed.append((lease, identity, operation, values))
                    if change_session and len(performed) == 1:
                        self.session.session(dict(v=1, session_id=old, state='end'), principal='legacy')
                        self.session.session(dict(v=1, session_id=new, state='start'), principal='legacy')
                controller.perform.side_effect = perform
                errors = []
                def upload_one():
                    try:
                        with self.session.condition:
                            if not self.session.condition.wait_for(lambda: self.session.pending is not None, 2):
                                raise AssertionError('synthetic capture request missing')
                            identity, pending = self.session.identity, self.session.pending['id']
                            self.session.accept_frame(dict(session=identity, request=pending, sequence=str(self.session.sequence + 1),
                                width='2', height='3', orientation='1', **{'geometry-epoch': '1'}), JPEG, principal='legacy')
                    except Exception as error:
                        errors.append(error)
                server = mcp.MCP(mcp.Tools(self.directory))
                initialize(server)
                uploader = threading.Thread(target=upload_one, daemon=True)
                uploader.start()
                first = tool(server, 'streamshot_get_screen', identity=200)['result']
                uploader.join(3)
                self.assertEqual(errors, [])
                metadata = json.loads(first['content'][1]['text'])
                uploader = threading.Thread(target=upload_one, daemon=True)
                uploader.start()
                args = act(session_id=old, frame_id=metadata['frame_id'], settle_ms=0,
                           actions=[{'op': 'key', 'usage': 4, 'modifiers': 0}, {'op': 'click', 'button': 'left'}])
                reply = tool(server, 'streamshot_act', args, 201)['result']
                uploader.join(3)
                self.assertFalse(uploader.is_alive())
                self.assertEqual(errors, [])
                summary = json.loads(reply['content'][0]['text'])
                self.assertEqual(summary['acknowledged'], 1 if change_session else 2)
                self.assertEqual(summary['screen_status'], 'session_changed' if change_session else 'captured')
                self.assertEqual(summary['screen']['session_id'], new if change_session else old)
                self.assertEqual(base64.b64decode(reply['content'][1]['data']), JPEG)
                self.assertEqual(len(performed), 1 if change_session else 2)
                self.assertEqual(len({item[0] for item in performed}), 1)
                self.assertNotEqual(performed[0][0], old)
                self.assertEqual(self.session.allow_control, not change_session)
                self.assertEqual(server.tools.control_reset_required, change_session)
                before = len(self.sni)
                replay = tool(server, 'streamshot_act', args, 201)['result']
                self.assertEqual(len(self.sni), before)
                self.assertEqual(len(replay['content']), 1)


if __name__ == '__main__':
    unittest.main()
