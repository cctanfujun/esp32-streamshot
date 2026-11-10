"""Synthetic protocol, concurrency and real localhost TLS tests; no phone or HID."""
import base64
import concurrent.futures
import contextlib
import importlib.util
import json
from pathlib import Path
import ssl
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import streamshot_bridge as bridge
import streamshot_mcp as mcp

# A generated, solid-color 2x3 JPEG. No screenshot or external image source.
JPEG = base64.b64decode('/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAA0JCgsKCA0LCgsODg0PEyAVExISEyccHhcgLikxMC4pLSwzOko+MzZGNywtQFdBRkxOUlNSMj5aYVpQYEpRUk//2wBDAQ4ODhMREyYVFSZPNS01T09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT0//wAARCAADAAIDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAb/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QAFQEBAQAAAAAAAAAAAAAAAAAABAX/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIRAxEAPwCfAUAX/9k=')


class Clock:
    now = 0
    def __call__(self): return self.now


class Controller:
    def __init__(self):
        self.calls = []; self.fail = None; self.cancelled = []; self.heartbeats = []
    def perform(self, session, identity, operation, arguments):
        self.calls.append((session, identity, operation, arguments))
        if self.fail: raise self.fail
    def stop(self, session): self.calls.append(('stop', session))
    def cancel(self, session): self.cancelled.append(session)
    def heartbeat(self, session): self.heartbeats.append(session)
    def status(self): return {'available': True, 'armed': False}


