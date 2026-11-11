"""Real SerialController methods over synthetic PTYs; no physical port or HID.

The tiny peer checks the firmware's flat ASCII v1 envelope and models arm/stop
only. Firmware parser/report/lease safety is tested separately in esp32/tests.
"""
import json
import os
from pathlib import Path
import pty
import select
import stat
import sys
import threading
import time
from types import SimpleNamespace
import tty
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import streamshot_bridge as bridge

SESSION_A = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
SESSION_B = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
BOOT_A = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
BOOT_B = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'
INPUTS = {'move', 'click', 'scroll', 'key'}


class Peer:
    """A test-only firmware peer on a new pseudoterminal, never /dev/cu.*."""
    def __init__(self, handler=None, timeout=.2):
        self.master, self.slave = pty.openpty()
        tty.setraw(self.slave)
        os.set_blocking(self.master, False)
        os.set_blocking(self.slave, False)
        # Exercise the actual constructor/state initialization while replacing
        # both physical-device metadata/open operations with this PTY fd.
        with patch.object(bridge.os, 'stat', return_value=SimpleNamespace(st_mode=stat.S_IFCHR)), \
                patch.object(bridge.os, 'open', return_value=self.slave) as opened:
            self.driver = bridge.SerialController('/dev/cu.usbmodemSYNTHETIC', timeout=timeout)
            opened.assert_called_once()
        self.handler = handler
        self.boot = BOOT_A
        self.armed = None
        self.used_sessions = set()
        self.last_seq = {}
        self.commands = []
        self.input_count = 0
        self.errors = []
        self.condition = threading.Condition()
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def close(self):
        self.closed.set()
        self.thread.join(2)
        os.close(self.slave)
        os.close(self.master)
        if self.thread.is_alive():
            raise AssertionError('synthetic peer thread did not stop')
        if self.errors:
            raise AssertionError('synthetic peer failed: ' + str(self.errors[0]))

    @staticmethod
    def validate(raw):
        if len(raw) > 512 or any(value > 126 for value in raw) or b'\\' in raw:
            raise AssertionError('noncanonical firmware wire packet')
        message = bridge.decode(raw)
        operation = message.get('op')
        fields = {'status': set(), 'arm': {'ttl_ms'}, 'heartbeat': {'ttl_ms'},
                  'move': {'dx', 'dy'}, 'click': {'button'}, 'scroll': {'dy'},
                  'key': {'usage', 'modifiers'}, 'release_all': set(), 'stop': set()}
        if operation not in fields or set(message) != {'v', 'id', 'session_id', 'seq', 'op'} | fields[operation]:
            raise AssertionError('wrong firmware fields')
        if type(message['v']) is not int or message['v'] != 1:
            raise AssertionError('wrong firmware version')
        bridge.uid(message['id']); bridge.uid(message['session_id'])
        bridge.integer(message['seq'], 1, 2147483647)
        if operation in ('arm', 'heartbeat'): bridge.integer(message['ttl_ms'], 1000, 30000)
        if operation == 'move':
            bridge.integer(message['dx'], -2048, 2048); bridge.integer(message['dy'], -2048, 2048)
        if operation == 'scroll': bridge.integer(message['dy'], -16, 16)
        if operation == 'key':
            bridge.integer(message['usage'], 4, 231); bridge.integer(message['modifiers'], 0, 255)
        if operation == 'click' and message['button'] not in ('left', 'right', 'middle'):
            raise AssertionError('wrong firmware button')
        return message

    def apply(self, message):
        session, operation = message['session_id'], message['op']
        answer = dict(v=1, id=message['id'], session_id=session, seq=message['seq'],
                      ok=True, boot_id=self.boot, connected=True, armed=False)
        if operation == 'arm':
            if session in self.used_sessions:
                answer.update(ok=False, error='session_reused')
            else:
                self.used_sessions.add(session)
                self.armed = session
        elif operation == 'stop':
            # The real firmware's safety stop neutralizes/latches the currently
            # active session, so an old queued stop must not reach a newer arm.
            self.armed = None
            self.used_sessions.add(session)
        elif operation in INPUTS or operation == 'heartbeat':
            if self.armed != session:
                answer.update(ok=False, error='not_armed')
            elif message['seq'] <= self.last_seq.get(session, 0):
                answer.update(ok=False, error='stale_seq')
            elif operation in INPUTS:
                self.input_count += 1
        if answer['ok'] and operation not in ('release_all', 'stop'):
            self.last_seq[session] = message['seq']
        answer['armed'] = self.armed == session
        return answer

    def reply(self, answer):
        if answer is None:
            return
        values = answer if isinstance(answer, list) else [answer]
        for value in values:
            encoded = value if isinstance(value, bytes) else json.dumps(value, separators=(',', ':')).encode() + b'\n'
            offset = 0
            deadline = time.monotonic() + 1
            while offset < len(encoded):
                if time.monotonic() > deadline:
                    raise AssertionError('synthetic reply could not be written')
                if select.select([], [self.master], [], .05)[1]:
                    offset += os.write(self.master, encoded[offset:])

    def serve(self):
        buffer = b''
        try:
            while not self.closed.is_set():
                if not select.select([self.master], [], [], .02)[0]:
                    continue
                chunk = os.read(self.master, 4096)
                if not chunk:
                    break
                buffer += chunk
                while b'\n' in buffer:
                    raw, buffer = buffer.split(b'\n', 1)
                    message = self.validate(raw)
                    with self.condition:
                        self.commands.append(message)
                        self.condition.notify_all()
                    answer = self.apply(message)
                    self.reply(self.handler(self, message, answer) if self.handler else answer)
        except Exception as error:
            if not self.closed.is_set():
                self.errors.append(error)


