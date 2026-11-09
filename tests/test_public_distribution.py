"""Public variant resource/exclusion contract; synthetic local widgets only."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave

from PySide6.QtWidgets import QApplication, QWidget
from maxclient.ui.assets import app_icon, icon_file, logo_pixmap
from maxclient.ui.call_window import CallWindow


class PublicDistributionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_only_original_sound_and_masks_and_maxim_brand_are_included(self):
        root = Path(__file__).resolve().parents[1] / 'maxclient'
        names = {p.name for p in (root / 'assets').iterdir()}
        self.assertEqual(len(names), 8)
        self.assertNotIn('message.mp3', names)
        self.assertFalse((root / 'ui/calls_web').exists())
        with wave.open(str(root / 'assets/message.wav')) as audio:
            self.assertEqual((audio.getnchannels(), audio.getsampwidth()), (1, 2))
            self.assertEqual(audio.getnframes(), 4410)
        self.assertEqual(Path(icon_file()).name, 'maxim.ico')
        self.assertFalse(app_icon().isNull())
        self.assertFalse(logo_pixmap(48).isNull())

    def test_omitted_call_sdk_disables_window_before_server_or_backend(self):
        runtime = Mock()
        runtime.busy = False
        config = Mock()
        with patch('maxclient.ui.call_window.QWebEngineView', QWidget), \
                patch('maxclient.ui.call_window._AssetsServer') as server:
            window = CallWindow(runtime, config, contact_id=7)
            try:
                self.assertIn('не включены', window.status.text())
                self.assertFalse(window.audio_call.isEnabled())
                self.assertFalse(window.video_call.isEnabled())
                window._start(False)
                runtime.arm.assert_not_called()
                runtime.arm_incoming.assert_not_called()
                config.public_dict.assert_not_called()
                server.assert_not_called()
            finally:
                window.close()
                self.app.processEvents()

    def test_frozen_miniapp_stops_before_service_or_window_import(self):
        import sys
        from PySide6.QtWidgets import QLabel
        from maxclient.ui.timeline_miniapps import TimelineMiniApps, create_window
        view = QWidget()
        view.h_sub = QLabel(view)
        view.service = Mock()
        controller = TimelineMiniApps(view)
        try:
            with patch.object(sys, 'frozen', True, create=True), \
                    patch('maxclient.ui.timeline_miniapps.run_async') as jobs:
                controller.open(None, 0, 0, 0, object())
                with self.assertRaises(RuntimeError):
                    create_window('https://synthetic.invalid', object(), Mock(), view)
            self.assertIn('не включены', view.h_sub.text())
            self.assertEqual(view.service.mock_calls, [])
            jobs.assert_not_called()
            self.assertIsNone(controller.pending)
        finally:
            view.close()
            view.deleteLater()

    def test_frozen_call_routes_stop_before_backend_or_optional_import(self):
        import sys
        from maxclient.ui.main_window import MainWindow
        fake = SimpleNamespace(statusBar=Mock(return_value=Mock()), service=Mock())
        with patch.object(sys, 'frozen', True, create=True):
            MainWindow._open_call(fake, object())
            MainWindow._on_incoming_call(fake, object())
        self.assertEqual(fake.service.mock_calls, [])
        self.assertEqual(fake.statusBar().showMessage.call_count, 2)


if __name__ == '__main__':
    unittest.main()
