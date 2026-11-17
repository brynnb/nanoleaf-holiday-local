import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from nanoleaf_holiday_local import cli, native, scenes, state

# Synthetic device ID, address and fixtures. No test reaches a real network.
DEVICE = '1234567890ABCDEF'
LAN = '192.168.123.10'


class Workflow(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_local_address_validation(self):
        self.assertEqual(cli.local_ip(LAN), LAN)
        for bad in ('localhost', '1.1.1.1', '127.0.0.1', '0.0.0.0', '::1'):
            with self.subTest(address=bad), self.assertRaises(ValueError):
                cli.local_ip(bad)

    def test_declining_device_does_not_enroll(self):
        with patch.object(cli, 'native_identity', return_value=DEVICE), \
             patch('builtins.input', return_value='no'), patch('builtins.print'):
            with self.assertRaisesRegex(RuntimeError, 'not selected'):
                cli.select_device(self.root, LAN, enroll=True)
        self.assertFalse((self.root/'device.json').exists())

    def test_saved_identity_can_move_but_not_change(self):
        path = self.root/'device.json'
        state.write_private(path, {'host': LAN, 'identity': DEVICE})
        other = '192.168.123.11'
        with patch.object(cli, 'native_identity', return_value='0'*16):
            with self.assertRaisesRegex(ValueError, 'no credentials'):
                cli.select_device(self.root, other, enroll=True)
        self.assertEqual(cli.doc_json(path)['host'], LAN)
        with patch.object(cli, 'native_identity', return_value=DEVICE):
            self.assertEqual(cli.select_device(self.root, other), (other, DEVICE))
        self.assertEqual(cli.doc_json(path)['host'], other)

    def test_native_authorization_requires_expected_id(self):
        session = native.Session(LAN)
        with patch.object(session, 'exchange') as exchange:
            with self.assertRaisesRegex(ValueError, 'Select a device'):
                session.handshake('fixture', b'dummy', 1)
        exchange.assert_not_called()

    def test_native_identity_rejects_untested_firmware(self):
        def tlv1(tag, content):
            return bytes([tag, len(content)]) + content
        info = tlv1(0, b'NL71K2') + tlv1(1, DEVICE.encode()) + tlv1(2, b'1.2.3')
        session = native.Session(LAN, DEVICE)
        with patch.object(session, 'exchange', return_value=info):
            with self.assertRaisesRegex(ValueError, 'firmware'):
                session.identify()
        self.assertFalse(session.identified)

    def test_openapi_5xx_is_not_invalid_token(self):
        with patch.object(cli.http.client, 'HTTPConnection') as http:
            resp = http.return_value.getresponse.return_value
            resp.read.return_value = b'{}'
            resp.status = 503
            with self.assertRaisesRegex(RuntimeError, '503'):
                cli.openapi_ok(LAN, 'testToken')
            resp.status = 401
            self.assertFalse(cli.openapi_ok(LAN, 'testToken'))
            resp.status = 200
            resp.read.return_value = b'{"model":"other"}'
            with self.assertRaisesRegex(ValueError, 'unexpected'):
                cli.openapi_ok(LAN, 'testToken')

    def test_interrupted_native_allocation_stops_before_retry(self):
        session = MagicMock()
        session.identify.return_value = {6: b'String-Lights'}
        session.encrypted.side_effect = TimeoutError('simulated response loss')
        with patch.object(cli, 'setup_code', return_value='34970112332'):
            with self.assertRaises(TimeoutError):
                cli.new_native(self.root, session)
        self.assertTrue((self.root/'native-allocation-started').is_file())
        with patch.object(cli.native, 'Session') as retry:
            with self.assertRaisesRegex(RuntimeError, 'uncertain result'):
                cli.enable_api(self.root, LAN, DEVICE, fresh=True)
        retry.assert_not_called()

    def test_interrupted_openapi_allocation_is_not_repeated(self):
        state.write_private(self.root/'native.json', {'test': True})
        session = MagicMock()
        session.request.side_effect = [b'\1', TimeoutError('simulated response loss')]
        with patch.object(cli, 'saved_session', return_value=session):
            with self.assertRaises(TimeoutError):
                cli.enable_api(self.root, LAN, DEVICE, fresh=False)
            session.request.side_effect = None
            session.request.return_value = b'\1'
            with self.assertRaisesRegex(RuntimeError, 'uncertain result'):
                cli.enable_api(self.root, LAN, DEVICE, fresh=False)
        self.assertEqual([c.args[0] for c in session.request.call_args_list],
                         ['openapi/enable', 'openapi/token_gen', 'openapi/enable'])

    def test_new_local_api_enrollment_enables_verifies_and_saves(self):
        session = MagicMock(client_id=3)
        session.identify.return_value = {6: b'String-Lights'}
        session.encrypted.return_value = native.tlv(6, b'x'*32)
        session.request.side_effect = [b'\0', b'', b'\1', b'testOpenAPIToken\0']
        with patch.object(cli.native, 'Session', return_value=session), \
             patch.object(cli, 'setup_code', return_value='34970112332'), \
             patch.object(cli, 'openapi_ok', return_value=True) as verify, \
             patch('builtins.print'):
            cli.enable_api(self.root, LAN, DEVICE, fresh=False)
        verify.assert_called_once_with(LAN, 'testOpenAPIToken')
        self.assertEqual(state.read_private(self.root/'openapi-token'), b'testOpenAPIToken')
        self.assertEqual(cli.doc_json(self.root/'native.json')['client_id'], 3)
        self.assertFalse(any(self.root.glob('pending-*')))
        session.connection.close.assert_called_once()

    def test_reinstall_is_idempotent_and_different_scene_refused(self):
        pattern = json.loads((cli.REPO/'classic-christmas.json').read_text())
        shape = {'leds_per_zone': 2, 'zone_count': 150}
        raw = scenes.encode_static(pattern, 3, shape)
        before = {'layout': shape, 'effects': {'3': raw.hex()}}
        session = MagicMock()
        self.assertEqual(scenes.store_static(session, pattern, before, allow_existing=True), 3)
        different = dict(pattern, colors=[{'hex': '00FE00'}])
        with self.assertRaisesRegex(ValueError, 'Stored scene differs'):
            scenes.store_static(session, different, before, allow_existing=True)
        session.request.assert_not_called()

    def test_private_state_has_restrictive_permissions(self):
        p = self.root/'subdir'/'token'
        state.write_private(p, b'test-token')
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        self.assertEqual(p.parent.stat().st_mode & 0o777, 0o700)
        alias = self.root/'alias'
        alias.symlink_to(p)
        with self.assertRaises(ValueError):
            state.read_private(alias)


if __name__ == '__main__':
    unittest.main()