class StateChecks(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(); self.controller = Controller()
        self.state = bridge.Session(self.controller, self.clock)
        self.session = str(uuid.uuid4())
        self.start()

    def start(self, control=True):
        self.state.session({'v': 1, 'session_id': self.session, 'state': 'start', 'allow_control': control})

    def frame_headers(self, request, sequence=1, epoch=1):
        return dict(session=self.session, request=request, sequence=str(sequence),
                    width='2', height='3', orientation='1', **{'geometry-epoch': str(epoch)})

    def capture(self, sequence=1, epoch=1):
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            result = pool.submit(self.state.capture, .5)
            request = self.state.poll(self.session, .2)
            self.assertIsNotNone(request)
            self.state.accept_frame(self.frame_headers(request['request_id'], sequence, epoch), JPEG)
            return result.result(1)

    def action(self, frame, operation='click', **kwargs):
        return dict(v=1, id=str(uuid.uuid4()), session_id=self.session,
                    frame_id=frame['frame_id'], op=operation, **kwargs)

    def reset(self, **changes):
        return dict(dict(v=1, id=str(uuid.uuid4()), session_id=self.session), **changes)

    def assertFailure(self, code, function, *args):
        with self.assertRaises(bridge.Failure) as caught: function(*args)
        self.assertEqual(caught.exception.code, code)

    def test_idle_neither_requests_nor_accepts_an_unsolicited_frame(self):
        self.assertIsNone(self.state.poll(self.session, 0))
        self.assertIsNone(self.state.frame)
        self.assertFailure('frame_not_requested_or_expired', self.state.accept_frame,
                           self.frame_headers(str(uuid.uuid4())), JPEG)
        self.assertEqual(self.controller.calls, [])

    def test_status_tracks_only_bounded_heartbeat_age_and_explicit_inactive_reason(self):
        fresh = bridge.Session(Controller(), self.clock)
        self.assertEqual({k: fresh.status()[k] for k in
                          ('last_inactive_reason', 'heartbeat_age_seconds', 'screen_request_timeouts')},
                         dict(last_inactive_reason=None, heartbeat_age_seconds=None, screen_request_timeouts=0))
        self.clock.now = 19
        self.assertEqual(self.state.status()['heartbeat_age_seconds'], 19)
        self.assertIsNone(self.state.status()['last_inactive_reason'])
        self.state.session(dict(v=1, session_id=self.session, state='pause'))
        self.assertEqual(self.state.status()['last_inactive_reason'], 'owner_pause')
        self.assertIsNone(self.state.status()['heartbeat_age_seconds'])
        self.session = str(uuid.uuid4()); self.start()
        self.assertEqual(self.state.status()['heartbeat_age_seconds'], 0)
        self.assertEqual(self.state.status()['last_inactive_reason'], 'owner_pause')
        self.state.session(dict(v=1, session_id=self.session, state='end'))
        self.state.stop()  # A later inactive stop cannot erase the real cause.
        self.assertEqual(self.state.status()['last_inactive_reason'], 'owner_end')

    def test_timeout_lease_and_agent_stop_diagnostics_do_not_change_expiration_rules(self):
        for elapsed, reason in ((21, 'heartbeat_timeout'), (1801, 'lease_expired')):
            clock = Clock(); clock.now = 0
            state = bridge.Session(Controller(), clock)
            state.session(dict(v=1, session_id=str(uuid.uuid4()), state='start'))
            clock.now = elapsed
            status = state.status()
            self.assertFalse(status['active'])
            self.assertEqual(status['last_inactive_reason'], reason)
            self.assertIsNone(status['heartbeat_age_seconds'])
        for reason in ('agent_stop', 'bridge_shutdown'):
            state = bridge.Session(Controller(), self.clock)
            state.session(dict(v=1, session_id=str(uuid.uuid4()), state='start'))
            state.stop(reason=reason)
            self.assertEqual(state.status()['last_inactive_reason'], reason)

    def test_capture_timeout_counter_is_bounded_and_not_a_screen_stop(self):
        self.assertFailure('capture_not_received', self.state.capture, .01)
        status = self.state.status()
        self.assertEqual(status['screen_request_timeouts'], 1)
        self.assertTrue(status['active'])
        self.assertIsNone(status['last_inactive_reason'])
        self.assertIsNone(self.state.pending)
        self.state.screen_request_timeouts = bridge.MAX_SEQUENCE
        self.assertFailure('capture_not_received', self.state.capture, .01)
        self.assertEqual(self.state.status()['screen_request_timeouts'], bridge.MAX_SEQUENCE)

    def test_cancelled_capture_is_not_counted_as_a_timeout(self):
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            pending = pool.submit(self.state.capture, .5)
            self.assertIsNotNone(self.state.poll(self.session, .2))
            self.state.session(dict(v=1, session_id=self.session, state='pause'))
            self.assertFailure('session_inactive', pending.result, 1)
        self.assertEqual(self.state.status()['screen_request_timeouts'], 0)
        self.assertEqual(self.state.status()['last_inactive_reason'], 'owner_pause')

    def test_one_request_one_frame_then_no_automatic_upload(self):
        frame = self.capture()
        self.assertEqual((frame['width'], frame['height']), (2, 3))
        self.assertIsNone(self.state.poll(self.session, 0))
        self.assertEqual(self.state.frame['jpeg'], JPEG)
        self.state.accept_frame(self.frame_headers(frame['request_id']), JPEG)  # exact retry is idempotent
        self.assertEqual(self.state.sequence, 1)

    def test_explicit_desktop_action_creates_a_separate_lease_with_recent_observation(self):
        frame = self.capture()
        self.assertFalse(self.state.allow_control)
        self.assertEqual(self.controller.calls, [])
        self.assertTrue(self.state.action(self.action(frame, button='left'))['ok'])
        control = self.state.control_identity
        self.assertNotEqual(control, self.session)
        self.assertEqual(self.controller.calls[0][0], control)
        self.assertTrue(self.state.allow_control)
        self.state.control_heartbeat()
        self.assertEqual(self.controller.heartbeats, [control])
        self.clock.now = 31
        self.assertFailure('session_inactive', self.state.action, self.action(frame, button='left'))
        self.assertFalse(self.state.allow_control)
        self.assertIn(control, self.controller.cancelled)

    def test_new_start_and_legacy_permission_fields_never_create_input(self):
        for legacy in (None, False, True):
            controller = Controller(); state = bridge.Session(controller, self.clock)
            value = dict(v=1, session_id=str(uuid.uuid4()), state='start')
            if legacy is not None: value['allow_control'] = legacy
            state.session(value)
            state.control_heartbeat()
            self.assertTrue(state.active)
            self.assertFalse(state.allow_control)
            self.assertIsNone(state.control_identity)
            self.assertEqual(controller.calls + controller.heartbeats, [])
            state.session(dict(value, allow_control=True))
            self.assertFalse(state.allow_control)
            self.assertFailure('invalid_control_permission', state.session, dict(value, allow_control=1))

    def test_screen_only_resume_uses_new_session_and_never_revives_input_permission(self):
        controller = Controller()
        state = bridge.Session(controller, self.clock)
        principal = str(uuid.uuid4())
        old_session, resumed_session = str(uuid.uuid4()), str(uuid.uuid4())

        def start(identity, control):
            state.session(dict(v=1, session_id=identity, state='start', allow_control=control),
                          principal=principal)

        def capture(identity):
            with concurrent.futures.ThreadPoolExecutor(1) as pool:
                pending = pool.submit(state.capture, .5)
                request = state.poll(identity, .2, principal=principal)
                self.assertIsNotNone(request)
                headers = dict(session=identity, request=request['request_id'], sequence='1',
                               width='2', height='3', orientation='1', **{'geometry-epoch': '1'})
                state.accept_frame(headers, JPEG, principal=principal)
                return pending.result(1), headers

        start(old_session, True)
        old_frame, old_headers = capture(old_session)
        self.assertFalse(state.status()['allow_control'])
        old_action = dict(v=1, id=str(uuid.uuid4()), session_id=old_session,
                          frame_id=old_frame['frame_id'], op='click', button='left')
        self.assertTrue(state.action(old_action)['ok'])
        old_control = state.control_identity
        state.session(dict(v=1, session_id=old_session, state='pause'), principal=principal)
        self.assertFalse(state.status()['active'])
        self.assertIsNone(state.frame)
        self.assertIn(old_session, state.closed)
        self.assertFailure('session_revoked', start, old_session, True)

        start(resumed_session, False)
        fresh_frame, _ = capture(resumed_session)
        self.assertEqual(fresh_frame['jpeg'], JPEG)
        self.assertNotEqual(fresh_frame['frame_id'], old_frame['frame_id'])
        self.assertEqual(state.status()['phone_source'], principal)
        self.assertTrue(state.status()['active'])
        self.assertFalse(state.status()['allow_control'])
        for session_id, frame_id, error in (
                (resumed_session, old_frame['frame_id'], 'observe_again'),
                (old_session, old_frame['frame_id'], 'session_inactive')):
            self.assertFailure(error, state.action,
                dict(v=1, id=str(uuid.uuid4()), session_id=session_id, frame_id=frame_id,
                     op='click', button='left'))
        self.assertFailure('session_inactive',
                           lambda: state.accept_frame(old_headers, JPEG, principal=principal))
        self.assertEqual(state.frame['frame_id'], fresh_frame['frame_id'])
        self.assertEqual(len([call for call in controller.calls if len(call) == 4]), 1)
        self.assertIn(old_control, controller.cancelled)
        self.assertTrue(state.action(dict(old_action, id=str(uuid.uuid4()), session_id=resumed_session,
                                         frame_id=fresh_frame['frame_id']))['ok'])
        self.assertNotEqual(state.control_identity, old_control)

    def test_live_phone_does_not_make_old_observations_valid(self):
        frame = self.capture(); self.clock.now = 31
        self.state.last_heartbeat = 31
        self.assertFailure('observe_again', self.state.action, self.action(frame, button='left'))

    def test_late_ancestor_end_cancels_descendant_control_under_lock_and_stops_outside(self):
        lock_states = []
        class LockCheckingController(Controller):
            def cancel(controller, identity):
                lock_states.append(('cancel', self.state.condition._is_owned()))
                super().cancel(identity)
            def stop(controller, identity):
                lock_states.append(('stop', self.state.condition._is_owned()))
                super().stop(identity)

        self.controller = LockCheckingController()
        self.state = bridge.Session(self.controller, self.clock)
        owner = str(uuid.uuid4())
        ancestor = self.session
        self.state.session(dict(v=1, session_id=ancestor, state='start'), principal=owner)
        self.state.session(dict(v=1, session_id=ancestor, state='pause'), principal=owner)
        self.session = str(uuid.uuid4())
        self.state.session(dict(v=1, session_id=self.session, state='start', resume_from=ancestor),
                           principal=owner)
        frame = self.capture()
        action = self.action(frame, button='left')
        answer = self.state.action(action)
        self.assertTrue(answer['ok'])
        control = self.state.control_identity
        self.state.session(dict(v=1, session_id=ancestor, state='end'), principal=owner)
        self.assertEqual(lock_states, [('cancel', True), ('stop', False)])
        self.assertEqual(self.controller.calls[-1], ('stop', control))
        self.assertFalse(self.state.active)
        self.assertFalse(self.state.allow_control)
        self.assertIsNone(self.state.frame)
        self.assertEqual(self.state.action(action), answer)  # Cached receipt never resends input.
        self.assertFailure('session_inactive', self.state.action, dict(action, id=str(uuid.uuid4())))
        self.assertEqual(len([call for call in self.controller.calls if len(call) == 4]), 1)

    def test_repeated_id_is_not_sent_twice_and_changed_payload_is_rejected(self):
        frame = self.capture(); action = self.action(frame, button='left')
        self.assertTrue(self.state.action(action)['ok'])
        self.assertTrue(self.state.action(action)['ok'])
        self.assertEqual(len(self.controller.calls), 1)
        action['button'] = 'right'
        self.assertFailure('command_id_changed', self.state.action, action)

    def test_unknown_input_needs_explicit_desktop_reset_then_a_new_observation(self):
        frame = self.capture(); self.controller.fail = OSError('SYNTHETIC_PRIVATE_DIAGNOSTIC')
        action = self.action(frame, button='left')
        result = self.state.action(action)
        self.assertEqual(result['status'], 'outcome_unknown')
        self.assertNotIn('SYNTHETIC_PRIVATE', json.dumps(result))
        self.assertEqual(self.state.action(action), result)
        self.assertEqual(len([call for call in self.controller.calls if len(call) == 4]), 1)
        old_control = self.state.retired_control_identity
        self.assertTrue(self.state.control_reset_required)
        self.assertTrue(self.state.active)
        self.assertFailure('control_reset_required', self.state.action, self.action(frame, button='left'))
        self.controller.fail = None
        new = self.capture(sequence=2)
        self.assertFailure('control_reset_required', self.state.action, self.action(new, button='left'))
        reset = self.reset()
        self.assertEqual(self.state.reset_control(reset), {'ok': True, 'status': 'control_reset'})
        self.assertTrue(self.state.active)
        self.assertFalse(self.state.allow_control)
        self.assertFalse(self.state.control_reset_required)
        self.assertIsNone(self.state.frame)
        self.assertFailure('observe_again', self.state.action, self.action(new, button='left'))
        fresh = self.capture(sequence=3)
        self.assertTrue(self.state.action(self.action(fresh, button='left'))['ok'])
        self.assertNotEqual(self.state.control_identity, old_control)
        before = list(self.controller.calls)
        # Neither the original uncertain action nor a delayed reset may replay.
        self.assertEqual(self.state.action(action), result)
        self.assertEqual(self.state.reset_control(reset), {'ok': True, 'status': 'control_reset'})
        self.assertEqual(self.controller.calls, before)
        self.assertTrue(self.state.allow_control)

    def test_pause_clears_ram_pending_and_control_and_cannot_resume_old_id(self):
        self.capture()
        self.state.session({'v': 1, 'session_id': self.session, 'state': 'pause'})
        self.assertIsNone(self.state.frame); self.assertFalse(self.state.active)
        self.assertFalse(self.state.allow_control)
        self.assertFailure('session_conflict', self.start)

    def test_geometry_change_invalidates_frame_and_old_context(self):
        frame = self.capture()
        self.state.session({'v': 1, 'session_id': self.session, 'state': 'geometry',
                            'geometry_epoch': 2, 'width': 3, 'height': 2, 'orientation': 6})
        self.assertIsNone(self.state.frame)
        self.assertFailure('observe_again', self.state.action, self.action(frame, button='left'))

    def test_frame_request_deadline_and_dimensions_are_enforced(self):
        self.state.pending = {'id': str(uuid.uuid4()), 'deadline': 5}
        headers = self.frame_headers(self.state.pending['id']); headers['width'] = '3'
        self.assertFailure('frame_dimensions_mismatch', self.state.accept_frame, headers, JPEG)
        headers['width'] = '2'; self.clock.now = 6
        self.assertFailure('frame_not_requested_or_expired', self.state.accept_frame, headers, JPEG)

    def test_input_bounds_and_no_shell_or_held_keys(self):
        frame = self.capture()
        for values in [self.action(frame, 'move', dx=2049, dy=0),
                       self.action(frame, 'key', usage=True, modifiers=0),
                       self.action(frame, 'shell', command='echo ignored'),
                       dict(self.action(frame, button='left'), held=True)]:
            with self.assertRaises(bridge.Failure): self.state.action(values)
        self.assertEqual(self.controller.calls, [])

    def test_old_inflight_failure_does_not_corrupt_new_session(self):
        entered, finish = threading.Event(), threading.Event()
        def delayed(*args):
            entered.set(); finish.wait(1)
            raise RuntimeError('SYNTHETIC_DRIVER_FAILURE')
        self.controller.perform = delayed
        action = self.action(self.capture(), button='left')
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            pending = pool.submit(self.state.action, action)
            self.assertTrue(entered.wait(.5))
            self.state.session({'v': 1, 'session_id': self.session, 'state': 'end'})
            self.session = str(uuid.uuid4()); self.start()
            finish.set()
            self.assertEqual(pending.result(1)['status'], 'outcome_unknown')
        self.assertFalse(self.state.unknown); self.assertEqual(self.state.commands, {})
        self.assertFalse(self.state.input_busy)

    def test_reset_rejects_stale_screen_and_keeps_command_replay_history(self):
        frame = self.capture(); action = self.action(frame, button='left')
        result = self.state.action(action)
        old_control = self.state.control_identity
        reset = self.reset()
        self.assertEqual(self.state.reset_control(reset), {'ok': True, 'status': 'control_reset'})
        self.assertIn(('stop', old_control), self.controller.calls)
        fresh = self.capture(sequence=2)
        self.state.action(self.action(fresh, button='left'))
        current = self.state.control_identity; before = list(self.controller.calls)
        self.assertEqual(self.state.action(action), result)
        self.assertEqual(self.state.reset_control(reset), {'ok': True, 'status': 'control_reset'})
        self.assertEqual(self.state.control_identity, current)
        self.assertEqual(self.controller.calls, before)
        self.assertFailure('command_id_changed', self.state.reset_control, dict(reset, session_id=None))
        self.assertFailure('session_inactive', self.state.reset_control,
                           self.reset(session_id=str(uuid.uuid4())))
        self.assertTrue(self.state.active)

    def test_reset_cannot_reuse_an_earlier_pending_capture_for_input(self):
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            pending = pool.submit(self.state.capture, .5)
            request = self.state.poll(self.session, .2)
            self.assertIsNotNone(request)
            self.state.reset_control(self.reset())
            # A pre-reset phone request may still upload for viewing. Returning
            # 409 here would unnecessarily terminate its broadcast.
            self.state.accept_frame(self.frame_headers(request['request_id']), JPEG)
            old = pending.result(1)
        self.assertEqual(old['jpeg'], JPEG)
        self.assertTrue(self.state.active)
        self.assertFailure('observe_again', self.state.action, self.action(old, button='left'))
        fresh = self.capture(sequence=2)
        self.assertTrue(self.state.action(self.action(fresh, button='left'))['ok'])

    def test_reset_cancels_inflight_input_and_old_failure_cannot_restore_fault(self):
        entered, finish = threading.Event(), threading.Event()
        self.addCleanup(finish.set)
        normal = self.controller.perform
        def delayed(*args):
            entered.set(); finish.wait(1)
            raise OSError('SYNTHETIC_OLD_INPUT')
        self.controller.perform = delayed
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            pending = pool.submit(self.state.action, self.action(self.capture(), button='left'))
            self.assertTrue(entered.wait(.5))
            old_control = self.state.control_identity
            self.state.reset_control(self.reset())
            self.assertIn(old_control, self.controller.cancelled)
            fresh = self.capture(sequence=2)
            self.assertFailure('input_in_progress', self.state.action, self.action(fresh, button='left'))
            finish.set(); self.assertFalse(pending.result(1)['ok'])
        self.assertFalse(self.state.control_reset_required)
        self.assertFalse(self.state.unknown)
        self.controller.perform = normal
        self.assertTrue(self.state.action(self.action(fresh, button='left'))['ok'])
        self.assertNotEqual(self.state.control_identity, old_control)

    def test_reset_in_progress_blocks_input_and_clears_its_intermediate_frames(self):
        self.state.action(self.action(self.capture(), button='left'))
        entered, finish = threading.Event(), threading.Event()
        self.addCleanup(finish.set)
        normal = self.controller.stop
        def delayed(identity):
            entered.set(); finish.wait(1); normal(identity)
        self.controller.stop = delayed
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            pending = pool.submit(self.state.reset_control, self.reset())
            self.assertTrue(entered.wait(.5))
            frame = self.capture(sequence=2)
            self.assertFailure('input_in_progress', self.state.action, self.action(frame, button='left'))
            finish.set(); self.assertTrue(pending.result(1)['ok'])
        self.assertFailure('observe_again', self.state.action, self.action(frame, button='left'))
        self.assertTrue(self.state.action(self.action(self.capture(sequence=3), button='left'))['ok'])

    def test_heartbeat_failure_stops_desktop_control_but_keeps_screen_live(self):
        self.state.control_heartbeat()
        self.assertEqual(self.controller.heartbeats, [])
        self.state.action(self.action(self.capture(), button='left'))
        old = self.state.control_identity
        def failure(identity): raise OSError('SYNTHETIC_USB_LOSS')
        self.controller.heartbeat = failure
        self.state.control_heartbeat()
        self.assertTrue(self.state.active)
        self.assertFalse(self.state.allow_control)
        self.assertTrue(self.state.control_reset_required)
        self.assertIn(old, self.controller.cancelled)
        frame = self.capture(sequence=2)
        self.assertEqual(frame['jpeg'], JPEG)
        self.assertFailure('control_reset_required', self.state.action, self.action(frame, button='left'))

    def test_failed_reset_is_latched_and_immutable_without_repeating_stop(self):
        self.state.action(self.action(self.capture(), button='left'))
        calls = []
        def failure(identity): calls.append(identity); raise OSError('PRIVATE_RESET_REASON')
        self.controller.stop = failure
        reset = self.reset()
        result = self.state.reset_control(reset)
        self.assertEqual(result, {'ok': False, 'status': 'control_reset_failed'})
        self.assertTrue(self.state.control_reset_required)
        self.assertEqual(self.state.reset_control(reset), result)
        self.assertEqual(len(calls), 1)
        frame = self.capture(sequence=2)
        self.assertFailure('control_reset_required', self.state.action, self.action(frame, button='left'))

    def test_neutral_release_without_a_lease_still_reaches_the_controller_without_arming(self):
        result = self.state.action(dict(v=1, id=str(uuid.uuid4()), session_id=self.session,
                                       frame_id=str(uuid.uuid4()), op='release_all'))
        self.assertTrue(result['ok'])
        self.assertEqual(self.controller.calls[0][2:], ('release_all', {}))
        self.assertIsNone(self.state.control_identity)
        self.assertFalse(self.state.allow_control)


class TLSChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.temporary.name).resolve() / 'state'
        bridge.init_state(cls.directory, '127.0.0.1', 8765)
        cls.config = bridge.configuration(cls.directory)

    @classmethod
    def tearDownClass(cls): cls.temporary.cleanup()

    def setUp(self):
        self.state = bridge.Session(Controller())
        self.server = bridge.Server(('127.0.0.1', 0), self.state, self.config['uploadToken'], self.config['agentToken'], '')
        self.authority = '127.0.0.1:' + str(self.server.server_address[1])
        self.server.authority = self.authority
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.directory / 'certificate.pem', self.directory / 'private-key.pem')
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        client = ssl.create_default_context(cafile=str(self.directory / 'certificate.pem'))
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=client))
        self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
        self.identity = str(uuid.uuid4())

    def request(self, path, role='phone', value=None, extra=None, image=False):
        headers = {'Authorization': 'Bearer ' + self.config['uploadToken' if role == 'phone' else 'agentToken']}
        data = value if image else json.dumps(value).encode() if value is not None else None
        if data is not None: headers['Content-Type'] = 'image/jpeg' if image else 'application/json'
        headers.update(extra or {})
        request = urllib.request.Request('https://' + self.authority + path, data=data, headers=headers)
        return self.opener.open(request, timeout=3)

    def test_real_tls_on_demand_roundtrip_and_no_image_files(self):
        before = {p.name for p in self.directory.iterdir()}
        with self.request('/v1/session', value={'v': 1, 'session_id': self.identity, 'state': 'start', 'allow_control': False}) as response:
            self.assertEqual(response.status, 200)
        def screen():
            with self.request('/v1/screen', role='agent', value={}) as response:
                return response.read(), dict(response.headers)
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            result = pool.submit(screen)
            with self.request('/v1/capture-request?session_id=' + self.identity) as response:
                capture = json.loads(response.read())
            headers = {'X-StreamShot-Session': self.identity, 'X-StreamShot-Request': capture['request_id'],
                       'X-StreamShot-Sequence': '1', 'X-StreamShot-Width': '2', 'X-StreamShot-Height': '3',
                       'X-StreamShot-Orientation': '1', 'X-StreamShot-Geometry-Epoch': '1'}
            with self.request('/v1/frame', value=JPEG, extra=headers, image=True) as response:
                self.assertEqual(response.status, 201)
            image, metadata = result.result(3)
        self.assertEqual(image, JPEG); self.assertEqual(metadata['Cache-Control'], 'no-store')
        self.assertEqual({p.name for p in self.directory.iterdir()}, before)

    def test_roles_origin_and_oversized_requests_are_rejected(self):
        for path, role, extra, code in [('/v1/status', 'phone', {}, 401),
                                         ('/v1/connection', 'agent', {}, 401),
                                         ('/v1/connection', 'phone', {'Origin': 'https://invalid.example'}, 403)]:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.request(path, role, extra=extra)
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn(self.config['uploadToken'].encode(), caught.exception.read())
        with self.request('/v1/connection') as response:
            self.assertEqual(json.loads(response.read())['capture'], 'on-demand')

    def test_actual_mcp_tls_screen_desktop_control_reset_and_new_lease(self):
        before = {p.name for p in self.directory.iterdir()}
        configured = dict(self.config, port=self.server.server_address[1], baseURL='https://' + self.authority)
        (self.directory / 'bridge.json').write_text(json.dumps(configured))
        server = mcp.MCP(mcp.Tools(self.directory))
        def rpc(method, identity, params=None):
            return server.handle(dict(jsonrpc='2.0', id=identity, method=method, params=params or {}))
        rpc('initialize', 1, dict(protocolVersion='2025-03-26', capabilities={},
                                 clientInfo=dict(name='synthetic-tls', version='1')))
        server.handle(dict(jsonrpc='2.0', method='notifications/initialized'))
        def call(name, identity, arguments=None):
            return rpc('tools/call', identity, dict(name=name, arguments=arguments or {}))
        def observe(sequence, identity):
            with concurrent.futures.ThreadPoolExecutor(1) as pool:
                pending = pool.submit(call, 'streamshot_get_screen', identity)
                with self.request('/v1/capture-request?session_id=' + self.identity) as response:
                    capture = json.loads(response.read())
                headers = {'X-StreamShot-Session': self.identity, 'X-StreamShot-Request': capture['request_id'],
                           'X-StreamShot-Sequence': str(sequence), 'X-StreamShot-Width': '2', 'X-StreamShot-Height': '3',
                           'X-StreamShot-Orientation': '1', 'X-StreamShot-Geometry-Epoch': '1'}
                with self.request('/v1/frame', value=JPEG, extra=headers, image=True): pass
                screen = pending.result(3)['result']
            self.assertEqual(base64.b64decode(screen['content'][0]['data']), JPEG)
            return json.loads(screen['content'][1]['text'])
        # New phones send no keyboard/mouse field at all.
        with self.request('/v1/session', value=dict(v=1, session_id=self.identity, state='start')): pass
        self.assertFalse(call('streamshot_status', 2)['result']['isError'])
        self.assertFalse(self.state.allow_control)
        self.assertEqual(self.state.controller.calls, [])
        metadata = observe(1, 3)
        self.assertEqual(self.state.controller.calls, [])
        arguments = dict(session_id=self.identity, frame_id=metadata['frame_id'], op='click', button='left')
        first = call('streamshot_control', 4, arguments)
        self.assertFalse(first['result']['isError'])
        self.assertEqual(call('streamshot_control', 4, arguments), first)
        old_control = self.state.control_identity
        self.assertNotEqual(old_control, self.identity)
        self.assertEqual(len(self.state.controller.calls), 1)
        reset = call('streamshot_reset_control', 5)
        self.assertFalse(reset['result']['isError'])
        self.assertTrue(self.state.active)
        self.assertFalse(self.state.allow_control)
        self.assertIsNone(self.state.frame)
        self.assertTrue(call('streamshot_control', 6, arguments)['result']['isError'])
        metadata = observe(2, 7)
        arguments['frame_id'] = metadata['frame_id']
        self.assertFalse(call('streamshot_control', 8, arguments)['result']['isError'])
        new_control = self.state.control_identity
        self.assertNotEqual(new_control, old_control)
        count = len(self.state.controller.calls)
        self.assertEqual(call('streamshot_reset_control', 5), reset)
        self.assertEqual(self.state.control_identity, new_control)
        self.assertEqual(len(self.state.controller.calls), count)
        self.assertFalse(call('streamshot_stop', 9)['result']['isError'])
        self.assertIsNone(self.state.frame)
        self.assertFalse(self.state.active)
        self.assertEqual({p.name for p in self.directory.iterdir()}, before)

    def test_reset_route_requires_agent_and_strict_body(self):
        with self.request('/v1/session', value=dict(v=1, session_id=self.identity, state='start', allow_control=True)): pass
        self.assertFalse(self.state.allow_control)
        body = dict(v=1, id=str(uuid.uuid4()), session_id=self.identity)
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.request('/v1/control/reset', role='phone', value=body)
        self.assertEqual(denied.exception.code, 401)
        for invalid in ({}, dict(body, extra=True), dict(body, v=True), dict(body, session_id='invalid')):
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                self.request('/v1/control/reset', role='agent', value=invalid)
            self.assertEqual(rejected.exception.code, 400)
        with self.request('/v1/control/reset', role='agent', value=body) as response:
            self.assertEqual(json.loads(response.read()), {'ok': True, 'status': 'control_reset'})
        self.assertTrue(self.state.active)
        self.assertEqual(self.state.controller.calls, [])

    def test_init_rejects_public_interface_and_existing_state(self):
        self.assertRaises(bridge.Failure, bridge.init_state, self.directory / 'public', '8.8.8.8', 8765)
        self.assertRaises(FileExistsError, bridge.init_state, self.directory, '127.0.0.1', 8765)


if __name__ == '__main__': unittest.main()
