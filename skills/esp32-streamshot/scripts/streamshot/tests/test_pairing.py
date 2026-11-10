"""Synthetic first-use trust and loopback TLS; no real phone, LAN or HID.

Trust is an explicit phone-side decision. The public identity lookup is read
only; each confirmed registration gets its own phone-only bearer and principal.
"""
import base64
import concurrent.futures
import hashlib
import http.client
import json
from pathlib import Path
import socket
import ssl
import stat
import sys
import tempfile
import threading
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import streamshot_bridge as bridge
from streamshot_pairing import (MAX_CLIENTS, NEW_PER_WINDOW, UNUSED_SECONDS,
                                PairingFailure, PairingService)


# Every value in this module is synthetic, including the legacy server secret.
PHONE = {'version': 1, 'name': 'Synthetic computer',
         'baseURL': 'https://pairing-fixture.local:8765',
         'uploadToken': 'a' * 64, 'certificateSHA256': 'b' * 64}
JPEG = base64.b64decode('/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAA0JCgsKCA0LCgsODg0PEyAVExISEyccHhcgLikxMC4pLSwzOko+MzZGNywtQFdBRkxOUlNSMj5aYVpQYEpRUk//2wBDAQ4ODhMREyYVFSZPNS01T09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT0//wAARCAADAAIDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAb/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QAFQEBAQAAAAAAAAAAAAAAAAAABAX/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIRAxEAPwCfAUAX/9k=')


class Clock:
    def __init__(self): self.now = 1000
    def __call__(self): return self.now


def trust_request(phone=PHONE, **changes):
    value = {'v': 2, 'client_id': str(uuid.uuid4()), 'request_id': str(uuid.uuid4()),
             'client_secret': 'c' * 64, 'certificateSHA256': phone['certificateSHA256']}
    return dict(value, **changes)


def start_request(identity=None, control=False):
    return {'v': 1, 'session_id': identity or str(uuid.uuid4()),
            'state': 'start', 'allow_control': control}


def frame_headers(identity, request):
    return {'session': identity, 'request': request, 'sequence': '1', 'width': '2',
            'height': '3', 'orientation': '1', 'geometry-epoch': '1'}


class PairingServiceChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve() / 'state'
        self.directory.mkdir(mode=0o700)
        self.clock = Clock()
        self.available = True
        self.service = self.make_service()

    def make_service(self):
        value = PairingService(PHONE, self.directory, clock=self.clock,
                               available=lambda: self.available)
        self.addCleanup(value.close)
        return value

    def failure(self, operation, *args, status=None):
        with self.assertRaises(PairingFailure) as caught:
            operation(*args)
        if status is not None: self.assertEqual(caught.exception.status, status)
        self.assertNotIn(PHONE['uploadToken'], str(caught.exception))
        self.assertNotIn('c' * 64, str(caught.exception))
        return caught.exception

    def register(self, request=None):
        request = request or trust_request()
        answer = self.service.trust(request)
        self.assertEqual(set(answer), {'v', 'client_id', 'request_id', 'configuration'})
        self.assertEqual((answer['v'], answer['client_id'], answer['request_id']),
                         (2, request['client_id'], request['request_id']))
        configuration = answer['configuration']
        self.assertEqual(set(configuration), set(PHONE))
        self.assertEqual({k: v for k, v in configuration.items() if k != 'uploadToken'},
                         {k: v for k, v in PHONE.items() if k != 'uploadToken'})
        self.assertRegex(configuration['uploadToken'], r'^[0-9a-f]{64}$')
        self.assertNotEqual(configuration['uploadToken'], PHONE['uploadToken'])
        self.assertNotIn(PHONE['uploadToken'], json.dumps(answer))
        self.assertNotIn(request['client_secret'], json.dumps(answer))
        return request, configuration['uploadToken']

    def test_identity_and_cancel_before_trust_create_no_grant_or_permission(self):
        expected = {'v': 2, **{k: PHONE[k] for k in ('name', 'baseURL', 'certificateSHA256')}}
        for _ in range(NEW_PER_WINDOW + 2):
            self.assertEqual(self.service.identity({'v': 2}), expected)
        self.assertFalse((self.directory / 'phones.json').exists())
        self.assertIsNone(self.service.authenticate('0' * 64))
        self.available = False
        self.assertEqual(self.service.identity({'v': 2}), expected)
        self.assertFalse((self.directory / 'phones.json').exists())

    def test_distinct_phone_tokens_and_opaque_principals_not_legacy_secret(self):
        first, first_token = self.register()
        second, second_token = self.register()
        self.assertNotEqual(first_token, second_token)
        first_owner = self.service.authenticate(first_token)
        second_owner = self.service.authenticate(second_token)
        self.assertIsNotNone(first_owner)
        self.assertIsNotNone(second_owner)
        self.assertNotEqual(first_owner, second_owner)
        self.assertNotIn(first_owner, (first['client_id'], first['request_id']))
        self.assertNotIn(second_owner, (second['client_id'], second['request_id']))
        self.assertIsNone(self.service.authenticate(PHONE['uploadToken']))
        for invalid in (None, '', '0' * 63, 'G' * 64, 'A' * 64, '0' * 64):
            self.assertIsNone(self.service.authenticate(invalid))

    def test_exact_secret_retry_recovers_only_its_own_token_including_after_restart(self):
        request, token = self.register()
        owner = self.service.authenticate(token)
        self.assertEqual(self.service.trust(request)['configuration']['uploadToken'], token)
        restarted = self.make_service()
        self.assertEqual(restarted.trust(request)['configuration']['uploadToken'], token)
        self.assertEqual(restarted.authenticate(token), owner)

    def test_concurrent_exact_retries_create_one_grant_and_one_principal(self):
        request = trust_request()
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            answers = list(pool.map(self.service.trust, [request] * 8))
        tokens = {answer['configuration']['uploadToken'] for answer in answers}
        self.assertEqual(len(tokens), 1)
        self.assertIsNotNone(self.service.authenticate(tokens.pop()))
        self.assertEqual(len(json.loads((self.directory / 'phones.json').read_text())['grants']), 1)

    def test_public_ids_cannot_retrieve_overwrite_or_rotate_existing_grant(self):
        request, token = self.register()
        owner = self.service.authenticate(token)
        before = (self.directory / 'phones.json').read_bytes()
        for changed in ({'client_secret': 'd' * 64}, {'request_id': str(uuid.uuid4())},
                        {'client_id': str(uuid.uuid4())}):
            with self.subTest(change=list(changed)):
                self.failure(self.service.trust, dict(request, **changed), status=409)
                self.assertEqual((self.directory / 'phones.json').read_bytes(), before)
                self.assertEqual(self.service.authenticate(token), owner)
        self.assertEqual(self.service.trust(request)['configuration']['uploadToken'], token)

    def test_registry_contains_only_hashes_metadata_and_survives_restart(self):
        request, token = self.register()
        path = self.directory / 'phones.json'
        content = path.read_text()
        for secret in (PHONE['uploadToken'], request['client_secret'], token):
            self.assertNotIn(secret, content)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        # Authenticating the phone must persist its used status, not just RAM.
        principal = self.service.authenticate(token)
        self.assertEqual(self.make_service().authenticate(token), principal)
        self.clock.now += UNUSED_SECONDS * 10
        self.assertEqual(self.make_service().authenticate(token), principal)

    def test_unclaimed_grant_expires_at_five_minutes_and_releases_slot(self):
        _, token = self.register()
        self.clock.now += UNUSED_SECONDS
        self.assertIsNone(self.service.authenticate(token))
        restarted = self.make_service()
        self.assertIsNone(restarted.authenticate(token))
        self.assertEqual(json.loads((self.directory / 'phones.json').read_text())['grants'], [])
        _, replacement = self.register()
        self.assertIsNotNone(self.service.authenticate(replacement))

    def test_new_registration_limit_is_global_and_retry_does_not_consume_it(self):
        entries = [self.register() for _ in range(NEW_PER_WINDOW)]
        for request, token in entries:
            self.assertEqual(self.service.trust(request)['configuration']['uploadToken'], token)
        self.failure(self.service.trust, trust_request(), status=429)
        self.clock.now += 60
        self.register()

    def test_eight_used_clients_are_not_evicted_by_anonymous_registration(self):
        owners = []
        for index in range(MAX_CLIENTS):
            if index and index % NEW_PER_WINDOW == 0: self.clock.now += 60
            _, token = self.register()
            owners.append((token, self.service.authenticate(token)))
        self.clock.now += 60
        self.failure(self.service.trust, trust_request(), status=429)
        self.assertEqual(len(json.loads((self.directory / 'phones.json').read_text())['grants']), MAX_CLIENTS)
        for token, owner in owners: self.assertEqual(self.service.authenticate(token), owner)

    def test_active_session_blocks_new_trust_and_retry_but_not_identity(self):
        request, token = self.register()
        self.available = False
        self.failure(self.service.trust, request, status=409)
        self.failure(self.service.trust, trust_request(), status=409)
        self.assertEqual(self.service.identity({'v': 2})['name'], PHONE['name'])
        self.available = True
        self.assertEqual(self.service.trust(request)['configuration']['uploadToken'], token)

    def test_wrong_certificate_and_malformed_requests_create_no_grant(self):
        request = trust_request()
        for changed in ({'v': True}, {'v': 2.0}, {'v': 1}, {'client_id': 'not-a-uuid'},
                        {'client_id': 'AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA'}, {'request_id': 'not-a-uuid'},
                        {'client_secret': 'c' * 63}, {'client_secret': 'C' * 64},
                        {'certificateSHA256': '0' * 64}, {'allow_control': True},
                        {'agentToken': 'd' * 64}):
            with self.subTest(change=list(changed)):
                self.failure(self.service.trust, dict(request, **changed))
        for value in ({'v': True}, {'v': 1}, {'v': 2, 'client_secret': 'c' * 64}):
            self.failure(self.service.identity, value)
        self.assertFalse((self.directory / 'phones.json').exists())
        self.register()  # Invalid input must not spend the valid-new-client budget.

    def test_corrupt_or_symlink_registry_fails_closed(self):
        path = self.directory / 'phones.json'
        path.write_text('{"version":2,"grants":"not-an-array"}')
        path.chmod(0o600)
        with self.assertRaises((ValueError, PairingFailure, bridge.Failure)):
            self.make_service()
        path.unlink()
        target = self.directory / 'other.json'
        target.write_text('{"version":2,"grants":[]}')
        target.chmod(0o600)
        path.symlink_to(target)
        with self.assertRaises(bridge.Failure): self.make_service()


