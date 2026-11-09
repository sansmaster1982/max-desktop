import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import time
import unittest
from unittest.mock import Mock,patch
from PySide6.QtWidgets import QApplication
from maxclient.core.config import Settings
from maxclient.core.qr_login import QrChallenge,QrLoginController
from maxclient.ui.qr_login_panel import QrLoginPanel,qr_pixmap


class QrPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.patch=patch('maxclient.ui.qr_login_panel.run_async')
        self.run=self.patch.start()
        self.panel=QrLoginPanel(Settings())

    def tearDown(self):
        self.panel.deactivate();self.panel.close();self.patch.stop()

    def test_stale_challenge_after_leaving_is_not_displayed(self):
        self.panel.activate();generation=self.panel._generation
        self.panel.deactivate()
        self.panel._created(generation,QrChallenge('https://max.ru/synthetic',time.time()*1000+60000,1000))
        self.assertTrue(self.panel.code.pixmap().isNull())
        self.assertFalse(self.panel._poll_timer.isActive())

    def test_password_challenge_requires_explicit_user_entry(self):
        self.panel.activate()
        self.panel._received(self.panel._generation,{'state':'password_required','hint':'synthetic'})
        self.assertEqual(self.panel.password.text(),'')
        self.assertFalse(self.panel.password.isHidden())
        self.assertFalse(self.panel._poll_timer.isActive())

    def test_qr_uses_black_white_modules_and_quiet_zone(self):
        image=qr_pixmap('https://max.ru/synthetic-qr').toImage()
        self.assertEqual(image.pixelColor(0,0).name(),'#ffffff')
        self.assertGreater(image.width(),100)
        self.assertLessEqual(image.width(),280)
        colors={image.pixelColor(x,y).name() for y in range(image.height()) for x in range(image.width())}
        self.assertEqual(colors,{'#ffffff','#000000'})

    def test_login_rejection_unlocks_explicit_refresh_and_stops_polling(self):
        self.panel.activate()
        self.panel.refresh_button.setEnabled(False)
        self.panel.authentication_failed('synthetic LOGIN rejection')
        self.assertTrue(self.panel.refresh_button.isEnabled())
        self.assertFalse(self.panel._poll_timer.isActive())
        self.assertIn('rejection',self.panel.status.text())

    def _pending_result(self, *, response=None, waiting=False):
        now=[0.0]
        client=Mock()
        client.device_id='synthetic-device'
        client.qr_create.return_value={'trackId':'synthetic-track','qrLink':'https://max.ru/synthetic',
            'ttl':1000,'pollingInterval':250}
        client.qr_poll.return_value={'status':{'loginAvailable':not waiting,'ttl':2000}}
        client.qr_finish.return_value=response
        controller=QrLoginController('26.10.11',client_factory=Mock(return_value=client),clock=lambda:now[0])
        controller.start()
        self.panel._active=True
        self.panel._generation=1
        self.panel._controller=controller
        self.panel._expires_at_ms=1000
        self.panel._busy=True
        now[0]=0.999
        result=controller.poll()
        return controller,client,result

    def test_cancel_before_queued_start_and_close_cannot_open_receiver(self):
        factory=Mock()
        controller=QrLoginController('26.10.11',client_factory=factory)
        with patch('maxclient.ui.qr_login_panel.QrLoginController',return_value=controller):
            self.panel.activate()
        start=self.run.call_args.args[0]
        error=self.run.call_args.kwargs['on_error']
        self.panel.deactivate()
        queued_close=self.run.call_args.args[0]
        # Cancellation is immediate even if shutdown is queued behind start.
        with self.assertRaises(ValueError):start()
        queued_close()
        with self.assertRaises(ValueError):start()
        error('synthetic cancelled start')
        factory.assert_not_called()
        self.assertEqual(controller.state,'closed')
        self.assertIsNone(self.panel._controller)
        self.assertFalse(self.panel._poll_timer.isActive())

    def test_authorized_callback_queued_across_expiry_is_delivered(self):
        controller,client,result=self._pending_result(response={
            'tokenAttrs':{'LOGIN':{'token':'synthetic-token'}},'profile':{'contact':{'id':77}}})
        delivered=[]
        self.panel.authorized.connect(delivered.append)
        self.assertEqual(controller.state,'authorized')
        with patch('maxclient.ui.qr_login_panel.time.time',return_value=1.001):
            self.assertTrue(self.panel._tick())
            self.panel._received(1,result)
        self.assertEqual(len(delivered),1)
        self.assertEqual(self.panel._generation,1)
        self.assertFalse(self.panel._busy)
        self.assertFalse(self.panel.refresh_button.isEnabled())
        self.assertFalse(self.panel._poll_timer.isActive())
        client.qr_finish.assert_called_once()

    def test_password_callback_queued_across_expiry_shows_explicit_prompt(self):
        _controller,client,result=self._pending_result(response={
            'passwordChallenge':{'trackId':'synthetic-password-track','hint':'synthetic hint'}})
        with patch('maxclient.ui.qr_login_panel.time.time',return_value=1.001):
            self.assertTrue(self.panel._tick())
            self.panel._received(1,result)
        self.assertEqual(self.panel._generation,1)
        self.assertFalse(self.panel.password.isHidden())
        self.assertEqual(self.panel.password.text(),'')
        self.assertFalse(self.panel._clock.isActive())
        client.qr_check_password.assert_not_called()

    def test_waiting_callback_uses_new_server_ttl_before_local_expiry(self):
        _controller,client,result=self._pending_result(waiting=True)
        with patch('maxclient.ui.qr_login_panel.time.time',return_value=1.001):
            self.assertTrue(self.panel._tick())
            self.panel._received(1,result)
        self.assertEqual(self.panel._generation,1)
        self.assertEqual(self.panel._expires_at_ms,2999)
        self.assertTrue(self.panel._poll_timer.isActive())
        client.qr_finish.assert_not_called()


if __name__=='__main__':unittest.main()
