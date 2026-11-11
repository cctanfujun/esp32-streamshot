"""Explicit desktop resets over synthetic PTYs, never a physical serial port."""
import contextlib
import errno
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_serial import BOOT_A, BOOT_B, Peer, SESSION_A, bridge


class SerialResetChecks(unittest.TestCase):
    def peer(self, handler=None):
        peer = Peer(handler=handler, timeout=.2)

        def close():
            # A failed reopen may intentionally leave the controller's old fd
            # closed. Give Peer.close one live descriptor without double-close.
            fd = peer.driver.fd
            try:
                os.fstat(fd)
            except (OSError, TypeError):
                fd = os.dup(peer.master)
            if peer.slave != fd:
                with contextlib.suppress(OSError):
                    os.close(peer.slave)
            peer.slave = fd
            peer.close()

        self.addCleanup(close)
        return peer

    def retire_input_session(self, peer):
        peer.driver.perform(SESSION_A, str(uuid.uuid4()), 'click', {'button': 'left'})
        self.assertEqual(peer.input_count, 1)
        self.assertEqual(peer.driver.boot, BOOT_A)
        peer.driver.cancel(SESSION_A)

    def assert_retired(self, peer, input_count=1):
        self.assertFalse(peer.driver.status()['armed'])
        before = len(peer.commands)
        with self.assertRaises(bridge.Failure) as failure:
            peer.driver.perform(SESSION_A, str(uuid.uuid4()), 'move', {'dx': 1, 'dy': 0})
        self.assertEqual(failure.exception.code, 'controller_reset_required')
        self.assertEqual(len(peer.commands), before, 'retired input reached the wire')
        self.assertEqual(peer.input_count, input_count)

    @contextlib.contextmanager
    def reenumerated(self, peer, fail_open=False):
        """Same selected port, changed fd identity, real PTY ACKs after reopen."""
        old_fd = peer.driver.fd
        real_fstat = os.fstat
        current = real_fstat(old_fd)
        old = SimpleNamespace(st_mode=current.st_mode, st_dev=current.st_dev,
                              st_ino=current.st_ino + 1, st_rdev=current.st_rdev + 1)
        replacement = None if fail_open else os.dup(old_fd)

        def fstat(fd):
            return old if fd == old_fd else real_fstat(fd)

        def reopen(path, flags, *args, **kwargs):
            self.assertEqual(path, peer.driver.serial_port)
            if fail_open:
                raise OSError(errno.ENXIO, 'synthetic device unavailable')
            return replacement

        try:
            with patch.object(bridge.os, 'stat', return_value=current), \
                    patch.object(bridge.os, 'fstat', side_effect=fstat), \
                    patch.object(bridge.os, 'open', side_effect=reopen) as opened:
                yield opened
        finally:
            if replacement is not None:
                if peer.driver.fd == replacement:
                    peer.slave = replacement
                else:
                    with contextlib.suppress(OSError):
                        os.close(replacement)

    def test_reenumeration_reopens_only_selected_port_accepts_new_boot_without_replay(self):
        peer = self.peer()
        self.retire_input_session(peer)
        before, sequence = len(peer.commands), peer.driver.sequence
        peer.boot = BOOT_B
        peer.armed = None
        peer.used_sessions.clear()
        peer.last_seq.clear()
        peer.driver.buffer = b'{"old_ack_fragment":'
        with self.reenumerated(peer) as opened:
            peer.driver.reset(SESSION_A)
            opened.assert_called_once()
        reset_commands = peer.commands[before:]
        self.assertEqual([row['op'] for row in reset_commands], ['status', 'stop'])
        self.assertEqual([row['seq'] for row in reset_commands], [sequence + 1, sequence + 2])
        self.assertEqual(peer.driver.boot, BOOT_B)
        self.assertEqual(peer.driver.buffer, b'')
        self.assertIsNone(peer.armed)
        self.assertIn(SESSION_A, peer.driver.blocked)
        self.assertTrue(peer.driver.cancelled[SESSION_A].is_set())
        self.assert_retired(peer)

    def test_missing_stop_ack_makes_explicit_reset_fail_and_keeps_session_retired(self):
        def handler(peer, message, answer):
            return None if message['op'] == 'stop' else answer

        peer = self.peer(handler)
        self.retire_input_session(peer)
        before = len(peer.commands)
        with patch.object(bridge.os, 'stat', return_value=os.fstat(peer.driver.fd)), \
                patch.object(bridge.os, 'open') as opened:
            with self.assertRaises(bridge.Failure):
                peer.driver.reset(SESSION_A)
            opened.assert_not_called()
        self.assertEqual([row['op'] for row in peer.commands[before:]], ['status', 'stop'])
        self.assert_retired(peer)

    def test_stop_ack_still_armed_makes_explicit_reset_fail(self):
        def handler(peer, message, answer):
            return dict(answer, armed=True) if message['op'] == 'stop' else answer

        peer = self.peer(handler)
        self.retire_input_session(peer)
        with patch.object(bridge.os, 'stat', return_value=os.fstat(peer.driver.fd)):
            with self.assertRaises(bridge.Failure):
                peer.driver.reset(SESSION_A)
        self.assert_retired(peer)

    def test_reopen_failure_keeps_input_retired_without_new_wire_commands(self):
        peer = self.peer()
        self.retire_input_session(peer)
        before, sequence = len(peer.commands), peer.driver.sequence
        with self.reenumerated(peer, fail_open=True) as opened:
            with self.assertRaises(bridge.Failure):
                peer.driver.reset(SESSION_A)
            opened.assert_called_once()
        self.assertEqual(len(peer.commands), before)
        self.assertEqual(peer.driver.sequence, sequence)
        self.assert_retired(peer)


if __name__ == '__main__':
    unittest.main()
