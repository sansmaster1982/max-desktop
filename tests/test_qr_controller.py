"""QR receiving-side security and timing, without network or real tokens."""
import unittest
from unittest.mock import Mock
from maxclient.core.qr_login import QrLoginController
from maxclient.protocol.client import MaxWriteOutcomeUnknown


class QrControllerTests(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.client = Mock()
        self.client.device_id = 'synthetic-device'
        self.client.qr_create.return_value = {'trackId':'synthetic-track','qrLink':'https://max.ru/synthetic-qr',
            'expiresAt':1060000,'ttl':60000,'pollingInterval':1000}
        self.client.qr_poll.return_value = {'status':{'loginAvailable':False,'expiresAt':1060000,'ttl':45000}}
        self.factory = Mock(return_value=self.client)
        self.controller = QrLoginController('26.10.11', client_factory=self.factory, clock=lambda:self.now[0])

    def tearDown(self):
        self.controller.close()

    def success(self):
        return {'tokenAttrs':{'LOGIN':{'token':'synthetic-secret-token'}},'profile':{'contact':{'id':77}}}

    def test_fresh_isolated_web_identity_and_millisecond_timing(self):
        challenge = self.controller.start()
        self.client.connect.assert_called_once_with(device_type='WEB')
        self.assertFalse(self.factory.call_args.kwargs['auto_reconnect'])
        self.assertTrue(self.factory.call_args.kwargs['device_id'])
        self.assertEqual(challenge.expires_at_ms,1060000)
        self.assertEqual(challenge.polling_interval_ms,1000)
        self.now[0] += 15
        result = self.controller.poll()
        self.assertEqual(result['expires_at_ms'],1060000)
        self.assertNotIn('synthetic-qr',repr(challenge))

    def test_expired_code_never_polls(self):
        self.controller.start()
        self.now[0] += 61
        self.assertEqual(self.controller.poll()['state'],'expired')
        self.client.qr_poll.assert_not_called()
        self.client.qr_finish.assert_not_called()

    def test_password_challenge_is_not_authentication_success(self):
        self.controller.start()
        self.client.qr_poll.return_value={'status':{'loginAvailable':True}}
        self.client.qr_finish.return_value={'passwordChallenge':{'trackId':'synthetic-password-track','hint':'synthetic hint'}}
        self.assertEqual(self.controller.poll()['state'],'password_required')
        self.client.qr_check_password.assert_not_called()
        self.client.qr_check_password.return_value=self.success()
        result=self.controller.check_password('synthetic-current-password')
        self.client.qr_check_password.assert_called_once_with('synthetic-password-track','synthetic-current-password')
        self.assertEqual(result['authorization'].profile_id,77)
        self.assertNotIn('synthetic-secret-token',repr(result['authorization']))

    def test_register_only_is_never_accepted(self):
        self.controller.start()
        self.client.qr_poll.return_value={'status':{'loginAvailable':True}}
        self.client.qr_finish.return_value={'tokenAttrs':{'REGISTER':{'token':'synthetic-register'}},'profile':{'contact':{'id':77}}}
        with self.assertRaises(ValueError): self.controller.poll()
        self.assertEqual(self.controller.state,'closed')

    def test_unknown_finalize_outcome_is_not_repeated(self):
        self.controller.start()
        self.client.qr_poll.return_value={'status':{'loginAvailable':True}}
        self.client.qr_finish.side_effect=MaxWriteOutcomeUnknown('synthetic unknown')
        with self.assertRaises(MaxWriteOutcomeUnknown): self.controller.poll()
        with self.assertRaises(ValueError): self.controller.poll()
        self.assertEqual(self.client.qr_finish.call_count,1)

    def test_cancel_during_connection_does_not_create_challenge(self):
        self.client.connect.side_effect=lambda **_kwargs:self.controller.close()
        with self.assertRaises(ValueError):self.controller.start()
        self.client.qr_create.assert_not_called()

    def test_cancel_before_queued_start_never_creates_a_client(self):
        self.controller.cancel()
        with self.assertRaises(ValueError):self.controller.start()
        self.controller.close()
        with self.assertRaises(ValueError):self.controller.start()
        self.factory.assert_not_called()
        self.client.connect.assert_not_called()
        self.client.qr_create.assert_not_called()

    def test_success_keeps_terminal_state_after_receiver_closes(self):
        self.controller.start()
        self.client.qr_poll.return_value={'status':{'loginAvailable':True}}
        self.client.qr_finish.return_value=self.success()
        self.assertEqual(self.controller.poll()['state'],'authorized')
        self.assertEqual(self.controller.state,'authorized')
        self.client.close.assert_called_once()
        with self.assertRaises(ValueError):self.controller.start()
        with self.assertRaises(ValueError):self.controller.poll()
        self.assertEqual(self.client.qr_finish.call_count,1)

    def test_poll_is_single_flight(self):
        self.controller.start()
        def status(_track):
            with self.assertRaises(ValueError):self.controller.poll()
            return {'status':{'loginAvailable':False,'ttl':1000}}
        self.client.qr_poll.side_effect=status
        self.assertEqual(self.controller.poll()['state'],'waiting')
        self.client.qr_poll.assert_called_once()
        self.client.qr_finish.assert_not_called()


if __name__=='__main__':unittest.main()