class SerialChecks(unittest.TestCase):
    def peer(self, handler=None, timeout=.2):
        value = Peer(handler=handler, timeout=timeout)
        self.addCleanup(value.close)
        return value

    def click(self, driver, session=SESSION_A):
        return driver.perform(session, str(uuid.uuid4()), 'click', {'button': 'left'})

    def worker(self, function):
        result = {}
        def run():
            try: result['value'] = function()
            except BaseException as error: result['error'] = error
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, result

    def join(self, thread):
        thread.join(2)
        self.assertFalse(thread.is_alive(), 'synthetic controller operation did not finish')

    def test_status_arm_input_heartbeat_follow_firmware_envelope_and_monotonic_seq(self):
        peer = self.peer()
        result = self.click(peer.driver)
        self.assertTrue(result['ok'])
        peer.driver.heartbeat(SESSION_A)
        peer.driver.perform(SESSION_A, str(uuid.uuid4()), 'key', {'usage': 4, 'modifiers': 2})
        self.assertEqual([item['op'] for item in peer.commands], ['status', 'arm', 'click', 'heartbeat', 'key'])
        self.assertEqual([item['seq'] for item in peer.commands], list(range(1, 6)))
        self.assertEqual(len({item['id'] for item in peer.commands}), 5)
        self.assertEqual(peer.driver.boot, BOOT_A)
        self.assertEqual(peer.driver.armed, SESSION_A)
        self.assertEqual(peer.input_count, 2)

    def test_cancel_while_status_ack_waits_prevents_later_arm_and_input(self):
        received, resume = threading.Event(), threading.Event()
        self.addCleanup(resume.set)
        def handler(peer, message, answer):
            if message['op'] == 'status':
                received.set(); resume.wait(1)
            return answer
        peer = self.peer(handler, timeout=1.5)
        performing, result = self.worker(lambda: self.click(peer.driver))
        self.assertTrue(received.wait(1))
        stopped, _ = self.worker(lambda: peer.driver.stop(SESSION_A))
        # cancel must become visible without waiting for the serial lock/ACK.
        with self.assertRaises(bridge.Failure) as failure:
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                peer.driver._check_session(SESSION_A)
                time.sleep(.001)
        self.assertEqual(failure.exception.code, 'controller_reset_required')
        resume.set(); self.join(performing); self.join(stopped)
        self.assertIsInstance(result.get('error'), bridge.Failure)
        self.assertEqual([item['op'] for item in peer.commands], ['status', 'stop'])
        self.assertEqual(peer.input_count, 0)
        self.assertFalse(peer.driver.status()['armed'])

    def test_root_session_deactivation_revokes_permission_before_waiting_for_serial(self):
        received, resume, cancelled = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(resume.set)
        def handler(peer, message, answer):
            if message['op'] == 'status':
                received.set(); resume.wait(1)
            return answer
        peer = self.peer(handler, timeout=1.5)
        state = bridge.Session(peer.driver)
        state.session(dict(v=1, session_id=SESSION_A, state='start', allow_control=True))
        frame = str(uuid.uuid4())
        state.frame = {'frame_id': frame, 'received': state.clock(), 'observation_epoch': state.observation_epoch}
        action = dict(v=1, id=str(uuid.uuid4()), session_id=SESSION_A, frame_id=frame, op='click', button='left')
        original = peer.driver.cancel
        def cancel(session):
            original(session); cancelled.set()
        peer.driver.cancel = cancel
        performing, result = self.worker(lambda: state.action(action))
        self.assertTrue(received.wait(1))
        stopping, _ = self.worker(state.stop)
        self.assertTrue(cancelled.wait(.3), 'logical cancellation waited on the USB acknowledgement')
        self.assertFalse(state.active)
        self.assertFalse(state.allow_control)
        self.assertIsNone(state.frame)
        resume.set(); self.join(performing); self.join(stopping)
        self.assertFalse(result['value']['ok'])
        self.assertEqual([item['op'] for item in peer.commands], ['status', 'stop'])
        self.assertEqual(peer.input_count, 0)

    def test_cancel_between_post_arm_check_and_assignment_cannot_resurrect_armed_metadata(self):
        arm_reply, checked, resume = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(resume.set)
        def handler(peer, message, answer):
            if message['op'] == 'arm': arm_reply.set()
            return answer
        peer = self.peer(handler, timeout=1.5)
        original = peer.driver._check_session
        paused = False
        def check(session):
            nonlocal paused
            original(session)
            if arm_reply.is_set() and not paused:
                paused = True; checked.set(); resume.wait(1)
        peer.driver._check_session = check
        performing, result = self.worker(lambda: self.click(peer.driver))
        self.assertTrue(checked.wait(1))
        cancelled = threading.Event()
        cancelling, _ = self.worker(lambda: (peer.driver.cancel(SESSION_A), cancelled.set()))
        early = cancelled.wait(.05)
        resume.set(); self.join(performing); self.join(cancelling)
        if early:
            self.assertIsInstance(result.get('error'), bridge.Failure)
        self.assertIsNone(peer.driver.armed)
        self.assertFalse(peer.driver.status()['armed'])

    def test_cancel_after_select_before_write_prevents_a_new_input_line(self):
        peer = self.peer(timeout=1.5)
        self.click(peer.driver)
        ready, resume = threading.Event(), threading.Event()
        self.addCleanup(resume.set)
        original = select.select
        paused = False
        def gated(readable, writable, exceptional, timeout=None):
            nonlocal paused
            result = original(readable, writable, exceptional, timeout)
            if writable == [peer.driver.fd] and not paused:
                paused = True; ready.set(); resume.wait(1)
            return result
        with patch.object(bridge.select, 'select', side_effect=gated):
            performing, result = self.worker(lambda: self.click(peer.driver))
            self.assertTrue(ready.wait(1))
            peer.driver.cancel(SESSION_A)
            resume.set(); self.join(performing)
        self.assertIsInstance(result.get('error'), bridge.Failure)
        self.assertEqual(peer.input_count, 1)
        self.assertEqual([item['op'] for item in peer.commands].count('click'), 1)

    def test_unknown_ack_id_or_no_ack_never_replays_input(self):
        for unknown in (True, False):
            def handler(peer, message, answer):
                if message['op'] == 'click':
                    return dict(answer, id=str(uuid.uuid4())) if unknown else None
                return answer
            peer = self.peer(handler, timeout=.08)
            with self.assertRaises(bridge.Failure) as failure:
                self.click(peer.driver)
            self.assertEqual(failure.exception.code, 'controller_outcome_unknown')
            self.assertEqual(peer.input_count, 1)
            self.assertEqual([item['op'] for item in peer.commands], ['status', 'arm', 'click', 'stop'])

    def test_unknown_input_revokes_old_session_and_requires_new_phone_uuid(self):
        def handler(peer, message, answer):
            if message['op'] == 'click' and message['session_id'] == SESSION_A: return None
            return answer
        peer = self.peer(handler, timeout=.08)
        with self.assertRaises(bridge.Failure) as failure:
            self.click(peer.driver)
        self.assertEqual(failure.exception.code, 'controller_outcome_unknown')
        self.assertIsNone(peer.driver.armed)
        self.assertIsNone(peer.armed, 'unknown input must make one best-effort neutral stop')
        self.assertIn(SESSION_A, peer.driver.blocked)
        before = len(peer.commands)
        with self.assertRaises(bridge.Failure) as failure:
            self.click(peer.driver)
        self.assertEqual(failure.exception.code, 'controller_reset_required')
        peer.driver.heartbeat(SESSION_A)
        self.assertEqual(len(peer.commands), before)
        # A newly owner-started phone UUID may arm; no old request is replayed.
        self.assertTrue(self.click(peer.driver, SESSION_B)['ok'])
        self.assertEqual(peer.input_count, 2)

    def test_unknown_input_is_not_retried_when_its_best_effort_stop_also_times_out(self):
        def handler(peer, message, answer):
            return None if message['op'] in ('click', 'stop') else answer
        peer = self.peer(handler, timeout=.08)
        with self.assertRaises(bridge.Failure) as failure: self.click(peer.driver)
        self.assertEqual(failure.exception.code, 'controller_outcome_unknown')
        self.assertEqual([item['op'] for item in peer.commands], ['status', 'arm', 'click', 'stop'])
        self.assertEqual(peer.input_count, 1)
        self.assertIn(SESSION_A, peer.driver.blocked)
        self.assertFalse(peer.driver.status()['armed'])

    def test_root_session_caches_unknown_outcome_without_sending_same_id_again(self):
        drop = [True]
        def handler(peer, message, answer):
            return None if message['op'] == 'click' and drop[0] else answer
        peer = self.peer(handler, timeout=.08)
        state = bridge.Session(peer.driver)
        state.session(dict(v=1, session_id=SESSION_A, state='start', allow_control=True))
        frame = str(uuid.uuid4())
        state.frame = {'frame_id': frame, 'received': state.clock(), 'observation_epoch': state.observation_epoch}
        action = dict(v=1, id=str(uuid.uuid4()), session_id=SESSION_A, frame_id=frame, op='click', button='left')
        first = state.action(action)
        self.assertEqual(first['status'], 'outcome_unknown')
        self.assertEqual(state.action(action), first)
        self.assertEqual(peer.input_count, 1)
        self.assertEqual([item['op'] for item in peer.commands], ['status', 'arm', 'click', 'stop'])
        with self.assertRaises(bridge.Failure) as failure:
            state.action(dict(action, id=str(uuid.uuid4())))
        self.assertEqual(failure.exception.code, 'control_reset_required')
        self.assertTrue(state.active)
        old_control = state.retired_control_identity
        self.assertNotEqual(old_control, SESSION_A)
        # A new screenshot alone cannot clear a hardware fault.
        state.frame = {'frame_id': str(uuid.uuid4()), 'received': state.clock(),
                       'observation_epoch': state.observation_epoch}
        with self.assertRaises(bridge.Failure):
            state.action(dict(action, id=str(uuid.uuid4()), frame_id=state.frame['frame_id']))
        reset = dict(v=1, id=str(uuid.uuid4()), session_id=SESSION_A)
        # The synthetic /dev/cu.* name exists only through constructor mocks;
        # explicit reset now verifies its device identity again before I/O.
        with patch.object(bridge.os, 'stat', return_value=os.fstat(peer.driver.fd)):
            self.assertTrue(state.reset_control(reset)['ok'])
        self.assertTrue(state.active)
        self.assertFalse(state.allow_control)
        drop[0] = False
        state.frame = {'frame_id': str(uuid.uuid4()), 'received': state.clock(),
                       'observation_epoch': state.observation_epoch}
        self.assertTrue(state.action(dict(action, id=str(uuid.uuid4()), frame_id=state.frame['frame_id']))['ok'])
        self.assertNotEqual(state.control_identity, old_control)
        self.assertEqual(state.identity, SESSION_A)
        self.assertEqual(peer.input_count, 2)
        count = len(peer.commands)
        self.assertEqual(state.action(action), first)
        self.assertTrue(state.reset_control(reset)['ok'])
        self.assertEqual(len(peer.commands), count)

    def test_boot_change_blocks_old_session_until_new_phone_session(self):
        peer = self.peer()
        self.click(peer.driver)
        peer.boot = BOOT_B; peer.armed = None; peer.used_sessions.clear(); peer.last_seq.clear()
        with self.assertRaises(bridge.Failure) as failure:
            self.click(peer.driver)
        self.assertEqual(failure.exception.code, 'controller_restarted_reset_required')
        self.assertEqual(peer.driver.boot, BOOT_B)
        count = len(peer.commands)
        with self.assertRaises(bridge.Failure): self.click(peer.driver)
        self.assertEqual(len(peer.commands), count)
        self.assertFalse(peer.driver.status()['armed'])
        self.assertTrue(self.click(peer.driver, SESSION_B)['ok'])

    def test_boot_change_during_handoff_clears_old_arm_and_blocks_both_old_sessions(self):
        peer = self.peer()
        self.click(peer.driver)
        peer.boot = BOOT_B; peer.armed = None; peer.used_sessions.clear(); peer.last_seq.clear()
        with self.assertRaises(bridge.Failure) as failure:
            self.click(peer.driver, SESSION_B)
        self.assertEqual(failure.exception.code, 'controller_restarted_reset_required')
        self.assertIsNone(peer.driver.armed)
        before = len(peer.commands)
        for session in (SESSION_A, SESSION_B):
            with self.assertRaises(bridge.Failure): self.click(peer.driver, session)
        self.assertEqual(len(peer.commands), before)
        self.assertTrue(self.click(peer.driver, str(uuid.uuid4()))['ok'])

    def test_neutral_is_allowed_unarmed_cancelled_and_does_not_arm(self):
        peer = self.peer()
        peer.driver.cancel(SESSION_A)
        result = peer.driver.perform(SESSION_A, str(uuid.uuid4()), 'release_all', {})
        self.assertTrue(result['ok'])
        self.assertEqual([item['op'] for item in peer.commands], ['release_all'])
        self.assertIsNone(peer.driver.armed)
        self.assertEqual(peer.input_count, 0)

    def test_old_session_stop_cannot_neutralize_a_new_session_arm(self):
        peer = self.peer()
        self.click(peer.driver, SESSION_B)
        before = len(peer.commands)
        peer.driver.stop(SESSION_A)
        self.assertEqual(len(peer.commands), before, 'old stop must not reach the firmware after a new session takes over')
        self.assertEqual(peer.driver.armed, SESSION_B)
        self.assertEqual(peer.armed, SESSION_B)

    def test_failed_old_stop_cannot_clear_new_session_metadata(self):
        def handler(peer, message, answer):
            if message['op'] == 'stop' and message['session_id'] == SESSION_A: return None
            return answer
        peer = self.peer(handler, timeout=.08)
        self.click(peer.driver, SESSION_B)
        peer.driver.stop(SESSION_A)
        self.assertEqual(peer.driver.armed, SESSION_B)
        self.assertTrue(peer.driver.status()['armed'])

    def test_successful_but_unarmed_arm_ack_must_not_send_input(self):
        def handler(peer, message, answer):
            if message['op'] == 'arm':
                peer.armed = None
                return dict(answer, armed=False)
            return answer
        peer = self.peer(handler)
        with self.assertRaises(bridge.Failure): self.click(peer.driver)
        self.assertEqual([item['op'] for item in peer.commands], ['status', 'arm'])
        self.assertEqual(peer.input_count, 0)
        self.assertFalse(peer.driver.status()['armed'])

    def test_malformed_matching_ack_is_rejected_without_replay(self):
        for field, bad in (('session_id', SESSION_B), ('seq', 999), ('ok', 1)):
            def handler(peer, message, answer):
                return dict(answer, **{field: bad}) if message['op'] == 'click' else answer
            peer = self.peer(handler)
            with self.assertRaises(bridge.Failure) as failure: self.click(peer.driver)
            self.assertEqual(failure.exception.code, 'controller_invalid_response')
            self.assertEqual(peer.input_count, 1)

    def test_boolean_version_or_sequence_is_not_an_integer_ack(self):
        for field in ('v', 'seq'):
            def handler(peer, message, answer):
                return dict(answer, **{field: True}) if message['op'] == 'status' else answer
            peer = self.peer(handler)
            with self.assertRaises(bridge.Failure) as failure:
                self.click(peer.driver)
            self.assertEqual(failure.exception.code, 'controller_invalid_response')
            self.assertEqual([item['op'] for item in peer.commands], ['status', 'stop'])
            self.assertIn(SESSION_A, peer.driver.blocked)
            self.assertEqual(peer.input_count, 0)

    def test_sequence_exhaustion_prevents_any_serial_write(self):
        peer = self.peer()
        peer.driver.sequence = bridge.MAX_SEQUENCE
        with self.assertRaises(bridge.Failure) as failure:
            peer.driver.perform(SESSION_A, str(uuid.uuid4()), 'release_all', {})
        self.assertEqual(failure.exception.code, 'serial_sequence_exhausted')
        self.assertEqual(peer.commands, [])


if __name__ == '__main__':
    unittest.main()