class SessionPrincipalChecks(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.session = bridge.Session(clock=self.clock)
        self.owner, self.other = str(uuid.uuid4()), str(uuid.uuid4())
        self.identity = str(uuid.uuid4())
        self.session.session(start_request(self.identity), principal=self.owner)

    def rejects(self, operation, *args, **kwargs):
        with self.assertRaises(bridge.Failure) as caught: operation(*args, **kwargs)
        self.assertEqual(caught.exception.status, 403)

    def test_other_principal_cannot_reuse_start_poll_change_geometry_or_end(self):
        self.rejects(self.session.session, start_request(self.identity), principal=self.other)
        self.rejects(self.session.poll, self.identity, timeout=0, principal=self.other)
        for state in ('pause', 'end'):
            self.rejects(self.session.session, {'v': 1, 'session_id': self.identity, 'state': state}, principal=self.other)
        self.rejects(self.session.session,
                     {'v': 1, 'session_id': self.identity, 'state': 'geometry', 'geometry_epoch': 1,
                      'width': 2, 'height': 3, 'orientation': 1}, principal=self.other)
        self.assertTrue(self.session.active)
        self.assertIsNone(self.session.poll(self.identity, timeout=0, principal=self.owner))

    def test_other_principal_cannot_inject_requested_frame(self):
        request = str(uuid.uuid4())
        self.session.pending = {'id': request, 'deadline': self.clock.now + 5, 'observation_epoch': self.session.observation_epoch}
        headers = frame_headers(self.identity, request)
        self.rejects(self.session.accept_frame, headers, JPEG, principal=self.other)
        self.assertIsNone(self.session.frame)
        self.session.accept_frame(headers, JPEG, principal=self.owner)
        self.assertEqual(self.session.frame['jpeg'], JPEG)

    def test_closed_session_retry_is_idempotent_only_for_its_owner(self):
        value = {'v': 1, 'session_id': self.identity, 'state': 'pause'}
        self.session.session(value, principal=self.owner)
        self.session.session(value, principal=self.owner)
        self.rejects(self.session.session, value, principal=self.other)
        self.rejects(self.session.session, dict(value, state='end'), principal=self.other)

    def test_only_old_owner_can_distinguish_heartbeat_timeout_from_revocation(self):
        for reason in ('heartbeat_timeout', 'lease_expired', 'agent_stop', 'owner_end', 'owner_pause'):
            clock = Clock(); state = bridge.Session(clock=clock)
            identity = str(uuid.uuid4())
            state.session(dict(v=1, session_id=identity, state='start'), principal=self.owner)
            if reason == 'heartbeat_timeout': clock.now += 21; state.status()
            elif reason == 'lease_expired': clock.now += 1801; state.status()
            elif reason == 'agent_stop': state.stop()
            else: state.session(dict(v=1, session_id=identity, state=reason[6:]), principal=self.owner)
            expected = 'session_heartbeat_expired' if reason == 'heartbeat_timeout' else 'session_revoked'
            with self.assertRaises(bridge.Failure) as caught:
                state.poll(identity, 0, principal=self.owner)
            self.assertEqual((caught.exception.status, caught.exception.code), (409, expected))
            # An agent action retains the generic inactive contract, and a
            # different phone never receives the old owner's disposition.
            with self.assertRaises(bridge.Failure) as caught:
                state._require(identity)
            self.assertEqual(caught.exception.code, 'session_inactive')
            self.rejects(state.poll, identity, timeout=0, principal=self.other)
            self.rejects(state.session, dict(v=1, session_id=identity, state='start'), principal=self.other)

    def test_stopped_or_ended_session_cannot_masquerade_as_a_confirmed_pause(self):
        pause = dict(v=1, session_id=self.identity, state='pause')
        self.session.session(pause, principal=self.owner)
        self.session.session(pause, principal=self.owner)  # Confirmed own pause.
        self.session.stop()  # Stop arriving while already paused is terminal.
        self.assertEqual(self.session.closed_reasons[self.identity], 'agent_stop')
        for state in ('pause', 'end', 'start'):
            with self.assertRaises(bridge.Failure) as caught:
                self.session.session(dict(pause, state=state), principal=self.owner)
            self.assertEqual(caught.exception.code, 'session_revoked')
        self.assertFalse(self.session.active)

    def test_unknown_or_restarted_history_does_not_offer_a_recoverable_reason(self):
        restarted = bridge.Session(clock=self.clock)
        for identity in (self.identity, str(uuid.uuid4())):
            with self.assertRaises(bridge.Failure) as caught:
                restarted.poll(identity, 0, principal=self.owner)
            self.assertEqual(caught.exception.code, 'session_inactive')
        self.assertEqual(restarted.closed_reasons, {})

    def test_closed_reasons_share_the_existing_256_screen_history_bound(self):
        state = bridge.Session(clock=self.clock)
        for _ in range(256):
            identity = str(uuid.uuid4())
            state.session(dict(v=1, session_id=identity, state='start'), principal=self.owner)
            state.session(dict(v=1, session_id=identity, state='end'), principal=self.owner)
        self.assertEqual(len(state.closed), 256)
        self.assertEqual(set(state.closed_reasons), state.closed)
        self.assertEqual(set(state.closed_owners), state.closed)
        with self.assertRaises(bridge.Failure) as caught:
            state.session(dict(v=1, session_id=str(uuid.uuid4()), state='start'), principal=self.owner)
        self.assertEqual(caught.exception.code, 'session_history_full_restart_bridge')
        state.stop()
        self.assertEqual(len(state.closed_reasons), 256)
        self.assertEqual(state.closed_reasons[identity], 'agent_stop')

    def test_atomic_resume_is_once_only_and_retry_of_the_same_new_id_is_idempotent(self):
        pause = dict(v=1, session_id=self.identity, state='pause')
        self.session.session(pause, principal=self.owner)
        new = str(uuid.uuid4())
        continuation = dict(v=1, session_id=new, state='start', resume_from=self.identity)
        self.session.session(continuation, principal=self.owner)
        self.session.session(continuation, principal=self.owner)
        self.assertEqual(self.session.identity, new)
        self.assertFalse(self.session.allow_control)
        self.assertEqual(self.session.resume_successors, {self.identity: new})
        self.session.stop()
        for previous in (self.identity, new):
            with self.assertRaises(bridge.Failure) as caught:
                self.session.session(dict(continuation, session_id=str(uuid.uuid4()), resume_from=previous),
                                     principal=self.owner)
            self.assertEqual(caught.exception.code, 'session_revoked')
        with self.assertRaises(bridge.Failure) as caught:
            self.session.session(continuation, principal=self.owner)
        self.assertEqual(caught.exception.code, 'session_revoked')
        self.assertFalse(self.session.active)

    def test_stop_after_confirmed_pause_before_new_start_rejects_continuation(self):
        pause = dict(v=1, session_id=self.identity, state='pause')
        self.session.session(pause, principal=self.owner)
        self.session.session(pause, principal=self.owner)  # Drain returned 200.
        self.session.stop()
        with self.assertRaises(bridge.Failure) as caught:
            self.session.session(dict(v=1, session_id=str(uuid.uuid4()), state='start',
                                      resume_from=self.identity), principal=self.owner)
        self.assertEqual(caught.exception.code, 'session_revoked')
        self.assertEqual(self.session.resume_successors, {})
        self.assertFalse(self.session.active)

    def test_explicit_owner_end_after_heartbeat_timeout_revokes_continuation(self):
        self.clock.now += 21
        self.session.status()
        end = dict(v=1, session_id=self.identity, state='end')
        self.rejects(self.session.session, end, principal=self.other)
        self.assertEqual(self.session.closed_reasons[self.identity], 'heartbeat_timeout')
        self.session.session(end, principal=self.owner)
        self.session.session(end, principal=self.owner)
        self.assertEqual(self.session.closed_reasons[self.identity], 'owner_end')
        self.assertEqual(self.session.status()['last_inactive_reason'], 'owner_end')
        for value in (dict(end, state='pause'),
                      dict(end, state='start', session_id=str(uuid.uuid4()), resume_from=self.identity)):
            with self.assertRaises(bridge.Failure) as caught:
                self.session.session(value, principal=self.owner)
            self.assertEqual(caught.exception.code, 'session_revoked')
        self.assertFalse(self.session.active)
        self.assertEqual(self.session.resume_successors, {})

    def test_late_ancestor_end_revokes_its_active_successor_chain(self):
        lineage = [self.identity, str(uuid.uuid4()), str(uuid.uuid4())]
        for previous, successor in zip(lineage, lineage[1:]):
            self.session.session(dict(v=1, session_id=previous, state='pause'), principal=self.owner)
            self.session.session(dict(v=1, session_id=successor, state='start', resume_from=previous),
                                 principal=self.owner)
        end = dict(v=1, session_id=lineage[0], state='end')
        self.rejects(self.session.session, end, principal=self.other)
        self.assertTrue(self.session.active)
        self.session.session(end, principal=self.owner)
        self.session.session(end, principal=self.owner)  # An old end replay stays terminal.
        self.assertFalse(self.session.active)
        self.assertEqual(self.session.status()['last_inactive_reason'], 'owner_end')
        self.assertEqual(self.session.closed_reasons, dict.fromkeys(lineage, 'owner_end'))
        for previous in lineage:
            with self.assertRaises(bridge.Failure) as caught:
                self.session.session(dict(v=1, session_id=str(uuid.uuid4()), state='start',
                                          resume_from=previous), principal=self.owner)
            self.assertEqual(caught.exception.code, 'session_revoked')

    def test_late_end_leaves_unrelated_manual_broadcast_and_terminal_reasons_unchanged(self):
        for reason in ('owner_pause', 'agent_stop'):
            with self.subTest(reason=reason):
                state = bridge.Session(clock=self.clock)
                ancestor, successor, manual = (str(uuid.uuid4()) for _ in range(3))
                state.session(dict(v=1, session_id=ancestor, state='start'), principal=self.owner)
                state.session(dict(v=1, session_id=ancestor, state='pause'), principal=self.owner)
                state.session(dict(v=1, session_id=successor, state='start', resume_from=ancestor),
                              principal=self.owner)
                if reason == 'agent_stop': state.stop()
                else: state.session(dict(v=1, session_id=successor, state='pause'), principal=self.owner)
                state.session(dict(v=1, session_id=manual, state='start'), principal=self.owner)
                state.session(dict(v=1, session_id=ancestor, state='end'), principal=self.owner)
                self.assertTrue(state.active)
                self.assertEqual(state.identity, manual)
                self.assertEqual(state.closed_reasons,
                                 {ancestor: 'owner_end', successor: 'agent_stop' if reason == 'agent_stop' else 'owner_end'})
                self.assertEqual(state.status()['last_inactive_reason'], reason)
                state.session(dict(v=1, session_id=manual, state='pause'), principal=self.owner)
                for previous in (ancestor, successor):
                    with self.assertRaises(bridge.Failure) as caught:
                        state.session(dict(v=1, session_id=str(uuid.uuid4()), state='start',
                                           resume_from=previous), principal=self.owner)
                    self.assertEqual(caught.exception.code, 'session_revoked')

    def test_heartbeat_continuation_and_late_ancestor_cannot_bypass_later_stop(self):
        self.clock.now += 21
        self.session.status()
        first = str(uuid.uuid4())
        self.session.session(dict(v=1, session_id=first, state='start', resume_from=self.identity),
                             principal=self.owner)
        self.clock.now += 21
        self.session.status()
        second = str(uuid.uuid4())
        self.session.session(dict(v=1, session_id=second, state='start', resume_from=first),
                             principal=self.owner)
        self.session.stop()
        for previous in (self.identity, first, second):
            with self.assertRaises(bridge.Failure) as caught:
                self.session.session(dict(v=1, session_id=str(uuid.uuid4()), state='start',
                                          resume_from=previous), principal=self.owner)
            self.assertEqual(caught.exception.code, 'session_revoked')
        self.assertEqual(len(self.session.resume_successors), 2)
        self.assertFalse(self.session.active)

    def test_resume_unknown_history_wrong_owner_and_invalid_fields_never_create_session(self):
        self.session.session(dict(v=1, session_id=self.identity, state='pause'), principal=self.owner)
        new = str(uuid.uuid4())
        base = dict(v=1, session_id=new, state='start', resume_from=self.identity)
        self.rejects(self.session.session, base, principal=self.other)
        for previous in (str(uuid.uuid4()),):
            with self.assertRaises(bridge.Failure) as caught:
                self.session.session(dict(base, resume_from=previous), principal=self.owner)
            self.assertEqual(caught.exception.code, 'session_inactive')
        for previous in (None, False, 'not-a-uuid', new):
            with self.assertRaises(bridge.Failure) as caught:
                self.session.session(dict(base, resume_from=previous), principal=self.owner)
            self.assertEqual(caught.exception.status, 400)
        restarted = bridge.Session(clock=self.clock)
        with self.assertRaises(bridge.Failure) as caught:
            restarted.session(base, principal=self.owner)
        self.assertEqual(caught.exception.code, 'session_inactive')
        self.assertFalse(restarted.active)
        self.assertEqual(self.session.resume_successors, {})

    def test_resume_maps_stay_within_the_existing_screen_history_limit(self):
        state = bridge.Session(clock=self.clock)
        current = str(uuid.uuid4())
        ancestor = current
        state.session(dict(v=1, session_id=current, state='start'), principal=self.owner)
        for _ in range(255):
            previous, current = current, str(uuid.uuid4())
            state.session(dict(v=1, session_id=previous, state='pause'), principal=self.owner)
            state.session(dict(v=1, session_id=current, state='start', resume_from=previous),
                          principal=self.owner)
        state.session(dict(v=1, session_id=current, state='pause'), principal=self.owner)
        with self.assertRaises(bridge.Failure) as caught:
            state.session(dict(v=1, session_id=str(uuid.uuid4()), state='start', resume_from=current),
                          principal=self.owner)
        self.assertEqual(caught.exception.code, 'session_history_full_restart_bridge')
        self.assertEqual(len(state.closed), 256)
        self.assertEqual(set(state.closed_reasons), state.closed)
        self.assertEqual(set(state.closed_owners), state.closed)
        self.assertEqual(len(state.resume_successors), 255)
        self.assertTrue(set(state.resume_successors) <= state.closed)
        state.session(dict(v=1, session_id=ancestor, state='end'), principal=self.owner)
        self.assertEqual(set(state.closed_reasons.values()), {'owner_end'})
        self.assertEqual(len(state.closed), 256)
        self.assertEqual(len(state.resume_successors), 255)


class PairingTLSChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.temporary.name).resolve() / 'state'
        bridge.init_state(cls.directory, '127.0.0.1', 8765, origin_host='pairing-fixture.local')
        cls.stored_config = bridge.configuration(cls.directory)
        cls.stored_phone = bridge.pairing_configuration(cls.directory, cls.stored_config)

    @classmethod
    def tearDownClass(cls): cls.temporary.cleanup()

    def setUp(self):
        self.clock = Clock()
        self.session = bridge.Session(clock=self.clock)
        self.server = bridge.Server(('127.0.0.1', 0), self.session,
                                    self.stored_config['uploadToken'], self.stored_config['agentToken'], '')
        self.authority = 'pairing-fixture.local:' + str(self.server.server_address[1])
        self.server.authority = self.authority
        self.phone = dict(self.stored_phone, baseURL='https://' + self.authority)
        self.registry = tempfile.TemporaryDirectory()
        self.addCleanup(self.registry.cleanup)
        self.service = PairingService(self.phone, self.registry.name, clock=self.clock,
                                      available=lambda: not self.session.status()['active'])
        self.server.pairing = self.service
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.directory / 'certificate.pem', self.directory / 'private-key.pem')
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True,
                                                 do_handshake_on_connect=False)
        self.context = ssl.create_default_context(cafile=str(self.directory / 'certificate.pem'))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(3)
        self.service.close()

    def request(self, path, value=None, *, token=None, role=None, headers=None, method='POST'):
        connection = http.client.HTTPSConnection('127.0.0.1', self.server.server_address[1],
                                                  context=self.context, timeout=3)
        try:
            connection.connect()
            self.assertEqual(hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest(),
                             self.phone['certificateSHA256'])
            fields = {'Host': self.authority, 'Connection': 'close'}
            if role: token = self.stored_config['agentToken' if role == 'agent' else 'uploadToken']
            if token: fields['Authorization'] = 'Bearer ' + token
            body = value if isinstance(value, bytes) else None if value is None else json.dumps(value).encode()
            if body is not None:
                fields['Content-Type'] = 'image/jpeg' if isinstance(value, bytes) else 'application/json'
            fields.update(headers or {})
            connection.request(method, path, body, fields)
            response = connection.getresponse()
            data = response.read()
            self.assertEqual(response.getheader('Cache-Control'), 'no-store')
            for secret in (self.stored_config['agentToken'], self.phone['uploadToken']):
                self.assertNotIn(secret.encode(), data)
            value = data if response.getheader('Content-Type', '').startswith('image/jpeg') else json.loads(data) if data else None
            return response.status, value
        finally:
            connection.close()

    def register(self, request=None):
        request = request or trust_request(self.phone)
        status, answer = self.request('/v2/pairing/trust', request)
        self.assertEqual(status, 200)
        self.assertEqual(set(answer), {'v', 'client_id', 'request_id', 'configuration'})
        self.assertEqual((answer['client_id'], answer['request_id']), (request['client_id'], request['request_id']))
        self.assertEqual({k: v for k, v in answer['configuration'].items() if k != 'uploadToken'},
                         {k: v for k, v in self.phone.items() if k != 'uploadToken'})
        token = answer['configuration']['uploadToken']
        self.assertRegex(token, r'^[0-9a-f]{64}$')
        self.assertNotEqual(token, self.phone['uploadToken'])
        return request, token

    def test_identity_and_cancel_are_read_only_over_real_tls(self):
        for _ in range(NEW_PER_WINDOW + 1):
            status, answer = self.request('/v2/pairing/identity', {'v': 2})
            self.assertEqual(status, 200)
            self.assertEqual(answer, {'v': 2, **{k: self.phone[k] for k in ('name', 'baseURL', 'certificateSHA256')}})
        self.assertFalse((Path(self.registry.name) / 'phones.json').exists())
        self.assertFalse(self.session.active)
        self.assertFalse(self.session.allow_control)
        self.assertIsNone(self.session.frame)

    def test_registration_only_grants_phone_routes_without_screen_or_control(self):
        request, token = self.register()
        self.assertEqual(self.request('/v1/connection', token=token, method='GET')[0], 200)
        self.assertEqual(self.request('/v1/connection', role='phone', method='GET')[0], 200)
        for path, method, value in (('/v1/status', 'GET', None), ('/v1/screen', 'POST', {}),
                                    ('/v1/actions', 'POST', {}), ('/v1/stop', 'POST', {}), ('/v1/control/reset', 'POST', {})):
            self.assertEqual(self.request(path, value, token=token, method=method)[0], 401)
        self.assertEqual(self.request('/v1/status', role='agent', method='GET')[0], 200)
        self.assertFalse(self.session.active)
        self.assertFalse(self.session.allow_control)
        self.assertIsNone(self.session.pending)
        self.assertEqual(self.request('/v2/pairing/trust', request)[1]['configuration']['uploadToken'], token)

    def test_tls_recovery_reason_is_owner_only_and_desktop_stop_blocks_pause_drain(self):
        _, first = self.register()
        _, second = self.register()
        identity = str(uuid.uuid4())
        start = dict(v=1, session_id=identity, state='start')
        self.assertEqual(self.request('/v1/session', start, token=first)[0], 200)
        self.clock.now += 21
        query = '/v1/capture-request?session_id=' + identity
        self.assertEqual(self.request(query, token=first, method='GET'),
                         (409, {'ok': False, 'error': 'session_heartbeat_expired'}))
        self.assertEqual(self.request('/v1/session', start, token=first),
                         (409, {'ok': False, 'error': 'session_heartbeat_expired'}))
        self.assertEqual(self.request(query, token=second, method='GET'),
                         (403, {'ok': False, 'error': 'session_owner_mismatch'}))
        self.assertEqual(self.request('/v1/session', start, token=second),
                         (403, {'ok': False, 'error': 'session_owner_mismatch'}))
        unknown = '/v1/capture-request?session_id=' + str(uuid.uuid4())
        self.assertEqual(self.request(unknown, token=first, method='GET'),
                         (409, {'ok': False, 'error': 'session_inactive'}))
        # Stop may arrive after the heartbeat failure but before a retry/drain.
        self.assertEqual(self.request('/v1/stop', {}, role='agent')[0], 200)
        self.assertEqual(self.request(query, token=first, method='GET'),
                         (409, {'ok': False, 'error': 'session_revoked'}))
        for state in ('pause', 'end', 'start'):
            self.assertEqual(self.request('/v1/session', dict(start, state=state), token=first),
                             (409, {'ok': False, 'error': 'session_revoked'}))
        continuation = dict(start, session_id=str(uuid.uuid4()), resume_from=identity)
        self.assertEqual(self.request('/v1/session', continuation, token=first),
                         (409, {'ok': False, 'error': 'session_revoked'}))
        self.assertFalse(self.session.active)

    def test_tls_continuation_binds_one_successor_and_stop_blocks_old_ancestors(self):
        _, owner = self.register()
        _, other = self.register()
        old, new = str(uuid.uuid4()), str(uuid.uuid4())
        self.assertEqual(self.request('/v1/session', dict(v=1, session_id=old, state='start'), token=owner)[0], 200)
        self.assertEqual(self.request('/v1/session', dict(v=1, session_id=old, state='pause'), token=owner)[0], 200)
        body = dict(v=1, session_id=new, state='start', resume_from=old)
        self.assertEqual(self.request('/v1/session', body, token=other),
                         (403, {'ok': False, 'error': 'session_owner_mismatch'}))
        self.assertEqual(self.request('/v1/session', body, token=owner), (200, {'ok': True}))
        self.assertEqual(self.request('/v1/session', body, token=owner), (200, {'ok': True}))
        self.assertEqual(self.session.resume_successors, {old: new})
        self.assertFalse(self.session.allow_control)
        self.assertEqual(self.request('/v1/stop', {}, role='agent')[0], 200)
        for previous in (old, new):
            candidate = dict(body, session_id=str(uuid.uuid4()), resume_from=previous)
            self.assertEqual(self.request('/v1/session', candidate, token=owner),
                             (409, {'ok': False, 'error': 'session_revoked'}))
        self.assertFalse(self.session.active)

    def test_tls_owner_end_after_timeout_or_resume_is_terminal_only_for_its_lineage(self):
        _, owner = self.register()
        _, other = self.register()
        expired = str(uuid.uuid4())
        start = dict(v=1, session_id=expired, state='start')
        self.assertEqual(self.request('/v1/session', start, token=owner)[0], 200)
        self.clock.now += 21
        end = dict(start, state='end')
        self.assertEqual(self.request('/v1/session', end, token=other),
                         (403, {'ok': False, 'error': 'session_owner_mismatch'}))
        self.assertEqual(self.request('/v1/session', end, token=owner), (200, {'ok': True}))
        self.assertEqual(self.session.closed_reasons[expired], 'owner_end')
        candidate = dict(start, session_id=str(uuid.uuid4()), resume_from=expired)
        self.assertEqual(self.request('/v1/session', candidate, token=owner),
                         (409, {'ok': False, 'error': 'session_revoked'}))

        ancestor, successor, manual = (str(uuid.uuid4()) for _ in range(3))
        self.assertEqual(self.request('/v1/session', dict(start, session_id=ancestor), token=owner)[0], 200)
        self.assertEqual(self.request('/v1/session', dict(end, session_id=ancestor, state='pause'), token=owner)[0], 200)
        self.assertEqual(self.request('/v1/session', dict(start, session_id=successor, resume_from=ancestor), token=owner)[0], 200)
        end = dict(end, session_id=ancestor)
        self.assertEqual(self.request('/v1/session', end, token=owner), (200, {'ok': True}))
        self.assertFalse(self.session.active)
        self.assertEqual(self.session.closed_reasons[successor], 'owner_end')
        self.assertEqual(self.request('/v1/session', dict(start, session_id=manual), token=owner)[0], 200)
        self.assertEqual(self.request('/v1/session', end, token=owner), (200, {'ok': True}))
        self.assertTrue(self.session.active)
        self.assertEqual(self.session.identity, manual)
        self.assertEqual(self.request('/v1/session', dict(end, session_id=manual, state='pause'), token=owner)[0], 200)
        for previous in (ancestor, successor):
            self.assertEqual(self.request('/v1/session', dict(candidate, resume_from=previous), token=owner),
                             (409, {'ok': False, 'error': 'session_revoked'}))

    def test_role_tokens_and_public_ids_cannot_impersonate_the_new_phone(self):
        request, token = self.register()
        self.assertEqual(self.request('/v1/connection', role='agent', method='GET')[0], 401)
        self.assertEqual(self.request('/v1/connection', token='0' * 64, method='GET')[0], 401)
        self.assertEqual(self.request('/v1/connection', token=request['client_id'], method='GET')[0], 401)
        self.assertEqual(self.request('/v2/pairing/trust', dict(request, client_secret='d' * 64))[0], 409)
        self.assertEqual(self.request('/v1/connection', token=token, method='GET')[0], 200)

    def test_first_phone_session_cannot_be_taken_over_polled_injected_or_ended_by_second(self):
        _, first = self.register()
        _, second = self.register()
        start = start_request()
        identity = start['session_id']
        self.assertEqual(self.request('/v1/session', start, token=first)[0], 200)
        self.assertEqual(self.request('/v1/session', start, token=second)[0], 403)
        self.assertEqual(self.request('/v1/capture-request?session_id=' + identity, token=second, method='GET')[0], 403)
        self.assertEqual(self.request('/v1/session', {'v': 1, 'session_id': identity, 'state': 'end'}, token=second)[0], 403)
        request = str(uuid.uuid4())
        self.session.pending = {'id': request, 'deadline': self.clock.now + 5, 'observation_epoch': self.session.observation_epoch}
        headers = {'X-StreamShot-' + k: v for k, v in frame_headers(identity, request).items()}
        self.assertEqual(self.request('/v1/frame', JPEG, token=second, headers=headers)[0], 403)
        self.assertIsNone(self.session.frame)
        self.assertEqual(self.request('/v1/frame', JPEG, token=first, headers=headers)[0], 201)
        self.assertEqual(self.session.frame['jpeg'], JPEG)

    def test_active_session_allows_identity_but_blocks_new_grant_and_retry(self):
        request, token = self.register()
        self.assertEqual(self.request('/v1/session', start_request(), token=token)[0], 200)
        self.assertEqual(self.request('/v2/pairing/identity', {'v': 2})[0], 200)
        self.assertEqual(self.request('/v2/pairing/trust', request)[0], 409)
        self.assertEqual(self.request('/v2/pairing/trust', trust_request(self.phone))[0], 409)

    def test_requested_frame_remains_phone_owned_and_agent_read_only(self):
        _, token = self.register()
        start = start_request()
        self.assertEqual(self.request('/v1/session', start, token=token)[0], 200)
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            screen = pool.submit(self.request, '/v1/screen', {}, role='agent')
            status, demand = self.request('/v1/capture-request?session_id=' + start['session_id'], token=token, method='GET')
            self.assertEqual(status, 200)
            headers = {'X-StreamShot-' + k: v for k, v in frame_headers(start['session_id'], demand['request_id']).items()}
            self.assertEqual(self.request('/v1/frame', JPEG, token=token, headers=headers)[0], 201)
            self.assertEqual(screen.result(3), (200, JPEG))
        self.assertFalse(self.session.allow_control)
        self.assertEqual(self.request('/v1/actions', {}, token=token)[0], 401)

    def test_actual_certificate_hash_is_required_and_known_hostname_is_verified(self):
        status, _ = self.request('/v2/pairing/trust', trust_request(self.phone, certificateSHA256='0' * 64))
        self.assertEqual(status, 403)
        self.assertFalse((Path(self.registry.name) / 'phones.json').exists())
        with socket.create_connection(('127.0.0.1', self.server.server_address[1]), timeout=3) as raw:
            with self.assertRaises(ssl.SSLCertVerificationError):
                self.context.wrap_socket(raw, server_hostname='different-fixture.local')

    def test_public_routes_reject_origin_spoofed_host_query_and_extra_fields(self):
        for path, value in (('/v2/pairing/identity', {'v': 2}), ('/v2/pairing/trust', trust_request(self.phone))):
            for headers in ({'Origin': 'https://' + self.authority}, {'Origin': 'null'}, {'Host': 'attacker.local:8765'}):
                status, answer = self.request(path, value, headers=headers)
                self.assertEqual((status, answer['error']), (403, 'origin_or_host_rejected'))
            self.assertEqual(self.request(path + '?ignored=1', value)[0], 404)
            self.assertEqual(self.request(path, dict(value, allow_control=True))[0], 400)
            self.assertEqual(self.request(path, method='GET')[0], 404)
        self.assertFalse((Path(self.registry.name) / 'phones.json').exists())

    def test_legacy_pairing_routes_are_removed_even_with_agent_credentials(self):
        for path in ('/v1/pairing/open', '/v1/pairing/start', '/v1/pairing/finish'):
            for role in (None, 'phone', 'agent'):
                self.assertEqual(self.request(path, {}, role=role)[0], 404)

    def test_disabled_service_has_no_public_trust_routes(self):
        self.server.pairing = None
        for path in ('/v2/pairing/identity', '/v2/pairing/trust'):
            self.assertEqual(self.request(path, {'v': 2})[0], 404)
        status, answer = self.request('/v1/status', role='agent', method='GET')
        self.assertEqual(status, 200)
        self.assertFalse(answer['pairing_available'])

    def malformed_headers(self, *, duplicate_host=False, duplicate_length=False, transfer=False):
        connection = http.client.HTTPSConnection('127.0.0.1', self.server.server_address[1], context=self.context, timeout=3)
        try:
            connection.putrequest('POST', '/v2/pairing/identity', skip_host=True)
            connection.putheader('Host', self.authority)
            if duplicate_host: connection.putheader('Host', self.authority)
            connection.putheader('Content-Type', 'application/json')
            connection.putheader('Content-Length', '7')
            if duplicate_length: connection.putheader('Content-Length', '4096')
            if transfer: connection.putheader('Transfer-Encoding', 'chunked')
            connection.endheaders(b'{"v":2}')
            response = connection.getresponse()
            data = json.loads(response.read())
            return response.status, data
        finally:
            connection.close()

    def test_duplicate_host_content_length_and_transfer_encoding_are_rejected(self):
        self.assertEqual(self.malformed_headers(duplicate_host=True)[0], 403)
        self.assertEqual(self.malformed_headers(duplicate_length=True)[0], 411)
        self.assertEqual(self.malformed_headers(transfer=True)[0], 411)
        self.assertFalse((Path(self.registry.name) / 'phones.json').exists())

    def test_incomplete_tls_handshake_does_not_block_another_client(self):
        with socket.create_connection(('127.0.0.1', self.server.server_address[1]), timeout=3) as slow:
            slow.sendall(b'\x16\x03\x01')
            self.assertEqual(self.request('/health', method='GET'), (200, {'v': 1, 'ok': True}))

    def test_incomplete_json_body_does_not_block_another_client(self):
        connection = http.client.HTTPSConnection('127.0.0.1', self.server.server_address[1], context=self.context, timeout=3)
        try:
            connection.putrequest('POST', '/v2/pairing/identity', skip_host=True)
            connection.putheader('Host', self.authority)
            connection.putheader('Content-Type', 'application/json')
            connection.putheader('Content-Length', '4096')
            connection.endheaders(b'{')
            self.assertEqual(self.request('/health', method='GET')[0], 200)
            connection.send(b' ' * 4095)
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
        finally:
            connection.close()

    def test_worker_limit_closes_excess_connection_before_starting_tls_worker(self):
        # Occupy all available worker permits without waiting for slow timeouts.
        count = 0
        try:
            while self.server.workers.acquire(blocking=False): count += 1
            self.assertEqual(count, 8)
            with socket.create_connection(('127.0.0.1', self.server.server_address[1]), timeout=3) as extra:
                self.assertEqual(extra.recv(1), b'')
        finally:
            for _ in range(count): self.server.workers.release()
        self.assertEqual(self.request('/health', method='GET')[0], 200)


class PairingConfigurationChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.temporary.name).resolve() / 'state'
        bridge.init_state(cls.directory, '127.0.0.1', 8765, origin_host='pairing-fixture.local')
        cls.config = bridge.configuration(cls.directory)
        cls.phone = bridge.pairing_configuration(cls.directory, cls.config)

    @classmethod
    def tearDownClass(cls): cls.temporary.cleanup()

    def test_stable_local_origin_and_actual_leaf_certificate_match(self):
        self.assertEqual(self.config['baseURL'], 'https://pairing-fixture.local:8765')
        decoded = ssl._ssl._test_decode_cert(str(self.directory / 'certificate.pem'))
        self.assertIn(('DNS', 'pairing-fixture.local'), decoded['subjectAltName'])
        pin = hashlib.sha256(ssl.PEM_cert_to_DER_cert((self.directory / 'certificate.pem').read_text())).hexdigest()
        self.assertEqual(self.phone['certificateSHA256'], pin)
        self.assertNotIn('agentToken', self.phone)

    def test_stale_certificate_origin_or_role_token_fails_closed(self):
        path = self.directory / 'phone-connection.json'
        original = path.read_bytes()
        try:
            for change in ({'certificateSHA256': '0' * 64}, {'baseURL': 'https://other.local:8765'},
                           {'uploadToken': self.config['agentToken']}, {'agentToken': self.config['agentToken']}):
                with self.subTest(fields=list(change)):
                    path.write_text(json.dumps(dict(self.phone, **change)))
                    with self.assertRaises(bridge.Failure):
                        bridge.pairing_configuration(self.directory, self.config)
        finally:
            path.write_bytes(original)


if __name__ == '__main__': unittest.main()
