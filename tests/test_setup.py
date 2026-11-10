import importlib.util
from pathlib import Path
import stat
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('skill_setup', ROOT / 'skills/esp32-streamshot/scripts/setup.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class SetupTests(unittest.TestCase):
    def test_view_only_config_never_selects_port(self):
        value = setup.configuration(state='/tmp/example-private-state')['mcpServers']['streamshot']
        self.assertNotIn('env', value)
        self.assertTrue(Path(value['args'][0]).is_file())
        self.assertEqual(value['args'][-3:], ['--state', '/tmp/example-private-state', 'mcp'])

    def test_rejects_non_device_or_relative_state(self):
        for port in ['/etc/passwd', '/dev/cu.bluetooth-test', 'usbmodem1']:
            with self.assertRaises(ValueError): setup.configuration(port)
        with self.assertRaises(ValueError): setup.configuration(state='relative')

    def test_explicit_port_checks_type_without_open(self):
        with patch.object(setup.os, 'stat', return_value=type('Info', (), {'st_mode': stat.S_IFCHR})()) as checked:
            with patch('builtins.open', side_effect=AssertionError('must not open a device')):
                value = setup.configuration('/dev/cu.usbmodemTEST')
        checked.assert_called()
        self.assertEqual(value['mcpServers']['streamshot']['env'], {'STREAMSHOT_SERIAL_PORT': '/dev/cu.usbmodemTEST'})
