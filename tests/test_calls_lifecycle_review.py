"""Public lifecycle: omitted SDK cannot start, and closing stays local.

The development JS lifecycle corpus targets the non-distributed vendor SDK and
is excluded. Core CallRuntime tests remain; this tests the public disabled UI.
"""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from unittest.mock import Mock, patch
from PySide6.QtWidgets import QApplication, QWidget
from maxclient.core.call_runtime import CallRuntime, CallRuntimeError
from maxclient.ui.call_window import CallWindow


class CallsLifecycleReviewTests(unittest.TestCase):
    def test_unavailable_sdk_window_closes_without_server_or_remote_action(self):
        app = QApplication.instance() or QApplication([])
        runtime = Mock()
        runtime.busy = False
        with patch('maxclient.ui.call_window.QWebEngineView', QWidget), \
                patch('maxclient.ui.call_window._AssetsServer') as server:
            window = CallWindow(runtime, Mock(), contact_id=7)
            window.show()
            app.processEvents()
            window._start(False)
            window.close()
            app.processEvents()
            runtime.close.assert_called_once_with()
            runtime.arm.assert_not_called()
            runtime.arm_incoming.assert_not_called()
            runtime.cancel_start.assert_not_called()
            server.assert_not_called()

    def test_cancel_during158_does_not_return_a_late_credential(self):
        calls = []
        runtime = None
        class Backend:
            def rpc(self, opcode, payload):
                calls.append((opcode, payload))
                runtime.cancel_start()
                return {'token': 'synthetic-call-only', 'token_lifetime_ts': 3, 'token_refresh_ts': 2}
        runtime = CallRuntime(Backend(), is_authorized=lambda: True)
        try:
            generation = runtime.arm_outgoing(77)['generation']
            with self.assertRaises(CallRuntimeError):
                runtime.execute('call_token', {}, generation)
            self.assertEqual(calls, [(158, None)])
        finally:
            runtime.close()


if __name__ == '__main__':
    unittest.main()
