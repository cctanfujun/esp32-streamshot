#!/usr/bin/env python3
"""Local, on-demand ReplayKit -> MCP -> USB/BLE bridge. No model service.

Owner-started phone sessions share images; explicit desktop requests grant input.
Images stay in bounded memory;
there is no continuous upload, image archive, arbitrary URL proxy or shell tool.
"""
import argparse
import base64
import contextlib
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import select
import socket
import ssl
import stat
import subprocess
import sys
import termios
import threading
import time
import tty
import urllib.error
import urllib.parse
import urllib.request
import uuid

STATE = Path.home() / 'Library/Application Support/start-1-dollar/streamshot-ai-link'
MAX_IMAGE = 1024 * 1024
MAX_JSON = 4096
MAX_SEQUENCE = 2147483647
SCREEN_RECOVERY_PROTOCOL = 'resume-from-v1'
CAPTURE_PROTOCOL = 'settle-ticket-v1'
MAX_SETTLE_MS = 1000
FRAME_TIMINGS = {'capture-wait-ms': 'capture_wait_ms', 'encode-ms': 'encode_ms',
                 'callback-age-ms': 'source_callback_age_ms'}
SCREEN_INACTIVE_REASONS = frozenset(('owner_pause', 'owner_end', 'agent_stop',
                                   'heartbeat_timeout', 'lease_expired', 'bridge_shutdown'))


class Failure(Exception):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.code, self.status = code, status


def decode(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise Failure('duplicate_field')
            result[key] = value
        return result
    try:
        return json.loads(data, object_pairs_hook=unique)
    except (ValueError, TypeError, UnicodeError):
        raise Failure('invalid_json') from None


def uid(value):
    if not isinstance(value, str):
        raise Failure('invalid_id')
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise Failure('invalid_id') from None
    if str(parsed) != value:
        raise Failure('invalid_id')
    return value


def integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise Failure('invalid_integer')
    return value


def exact(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise Failure('invalid_fields')


def jpeg_size(data):
    """Read bounded SOF dimensions, without decompressing or writing an image."""
    if not 4 <= len(data) <= MAX_IMAGE or not data.startswith(b'\xff\xd8') or not data.endswith(b'\xff\xd9'):
        raise Failure('invalid_jpeg')
    offset = 2
    while offset + 4 <= len(data):
        if data[offset] != 255:
            raise Failure('invalid_jpeg')
        while offset < len(data) and data[offset] == 255:
            offset += 1
        marker = data[offset]; offset += 1
        if marker in (0xDA, 0xD9):
            break
        if marker in range(0xD0, 0xD9) or marker == 1:
            continue
        length = int.from_bytes(data[offset:offset + 2], 'big')
        if length < 2 or offset + length > len(data):
            raise Failure('invalid_jpeg')
        if marker in (0xC0, 0xC1, 0xC2):
            if length < 8:
                raise Failure('invalid_jpeg')
            return (integer(int.from_bytes(data[offset + 5:offset + 7], 'big'), 1, 8192),
                    integer(int.from_bytes(data[offset + 3:offset + 5], 'big'), 1, 8192))
        offset += length
    raise Failure('invalid_jpeg')


def action_arguments(value):
    op = value.get('op') if isinstance(value, dict) else None
    fields = {'move': {'dx', 'dy'}, 'click': {'button'}, 'scroll': {'dy'},
              'key': {'usage', 'modifiers'}, 'release_all': set()}
    if not isinstance(op, str) or op not in fields:
        raise Failure('unsupported_operation')
    exact(value, {'v', 'id', 'session_id', 'frame_id', 'op'} | fields[op])
    if type(value['v']) is not int or value['v'] != 1:
        raise Failure('unsupported_version')
    uid(value['id']); uid(value['session_id']); uid(value['frame_id'])
    args = {key: value[key] for key in fields[op]}
    if op == 'move':
        integer(args['dx'], -2048, 2048); integer(args['dy'], -2048, 2048)
    elif op == 'scroll':
        integer(args['dy'], -16, 16)
    elif op == 'key':
        integer(args['usage'], 4, 231); integer(args['modifiers'], 0, 255)
    elif op == 'click' and args['button'] not in ('left', 'right', 'middle'):
        raise Failure('invalid_button')
    return op, args


def reset_arguments(value):
    exact(value, {'v', 'id', 'session_id'})
    if type(value['v']) is not int or value['v'] != 1:
        raise Failure('unsupported_version')
    uid(value['id'])
    if value['session_id'] is not None:
        uid(value['session_id'])


def screen_arguments(value):
    if not isinstance(value, dict) or set(value) not in (set(), {'settle_ms'}):
        raise Failure('invalid_fields')
    return integer(value.get('settle_ms', 0), 0, MAX_SETTLE_MS)


class UnavailableController:
    def perform(self, session, identity, operation, arguments):
        raise Failure('controller_unavailable', 503)

    def stop(self, session):
        pass

    def cancel(self, session):
        pass

    def heartbeat(self, session):
        pass

    def status(self):
        return {'available': False, 'armed': False}


class SerialController:
    """One explicit character device, bounded NDJSON and no automatic replay."""
    def __init__(self, port, timeout=2):
        if not re.fullmatch(r'/dev/(?:cu\.(?:usbmodem|usbserial)[A-Za-z0-9_.-]*|tty(?:ACM|USB)[0-9]+)', port):
            raise Failure('serial_port_not_allowed')
        if not stat.S_ISCHR(os.stat(port).st_mode):
            raise Failure('serial_port_not_allowed')
        self.fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        self.serial_port = port
        tty.setraw(self.fd)
        options = termios.tcgetattr(self.fd)
        options[4] = options[5] = termios.B115200
        termios.tcsetattr(self.fd, termios.TCSANOW, options)
        self.timeout = timeout
        self.lock = threading.RLock()
        self.state_lock = threading.RLock()
        self.sequence = 0
        self.boot = None
        self.armed = None
        self.current_session = None
        self.connected = None
        self.blocked = set()
        self.stop_attempted = set()
        self.cancelled = {}
        self.buffer = b''

    def _command(self, session, identity, operation, arguments):
        guarded = operation not in ('status', 'release_all', 'stop')
        if guarded:
            self._check_session(session)
        self.sequence += 1
        if self.sequence > MAX_SEQUENCE:
            raise Failure('serial_sequence_exhausted', 409)
        message = dict(v=1, id=identity, session_id=session, seq=self.sequence, op=operation, **arguments)
        line = json.dumps(message, separators=(',', ':')).encode() + b'\n'
        if len(line) > 512:
            raise Failure('serial_message_too_large')
        deadline = time.monotonic() + self.timeout
        offset = 0
        try:
            while offset < len(line):
                if guarded:
                    self._check_session(session)
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([], [self.fd], [], remaining)[1]:
                    raise Failure('controller_outcome_unknown', 503)
                if guarded:
                    # Cancellation can arrive while select is waiting. Commit
                    # only this nonblocking write under the short state lock.
                    with self.state_lock:
                        self._check_session(session)
                        offset += os.write(self.fd, line[offset:])
                else:
                    offset += os.write(self.fd, line[offset:])
            consumed = 0
            while time.monotonic() < deadline:
                while b'\n' in self.buffer:
                    raw, self.buffer = self.buffer.split(b'\n', 1)
                    if len(raw) > 512:
                        continue
                    try:
                        answer = decode(raw)
                    except Failure:
                        continue
                    if not isinstance(answer, dict) or answer.get('id') != identity:
                        continue
                    if (type(answer.get('v')) is not int or answer['v'] != 1
                            or answer.get('session_id') != session
                            or type(answer.get('seq')) is not int or answer['seq'] != self.sequence
                            or any(type(answer.get(key)) is not bool for key in ('ok', 'armed', 'connected'))):
                        raise Failure('controller_invalid_response', 503)
                    try:
                        boot = uid(answer.get('boot_id'))
                    except Failure:
                        raise Failure('controller_invalid_response', 503) from None
                    if self.boot is not None and self.boot != boot:
                        # A boot change invalidates every old phone grant, not
                        # just the request which happened to discover it.
                        with self.state_lock:
                            for old in set(self.cancelled) | {session, self.armed, self.current_session}:
                                if old is not None:
                                    self.cancel(old)
                            self.armed = self.current_session = None
                            self.boot = boot
                        raise Failure('controller_restarted_reset_required', 409)
                    self.boot = boot
                    self.connected = answer.get('connected') is True
                    if not answer['ok']:
                        self.armed = None
                        raise Failure('controller_rejected', 503)
                    return answer
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([self.fd], [], [], remaining)[0]:
                    break
                received = os.read(self.fd, 1024)
                if not received:
                    break
                consumed += len(received)
                if consumed > 16384:
                    break
                self.buffer = (self.buffer + received)[-4096:]
        except Failure as error:
            if error.code == 'controller_invalid_response':
                self._unknown(session, operation)
            raise
        except OSError:
            self._unknown(session, operation)
            raise Failure('controller_outcome_unknown', 503) from None
        self._unknown(session, operation)
        raise Failure('controller_outcome_unknown', 503)

    def _unknown(self, session, operation):
        # Do not guess whether an unacknowledged report executed or which arm
        # remains live on the accessory. Neutralize once; the desktop must
        # explicitly reset control before issuing input under a new lease.
        self.cancel(session)
        if operation != 'stop':
            self.stop(session)

    def perform(self, session, identity, operation, arguments):
        with self.lock:
            if operation == 'release_all':
                return self._command(session, identity, operation, {})
            with self.state_lock:
                self._check_session(session)
                self.current_session = session
            if self.armed != session:
                state = self._command(session, str(uuid.uuid4()), 'status', {})
                if state.get('connected') is not True:
                    raise Failure('controller_not_paired', 503)
                self._check_session(session)
                answer = self._command(session, str(uuid.uuid4()), 'arm', {'ttl_ms': 30000})
                if answer.get('armed') is not True or answer.get('connected') is not True:
                    self.cancel(session)
                    raise Failure('controller_not_ready', 503)
                with self.state_lock:
                    self._check_session(session)
                    self.armed = session
            self._check_session(session)
            return self._command(session, identity, operation, arguments)

    def _check_session(self, session):
        with self.state_lock:
            if session in self.blocked or self.cancelled.setdefault(session, threading.Event()).is_set():
                raise Failure('controller_reset_required', 409)

    def cancel(self, session):
        # Never wait for serial I/O to revoke permission. A status/arm already
        # waiting on USB must recheck this flag before its next mutating write.
        with self.state_lock:
            self.blocked.add(session)
            self.cancelled.setdefault(session, threading.Event()).set()
            if self.armed == session:
                self.armed = None

    def heartbeat(self, session):
        with self.lock:
            if self.armed == session:
                self._command(session, str(uuid.uuid4()), 'heartbeat', {'ttl_ms': 30000})

    def stop(self, session, retry=False):
        self.cancel(session)
        with self.lock:
            with self.state_lock:
                if self.current_session is not None and self.current_session != session:
                    return  # Old A must not send a global neutral/stop to new B.
                if session in self.stop_attempted and not retry:
                    return
                self.stop_attempted.add(session)
            with contextlib.suppress(Failure):
                self._command(session, str(uuid.uuid4()), 'stop', {})
            with self.state_lock:
                if self.current_session == session:
                    self.armed = self.current_session = None

    def reset(self, session):
        # Only an explicit desktop reset can reopen the selected USB device.
        # Re-enumeration can revoke an fd while retaining the same /dev name.
        self.cancel(session)
        with self.lock:
            with self.state_lock:
                if self.current_session is not None and self.current_session != session:
                    raise Failure('controller_reset_required', 409)
            replacement = None
            try:
                selected = os.stat(self.serial_port)
                if not stat.S_ISCHR(selected.st_mode):
                    raise OSError('selected serial device unavailable')
                try:
                    opened = os.fstat(self.fd)
                    changed = ((selected.st_dev, selected.st_ino, selected.st_rdev)
                               != (opened.st_dev, opened.st_ino, opened.st_rdev))
                except OSError:
                    changed = True
                if changed:
                    replacement = os.open(self.serial_port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
                    tty.setraw(replacement)
                    options = termios.tcgetattr(replacement)
                    options[4] = options[5] = termios.B115200
                    termios.tcsetattr(replacement, termios.TCSANOW, options)
                    with contextlib.suppress(OSError):
                        os.close(self.fd)
                    self.fd, replacement = replacement, None
                    self.buffer = b''
                try:
                    self._command(session, str(uuid.uuid4()), 'status', {})
                except Failure as error:
                    # A valid status ACK has already recorded the new boot and
                    # retired old sessions. Still require a confirmed stop.
                    if error.code != 'controller_restarted_reset_required':
                        raise
                self.stop_attempted.add(session)
                answer = self._command(session, str(uuid.uuid4()), 'stop', {})
                if answer.get('armed') is not False:
                    raise Failure('controller_not_ready', 503)
                with self.state_lock:
                    self.armed = self.current_session = None
            except OSError:
                raise Failure('controller_unavailable', 503) from None
            finally:
                if replacement is not None:
                    os.close(replacement)

    def status(self):
        with self.state_lock:
            return {'available': True, 'armed': self.armed is not None, 'connected': self.connected,
                    'serial_port': self.serial_port}


class Session:
    def __init__(self, controller=None, clock=time.monotonic):
        self.controller = controller or UnavailableController()
        self.clock = clock
        self.condition = threading.Condition(threading.RLock())
        self.identity = None
        self.owner = None
        self.closed_owners = {}
        self.closed_reasons = {}
        self.resume_successors = {}
        self.active = False
        # Screen sharing never grants input. A separate desktop lease is
        # created only by an authenticated, observed action request.
        self.allow_control = False
        self.control_identity = None
        self.retired_control_identity = None
        self.control_generation = 0
        self.control_reset_required = False
        self.control_reset_busy = False
        self.control_resets = {}
        self.geometry = None
        self.frame = None
        self.observation_epoch = 0
        self.sequence = 0
        self.pending = None
        self.result = None
        self.capture_busy = False
        self.phone_capture_protocol = None
        self.closed = set()
        self.commands = {}
        self.input_busy = False
        self.unknown = False
        self.last_heartbeat = self.started = 0
        self.last_inactive_reason = None
        self.screen_request_timeouts = 0

    def _detach_control(self, reset_required=False):
        previous = self.control_identity
        if previous:
            getattr(self.controller, 'cancel', lambda identity: None)(previous)
            self.retired_control_identity = previous
        self.control_identity = None
        self.allow_control = False
        self.control_reset_required = reset_required
        self.control_generation += 1
        return previous

    def _deactivate(self, reason):
        if reason not in SCREEN_INACTIVE_REASONS:
            raise ValueError('invalid_screen_inactive_reason')
        if self.active:
            self.last_inactive_reason = reason
        previous = self.identity
        if previous:
            self.closed.add(previous)
            self.closed_owners[previous] = self.owner
            if self.active or reason in ('agent_stop', 'bridge_shutdown'):
                # A desktop stop while the phone is already paused must also
                # prevent the next automatic continuation of that old run.
                self.closed_reasons[previous] = reason
        control = self._detach_control()
        self.active = False
        self.frame = self.pending = self.result = None
        self.condition.notify_all()
        return control

    def _expire(self):
        now = self.clock()
        if self.active and (now - self.last_heartbeat > 20 or now - self.started > 1800):
            reason = 'lease_expired' if now - self.started > 1800 else 'heartbeat_timeout'
            old = self._deactivate(reason)
            if old:
                threading.Thread(target=self.controller.stop, args=(old,), daemon=True).start()
        if self.frame and now - self.frame['received'] > 30:
            self.frame = None

    def _inactive_phone_failure(self, identity, principal):
        """Disclose a recovery disposition only to the old screen's owner."""
        if principal is None or identity not in self.closed:
            return
        if self.closed_owners.get(identity) != principal:
            raise Failure('session_owner_mismatch', 403)
        if self.active:
            return  # An old run cannot infer/recover over a newer live screen.
        reason = self.closed_reasons.get(identity)
        if reason == 'heartbeat_timeout':
            raise Failure('session_heartbeat_expired', 409)
        if reason in SCREEN_INACTIVE_REASONS:
            raise Failure('session_revoked', 409)

    def _resume_predecessor(self, previous, identity, principal, replay=False):
        if previous not in self.closed:
            raise Failure('session_inactive', 409)
        if self.closed_owners.get(previous) != principal:
            raise Failure('session_owner_mismatch', 403)
        if previous not in self.closed_reasons:
            raise Failure('session_inactive', 409)
        successor = self.resume_successors.get(previous)
        if replay:
            if successor != identity:
                raise Failure('session_revoked', 409)
            return
        if successor is not None:
            raise Failure('session_revoked', 409)
        if self.closed_reasons[previous] not in ('owner_pause', 'heartbeat_timeout'):
            raise Failure('session_revoked', 409)

    def _end_closed_lineage(self, identity, principal):
        # A delayed owner end also revokes the already-created continuation of
        # this run. Follow only its bounded successor chain; an unrelated manual
        # broadcast, even by the same phone, must remain active.
        old = None
        seen = set()
        while identity is not None and identity not in seen:
            seen.add(identity)
            if self.active and self.identity == identity and self.owner == principal:
                old = self._deactivate('owner_end')
            elif identity in self.closed and self.closed_owners.get(identity) == principal:
                if self.closed_reasons.get(identity) in ('owner_pause', 'heartbeat_timeout'):
                    self.closed_reasons[identity] = 'owner_end'
                    if self.identity == identity and not self.active:
                        self.last_inactive_reason = 'owner_end'
            else:
                break
            identity = self.resume_successors.get(identity)
        return old

    def _require(self, identity, principal=None):
        self._expire()
        if not self.active or self.identity != identity:
            self._inactive_phone_failure(identity, principal)
            raise Failure('session_inactive', 409)
        if principal is not None and self.owner != principal:
            raise Failure('session_owner_mismatch', 403)

    def session(self, value, principal=None):
        if not isinstance(value, dict):
            raise Failure('invalid_fields')
        state = value.get('state')
        fields = {'v', 'session_id', 'state'}
        if state == 'start':
            # Older phones may still send this field. It is strictly parsed
            # for compatibility, but cannot authorize desktop input.
            if 'allow_control' in value:
                fields |= {'allow_control'}
            if 'resume_from' in value:
                fields |= {'resume_from'}
            if 'capture_protocol' in value:
                fields |= {'capture_protocol'}
        elif state == 'geometry': fields |= {'geometry_epoch', 'width', 'height', 'orientation'}
        elif state not in ('pause', 'end'): raise Failure('invalid_session_state')
        exact(value, fields); uid(value['session_id'])
        if type(value['v']) is not int or value['v'] != 1:
            raise Failure('unsupported_version')
        previous = value.get('resume_from')
        if previous is not None:
            uid(previous)
            if previous == value['session_id']:
                raise Failure('invalid_resume_from')
        elif 'resume_from' in value:
            raise Failure('invalid_resume_from')
        old = None
        with self.condition:
            self._expire()
            identity = value['session_id']
            if state == 'start':
                if 'allow_control' in value and type(value['allow_control']) is not bool:
                    raise Failure('invalid_control_permission')
                capture_protocol = value.get('capture_protocol')
                if 'capture_protocol' in value and capture_protocol != CAPTURE_PROTOCOL:
                    raise Failure('unsupported_capture_protocol')
                if identity in self.closed:
                    self._inactive_phone_failure(identity, principal)
                    raise Failure('session_conflict', 409)
                if self.active and self.identity != identity:
                    raise Failure('session_conflict', 409)
                if self.active:
                    if self.owner != principal:
                        raise Failure('session_owner_mismatch', 403)
                    if capture_protocol != self.phone_capture_protocol:
                        raise Failure('capture_protocol_changed', 409)
                    if previous is not None:
                        self._resume_predecessor(previous, identity, principal, replay=True)
                    return
                if previous is not None:
                    self._resume_predecessor(previous, identity, principal)
                if len(self.closed) >= 256:
                    raise Failure('session_history_full_restart_bridge', 409)
                if previous is not None:
                    self.resume_successors[previous] = identity
                self.identity, self.active = identity, True
                self.owner = principal
                self.phone_capture_protocol = capture_protocol
                self.allow_control = False
                self.control_identity = None
                self.control_reset_required = False
                self.control_generation += 1
                self.geometry = self.frame = self.pending = self.result = None
                self.sequence = 0; self.commands = {}; self.unknown = False
                self.last_heartbeat = self.started = self.clock()
            elif state in ('pause', 'end'):
                if identity in self.closed:
                    if principal is not None and self.closed_owners.get(identity) != principal:
                        raise Failure('session_owner_mismatch', 403)
                    if principal is not None:
                        reason = self.closed_reasons.get(identity)
                        if state == 'end' and reason in ('owner_pause', 'heartbeat_timeout', 'owner_end'):
                            old = self._end_closed_lineage(identity, principal)
                        elif state == 'pause' and reason == 'owner_pause':
                            return
                        else:
                            self._inactive_phone_failure(identity, principal)
                            raise Failure('session_inactive', 409)
                    else:
                        return
                else:
                    self._require(identity, principal)
                    old = self._deactivate('owner_' + state)
            else:
                self._require(identity, principal)
                geometry = (integer(value['geometry_epoch'], 1, MAX_SEQUENCE), integer(value['width'], 1, 8192),
                            integer(value['height'], 1, 8192), integer(value['orientation'], 1, 8))
                if self.geometry and geometry != self.geometry:
                    if geometry[0] <= self.geometry[0]:
                        raise Failure('stale_geometry', 409)
                    self.frame = self.pending = self.result = None
                self.geometry = geometry
                self.last_heartbeat = self.clock()
                self.condition.notify_all()
        if old:
            self.controller.stop(old)

    def poll(self, identity, timeout=8, principal=None, outcome=None):
        uid(identity)
        if outcome is not None:
            exact(outcome, {'request_id', 'status'})
            uid(outcome['request_id'])
            if outcome['status'] not in ('no_video_sample', 'ticket_expired'):
                raise Failure('invalid_capture_outcome')
        with self.condition:
            self._require(identity, principal)
            self.last_heartbeat = self.clock()
            if (outcome is not None and self.pending is not None
                    and self.pending['id'] == outcome['request_id']):
                self.result = {'capture_error': 'capture_' + outcome['status']}
                self.pending = None
                self.condition.notify_all()
                return None
            self.condition.wait_for(lambda: self.pending is not None or not self.active, timeout)
            self._require(identity, principal)
            self.last_heartbeat = self.clock()
            if self.pending and self.pending['deadline'] > self.clock():
                ttl = min(5000, max(1, int((self.pending['deadline'] - self.clock()) * 1000)))
                result = {'v': 1, 'session_id': identity, 'request_id': self.pending['id'], 'ttl_ms': ttl}
                if self.phone_capture_protocol == CAPTURE_PROTOCOL:
                    result['settle_ms'] = min(self.pending.get('settle_ms', 0), ttl)
                return result
            return None

    def accept_frame(self, values, data, principal=None):
        expected = {'session', 'request', 'sequence', 'width', 'height', 'orientation', 'geometry-epoch'}
        if not isinstance(values, dict) or set(values) - expected - set(FRAME_TIMINGS) or not expected <= set(values):
            raise Failure('invalid_fields')
        timings = {}
        for name, field in FRAME_TIMINGS.items():
            if name in values:
                if (not isinstance(values[name], str) or not re.fullmatch(r'[0-9]{1,4}', values[name])
                        or int(values[name]) > 5000):
                    raise Failure('invalid_frame_timings')
                timings[field] = int(values[name])
        identity, request = uid(values['session']), uid(values['request'])
        parsed = {}
        for key in expected - {'session', 'request'}:
            if not isinstance(values[key], str) or not re.fullmatch(r'[0-9]{1,10}', values[key]):
                raise Failure('invalid_frame_headers')
            parsed[key] = int(values[key])
        sequence = integer(parsed['sequence'], 1, MAX_SEQUENCE)
        geometry = (integer(parsed['geometry-epoch'], 1, MAX_SEQUENCE), integer(parsed['width'], 1, 8192),
                    integer(parsed['height'], 1, 8192), integer(parsed['orientation'], 1, 8))
        if jpeg_size(data) != geometry[1:3]:
            raise Failure('frame_dimensions_mismatch')
        digest = hashlib.sha256(data).hexdigest()
        with self.condition:
            self._require(identity, principal)
            if ((self.phone_capture_protocol == CAPTURE_PROTOCOL and len(timings) != len(FRAME_TIMINGS))
                    or (self.phone_capture_protocol is None and timings)):
                raise Failure('invalid_frame_timings')
            if self.frame and request == self.frame['request_id']:
                if sequence == self.frame['sequence'] and digest == self.frame['digest']:
                    return
                raise Failure('duplicate_frame_changed', 409)
            if (not self.pending or self.pending['id'] != request or self.pending['deadline'] < self.clock()
                    or sequence <= self.sequence):
                raise Failure('frame_not_requested_or_expired', 409)
            if self.geometry is not None and geometry != self.geometry:
                raise Failure('geometry_changed_request_again', 409)
            self.geometry = geometry; self.sequence = sequence
            now = self.clock()
            observed = min(self.pending['created_at'], now - timings['source_callback_age_ms'] / 1000) if timings else now
            self.frame = {'frame_id': str(uuid.uuid4()), 'session_id': identity, 'request_id': request,
                          'sequence': sequence, 'width': geometry[1], 'height': geometry[2],
                          'orientation': geometry[3], 'geometry_epoch': geometry[0], 'received': observed,
                          'digest': digest, 'jpeg': data,
                          'observation_epoch': self.pending['observation_epoch']}
            if timings:
                self.frame.update(timings, observation_age_ms=min(5000, max(0, int((now - observed) * 1000))))
            self.result = self.frame; self.pending = None
            self.last_heartbeat = self.clock()
            self.condition.notify_all()

    def capture(self, timeout=8, settle_ms=0):
        integer(settle_ms, 0, MAX_SETTLE_MS)
        with self.condition:
            self._require(self.identity)
            if settle_ms and self.phone_capture_protocol != CAPTURE_PROTOCOL:
                raise Failure('phone_settle_unsupported', 409)
            if self.capture_busy:
                raise Failure('capture_busy', 409)
            self.capture_busy = True
            self.result = None
            self.pending = {'id': str(uuid.uuid4()), 'deadline': self.clock() + 5, 'created_at': self.clock(),
                            'settle_ms': settle_ms,
                            'observation_epoch': self.observation_epoch}
            identity = self.identity
            self.condition.notify_all()
            try:
                ready = self.condition.wait_for(
                    lambda: self.result is not None or self.pending is None or not self.active, timeout)
                self._require(identity)
                if self.result is None:
                    if not ready:
                        self.screen_request_timeouts = min(MAX_SEQUENCE, self.screen_request_timeouts + 1)
                    raise Failure('capture_not_received', 408)
                result = self.result
                if 'capture_error' in result:
                    raise Failure(result['capture_error'], 408)
                self.unknown = False
                return result
            finally:
                self.capture_busy = False
                self.pending = self.result = None

    def action(self, value):
        operation, arguments = action_arguments(value)
        digest = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
        with self.condition:
            old = self.commands.get(value['id'])
            if old:
                if old['digest'] != digest:
                    raise Failure('command_id_changed', 409)
                return old['result']
            if operation != 'release_all':
                self._require(value['session_id'])
                if self.control_reset_required:
                    raise Failure('control_reset_required', 409)
                if (self.frame is None or self.frame['frame_id'] != value['frame_id']
                        or self.frame.get('observation_epoch') != self.observation_epoch):
                    raise Failure('observe_again', 409)
                if self.unknown:
                    raise Failure('previous_input_unknown_observe_again', 409)
            if self.input_busy or self.control_reset_busy:
                raise Failure('input_in_progress', 409)
            if len(self.commands) >= 4096:
                raise Failure('command_limit_restart_bridge', 409)
            # Neutral release does not create or restore a control lease.
            if operation != 'release_all' and self.control_identity is None:
                self.control_identity = str(uuid.uuid4())
                self.control_generation += 1
                self.allow_control = True
            active_control = self.control_identity
            control_identity = (active_control or self.retired_control_identity or str(uuid.uuid4())
                                if operation == 'release_all' else active_control)
            generation = self.control_generation
            self.input_busy = True
            result = {'ok': False, 'status': 'outcome_unknown', 'id': value['id']}
            record = {'digest': digest, 'result': result}
            command_history = self.commands
            command_history[value['id']] = record
        try:
            self.controller.perform(control_identity, value['id'], operation, arguments)
            result = {'ok': True, 'status': 'acknowledged_not_visual_confirmation', 'id': value['id']}
        except Failure as error:
            result = {'ok': False, 'status': 'outcome_unknown', 'error': error.code, 'id': value['id']}
            with self.condition:
                if (self.identity == value['session_id'] and self.commands is command_history
                        and self.control_identity == active_control and self.control_generation == generation):
                    self.unknown = True
                    stop = self._detach_control(reset_required=True)
                else:
                    stop = None
            if stop:
                with contextlib.suppress(Exception):
                    self.controller.stop(stop)
        except Exception:
            result = {'ok': False, 'status': 'outcome_unknown', 'error': 'controller_unavailable', 'id': value['id']}
            with self.condition:
                if (self.identity == value['session_id'] and self.commands is command_history
                        and self.control_identity == active_control and self.control_generation == generation):
                    self.unknown = True
                    stop = self._detach_control(reset_required=True)
                else:
                    stop = None
            if stop:
                with contextlib.suppress(Exception):
                    self.controller.stop(stop)
        finally:
            with self.condition:
                self.input_busy = False
                record['result'] = result
        return result

    def reset_control(self, value):
        """Revoke desktop input only; never restart or grant screen sharing."""
        reset_arguments(value)
        digest = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
        with self.condition:
            record = self.control_resets.get(value['id'])
            if record is not None:
                if record['digest'] != digest:
                    raise Failure('command_id_changed', 409)
                if record['result'] is None:
                    raise Failure('control_reset_in_progress', 409)
                return record['result']
            self._expire()
            if value['session_id'] != (self.identity if self.active else None):
                raise Failure('session_inactive', 409)
            if self.control_reset_busy:
                raise Failure('control_reset_in_progress', 409)
            if len(self.control_resets) >= 4096:
                raise Failure('control_reset_limit_restart_bridge', 409)
            record = {'digest': digest, 'result': None}
            self.control_resets[value['id']] = record
            self.control_reset_busy = True
            self.observation_epoch += 1
            screen = self.identity
            old = self._detach_control() or self.retired_control_identity
            generation = self.control_generation
            self.frame = self.result = None
        succeeded = False
        try:
            if old:
                getattr(self.controller, 'reset', self.controller.stop)(old)
            succeeded = True
        except Exception:
            # The reset result is immutable too. Repeating an uncertain reset
            # ID must never issue another stop against a later input lease.
            pass
        finally:
            with self.condition:
                # An old reset completion cannot alter a later screen/lease.
                if self.identity == screen and self.control_generation == generation:
                    self.control_reset_required = not succeeded
                    self.unknown = not succeeded
                # Captures already in progress may finish for viewing, without
                # failing the phone's upload. They cannot authorize later input.
                self.observation_epoch += 1
                self.frame = self.result = None
                self.control_reset_busy = False
                record['result'] = {'ok': succeeded,
                                    'status': 'control_reset' if succeeded else 'control_reset_failed'}
                self.condition.notify_all()
        return record['result']

    def control_heartbeat(self):
        """Renew only an actual desktop lease; hardware faults keep video live."""
        with self.condition:
            self._expire()
            identity = self.control_identity
            generation = self.control_generation
            if not self.active or not self.allow_control or identity is None:
                return
        try:
            self.controller.heartbeat(identity)
        except Exception:
            with self.condition:
                if self.control_identity != identity or self.control_generation != generation:
                    return
                self.unknown = True
                old = self._detach_control(reset_required=True)
            if old:
                with contextlib.suppress(Exception):
                    self.controller.stop(old)

    def stop(self, reason='agent_stop'):
        with self.condition:
            old = self._deactivate(reason)
        if old:
            self.controller.stop(old)

    def status(self):
        with self.condition:
            self._expire()
            return {'v': 1, 'active': self.active, 'allow_control': self.allow_control,
                    'control_protocol': 'desktop-lease-v1',
                    'sharing_protocol': SCREEN_RECOVERY_PROTOCOL,
                    'capture_protocol': CAPTURE_PROTOCOL,
                    'phone_capture_protocol': self.phone_capture_protocol if self.active else None,
                    'control_reset_required': self.control_reset_required,
                    'last_inactive_reason': self.last_inactive_reason,
                    'heartbeat_age_seconds': min(1800, max(0, int(self.clock() - self.last_heartbeat))) if self.active else None,
                    'screen_request_timeouts': self.screen_request_timeouts,
                    'phone_source': self.owner if self.active else None,
                    'source_trust': ('private-configuration' if self.owner == 'legacy' else 'local-network-registration') if self.active and self.owner is not None else None,
                    'session_id': self.identity if self.active else None,
                    'has_observation': self.frame is not None, 'input_outcome_unknown': self.unknown,
                    'controller': self.controller.status()}


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, session, upload_token, agent_token, authority, pairing=None):
        self.address_family = socket.AF_INET6 if ':' in address[0] else socket.AF_INET
        super().__init__(address, Handler)
        self.session = session
        self.tokens = {'phone': upload_token, 'agent': agent_token}
        self.authority = authority
        self.pairing = pairing
        self.workers = threading.BoundedSemaphore(8)

    def process_request(self, request, client_address):
        if not self.workers.acquire(blocking=False):
            self.close_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.workers.release()
            self.close_request(request)
            raise

    def process_request_thread(self, request, client_address):
        try:
            if isinstance(request, ssl.SSLSocket):
                request.settimeout(5)
                request.do_handshake()
            request.settimeout(15)
            super().process_request_thread(request, client_address)
        except OSError:
            self.close_request(request)
        finally:
            self.workers.release()

    def get_request(self):
        connection, address = super().get_request()
        # Replies send small headers and bodies separately. Do not delay the
        # body behind a TCP acknowledgement during an interactive action.
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        connection.settimeout(15)
        return connection, address


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'StreamShot/1'

    def log_message(self, *args):
        pass

    def _origin(self):
        if (self.headers.get('Origin') is not None
                or len(self.headers.get_all('Host', [])) != 1
                or self.headers.get('Host') != self.server.authority):
            raise Failure('origin_or_host_rejected', 403)

    def _authorize(self, role):
        self._origin()
        supplied = self.headers.get('Authorization', '')
        if len(self.headers.get_all('Authorization', [])) != 1 or not supplied.isascii():
            raise Failure('unauthorized', 401)
        if hmac.compare_digest(supplied, 'Bearer ' + self.server.tokens[role]):
            return 'legacy' if role == 'phone' else 'agent'
        if role == 'phone' and self.server.pairing is not None and supplied.startswith('Bearer '):
            principal = self.server.pairing.authenticate(supplied[7:])
            if principal is not None:
                return principal
        raise Failure('unauthorized', 401)

    def _reply(self, status, value=None, image=None, metadata=None):
        data = image if image is not None else json.dumps(value).encode() if value is not None else b''
        self.send_response(status)
        self.send_header('Content-Type', 'image/jpeg' if image is not None else 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if isinstance(value, dict):
            disposition = {'phone_settle_unsupported': 'phone_settle_unsupported',
                           'capture_no_video_sample': 'no_video_sample', 'capture_ticket_expired': 'ticket_expired'}.get(value.get('error'))
            if disposition is not None:
                self.send_header('X-StreamShot-Capture-Error', disposition)
        if metadata:
            for key in ('frame_id', 'session_id', 'width', 'height', 'orientation', 'geometry_epoch'):
                self.send_header('X-StreamShot-' + key.replace('_', '-'), str(metadata[key]))
            for key in (*FRAME_TIMINGS.values(), 'observation_age_ms'):
                if key in metadata:
                    self.send_header('X-StreamShot-' + key.replace('_', '-'), str(metadata[key]))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _body(self, image=False):
        maximum = MAX_IMAGE if image else MAX_JSON
        raw = self.headers.get('Content-Length', '')
        if (len(self.headers.get_all('Content-Length', [])) != 1
                or not re.fullmatch(r'[0-9]{1,8}', raw) or self.headers.get('Transfer-Encoding')):
            raise Failure('content_length_required', 411)
        length = int(raw)
        if length > maximum:
            raise Failure('request_too_large', 413)
        expected = 'image/jpeg' if image else 'application/json'
        if self.headers.get_content_type() != expected:
            raise Failure('unsupported_content_type', 415)
        data = self.rfile.read(length)
        if len(data) != length:
            raise Failure('incomplete_body')
        return data if image else decode(data)

    def _dispatch(self):
        self.connection.settimeout(15)
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if self.command == 'GET' and path == '/health' and not parsed.query:
            return self._reply(200, {'v': 1, 'ok': True})
        if path.startswith('/v1/pairing/'):
            self._origin()
            raise Failure('not_found', 404)
        if path in ('/v2/pairing/identity', '/v2/pairing/trust'):
            self._origin()
            if parsed.query or parsed.fragment or self.command != 'POST' or self.server.pairing is None:
                raise Failure('not_found', 404)
            operation = self.server.pairing.identity if path.endswith('/identity') else self.server.pairing.trust
            return self._pair(operation, self._body())
        phone = path in ('/v1/session', '/v1/capture-request', '/v1/frame', '/v1/connection')
        principal = self._authorize('phone' if phone else 'agent')
        if self.command == 'GET' and path == '/v1/connection' and not parsed.query:
            return self._reply(200, {'v': 1, 'ok': True, 'capture': 'on-demand'})
        if self.command == 'GET' and path == '/v1/capture-request':
            query = urllib.parse.parse_qs(parsed.query, strict_parsing=True)
            if set(query) != {'session_id'} or len(query['session_id']) != 1:
                raise Failure('invalid_query')
            outcome = None
            fields = ('X-StreamShot-Capture-Outcome', 'X-StreamShot-Capture-Request-Id')
            if any(self.headers.get(name) is not None for name in fields):
                if any(len(self.headers.get_all(name, [])) != 1 for name in fields):
                    raise Failure('invalid_capture_outcome')
                outcome = {'status': self.headers[fields[0]], 'request_id': self.headers[fields[1]]}
            value = self.server.session.poll(query['session_id'][0], principal=principal, outcome=outcome)
            return self._reply(200 if value else 204, value)
        if parsed.query:
            raise Failure('query_not_allowed')
        if self.command == 'GET' and path == '/v1/status':
            return self._reply(200, dict(self.server.session.status(), pairing_available=self.server.pairing is not None,
                pairing_protocol='explicit-trust-v2' if self.server.pairing is not None else None))
        if self.command == 'POST' and path == '/v1/session':
            self.server.session.session(self._body(), principal=principal)
            return self._reply(200, {'ok': True})
        if self.command == 'POST' and path == '/v1/frame':
            headers = {key: self.headers.get('X-StreamShot-' + key) for key in
                       ('session', 'request', 'sequence', 'width', 'height', 'orientation', 'geometry-epoch')}
            for key in FRAME_TIMINGS:
                name = 'X-StreamShot-' + key
                if self.headers.get(name) is not None:
                    if len(self.headers.get_all(name, [])) != 1:
                        raise Failure('invalid_frame_timings')
                    headers[key] = self.headers[name]
            self.server.session.accept_frame(headers, self._body(image=True), principal=principal)
            return self._reply(201, {'ok': True})
        if self.command == 'POST' and path == '/v1/screen':
            settle = screen_arguments(self._body())
            value = self.server.session.capture(settle_ms=settle)
            return self._reply(200, image=value['jpeg'], metadata=value)
        if self.command == 'POST' and path == '/v1/actions':
            return self._reply(200, self.server.session.action(self._body()))
        if self.command == 'POST' and path == '/v1/control/reset':
            return self._reply(200, self.server.session.reset_control(self._body()))
        if self.command == 'POST' and path == '/v1/stop':
            exact(self._body(), set()); self.server.session.stop()
            return self._reply(200, {'ok': True})
        raise Failure('not_found', 404)

    def _pair(self, operation, *args):
        return self._reply(200, self._pair_call(operation, *args))

    def _pair_call(self, operation, *args):
        from streamshot_pairing import PairingFailure
        try:
            return operation(*args)
        except PairingFailure as error:
            raise Failure(error.code, error.status) from None

    def do_GET(self):
        try:
            self._dispatch()
        except Failure as error:
            self.close_connection = True
            self._reply(error.status, {'ok': False, 'error': error.code})
        except (OSError, ValueError, TimeoutError):
            self.close_connection = True
        except Exception:
            self.close_connection = True
            with contextlib.suppress(OSError):
                self._reply(500, {'ok': False, 'error': 'bridge_unavailable'})

    do_POST = do_GET


def private_file(path):
    path = Path(path)
    for item in (path, path.parent):
        info = item.lstat()
        if info.st_uid != os.getuid() or stat.S_ISLNK(info.st_mode) or info.st_mode & 0o077:
            raise Failure('private_configuration_required')
    if not path.is_file() or path.stat().st_size > 16384:
        raise Failure('invalid_private_configuration')
    return path


def local_origin_host(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.local', value):
        raise Failure('invalid_local_origin')
    return value


def init_state(directory, host, port, origin_host=None, name='My computer'):
    address = ipaddress.ip_address(host)
    if not address.is_private or address.is_unspecified or address.is_multicast:
        raise Failure('local_interface_required')
    integer(port, 1024, 65535)
    if origin_host is not None:
        local_origin_host(origin_host)
    if not isinstance(name, str) or not 1 <= len(name) <= 80 or any(ord(c) < 32 for c in name):
        raise Failure('invalid_computer_name')
    directory = Path(directory).expanduser().absolute()
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    certificate, key = directory / 'certificate.pem', directory / 'private-key.pem'
    settings = directory / 'openssl.cnf'
    san = 'IP:' + host + (',DNS:' + origin_host if origin_host else '')
    settings.write_text('[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=extensions\n[dn]\nCN=StreamShot local bridge\n[extensions]\nsubjectAltName=' + san + '\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,digitalSignature,keyCertSign\nextendedKeyUsage=serverAuth\n')
    settings.chmod(0o600)
    try:
        result = subprocess.run(['/usr/bin/openssl', 'req', '-new', '-x509', '-newkey', 'rsa:2048', '-sha256',
                                 '-nodes', '-days', '90', '-config', str(settings),
                                 '-keyout', str(key), '-out', str(certificate)], capture_output=True, timeout=20)
        if result.returncode != 0:
            raise Failure('certificate_generation_failed')
        certificate.chmod(0o600); key.chmod(0o600)
        der = subprocess.run(['/usr/bin/openssl', 'x509', '-in', str(certificate), '-outform', 'DER'],
                             capture_output=True, check=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        raise Failure('certificate_generation_failed') from None
    finally:
        settings.unlink(missing_ok=True)
    upload, agent = secrets.token_hex(32), secrets.token_hex(32)
    url = f'https://[{host}]:{port}' if address.version == 6 else f'https://{host}:{port}'
    if origin_host:
        url = f'https://{origin_host}:{port}'
    config = {'version': 1, 'host': host, 'port': port, 'baseURL': url, 'uploadToken': upload, 'agentToken': agent}
    if origin_host:
        config['originHost'] = origin_host
    phone = {'version': 1, 'name': name, 'baseURL': url, 'uploadToken': upload,
             'certificateSHA256': hashlib.sha256(der).hexdigest()}
    for name, value in [('bridge.json', config), ('phone-connection.json', phone)]:
        p = directory / name
        descriptor = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(value, stream, indent=2)


def configuration(directory):
    value = decode(private_file(Path(directory) / 'bridge.json').read_bytes())
    fields = {'version', 'host', 'port', 'baseURL', 'uploadToken', 'agentToken'}
    exact(value, fields | ({'originHost'} if isinstance(value, dict) and 'originHost' in value else set()))
    if value['version'] != 1:
        raise Failure('unsupported_version')
    address = ipaddress.ip_address(value['host'])
    if not address.is_private or address.is_unspecified or address.is_multicast:
        raise Failure('local_interface_required')
    integer(value['port'], 1024, 65535)
    expected = f'https://[{value["host"]}]:{value["port"]}' if address.version == 6 else f'https://{value["host"]}:{value["port"]}'
    if 'originHost' in value:
        expected = f'https://{local_origin_host(value["originHost"])}:{value["port"]}'
    if value['baseURL'] != expected or any(not re.fullmatch(r'[a-f0-9]{64}', value[k]) for k in ('uploadToken', 'agentToken')):
        raise Failure('invalid_private_configuration')
    private_file(Path(directory) / 'certificate.pem'); private_file(Path(directory) / 'private-key.pem')
    return value


def pairing_configuration(directory, config):
    value = decode(private_file(Path(directory) / 'phone-connection.json').read_bytes())
    exact(value, {'version', 'name', 'baseURL', 'uploadToken', 'certificateSHA256'})
    pem = private_file(Path(directory) / 'certificate.pem').read_text()
    pin = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
    if (type(value['version']) is not int or value['version'] != 1
            or not isinstance(value['name'], str) or not 1 <= len(value['name']) <= 80
            or any(ord(c) < 32 for c in value['name'])
            or value['baseURL'] != config['baseURL'] or value['uploadToken'] != config['uploadToken']
            or value['certificateSHA256'] != pin):
        raise Failure('invalid_private_configuration')
    return value


def serve(directory, port=None, discovery=False):
    config = configuration(directory)
    controller = SerialController(port) if port else UnavailableController()
    session = Session(controller)
    pairing = None
    if discovery:
        from streamshot_pairing import PairingService
        pairing = PairingService(pairing_configuration(directory, config), directory=directory,
                                 available=lambda: not session.status()['active'])
    server = Server((config['host'], config['port']), session, config['uploadToken'], config['agentToken'],
                    urllib.parse.urlsplit(config['baseURL']).netloc, pairing=pairing)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(Path(directory) / 'certificate.pem', Path(directory) / 'private-key.pem')
    # Handshake inside a bounded worker, never on the accept loop.
    server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    stopped = threading.Event()
    def watchdog():
        while not stopped.wait(5):
            session.control_heartbeat()
    threading.Thread(target=watchdog, daemon=True).start()
    print(json.dumps({'ok': True, 'status': 'listening', 'baseURL': config['baseURL'],
                      'capture': 'on-demand', 'controllerConfigured': port is not None}), flush=True)
    try:
        server.serve_forever()
    finally:
        stopped.set(); session.stop(reason='bridge_shutdown'); server.server_close()
        if pairing is not None:
            pairing.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, default=STATE)
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init'); init.add_argument('--host', required=True); init.add_argument('--port', type=int, default=8765)
    run = commands.add_parser('serve'); run.add_argument('--serial', help='Explicit ESP32-S3 USB character device; never autodetected or flashed')
    run.add_argument('--discovery', action='store_true', help='Enable owner-opened SRP pairing for MCP discovery')
    commands.add_parser('mcp')
    args = parser.parse_args(argv)
    try:
        if args.command == 'init':
            init_state(args.state, args.host, args.port)
            print(json.dumps({'ok': True, 'status': 'private_connection_created', 'credentialsPrinted': False}))
        elif args.command == 'serve': serve(args.state, args.serial, args.discovery)
        else:
            from streamshot_mcp import run_mcp
            run_mcp(args.state)
        return 0
    except KeyboardInterrupt:
        return 0
    except Failure as error:
        print(json.dumps({'ok': False, 'error': error.code}))
    except Exception:
        print(json.dumps({'ok': False, 'error': 'bridge_unavailable'}))
    return 2


if __name__ == '__main__':
    sys.exit(main())
