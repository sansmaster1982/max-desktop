"""Synthetic download UI: no stream, file saving, account or external navigation."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import dataclass
from pathlib import Path
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLabel
from shiboken6 import isValid

from maxclient.ui.conversation_file_dialog import FileDownloadDialog, UnsafeFileDownloadDialog, confirm_download
from test_desktop_shell import _load_offscreen_font


@dataclass(frozen=True)
class Descriptor:
    context: object
    key: str = 'attachment:42:91:0'
    file_id: float = 52.25
    chat_id: int = 42
    message_id: int = 91
    time_ms: float = 1793459500091.5
    item_type: str = 'REGULAR'
    file_name: str = '<b>Синтетический счёт & приложение.pdf</b>'
    size: int = 10485760


class Relay(QObject):
    progress = Signal(object, object)
    status = Signal(str)


class FileDownloadDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        _load_offscreen_font(cls.app)

    def setUp(self):
        self.descriptor = Descriptor(object())
        self.dialog = FileDownloadDialog(self.descriptor)
        self.cancelled, self.messages = [], []
        self.dialog.cancel_requested.connect(lambda: self.cancelled.append(True))
        self.dialog.message_requested.connect(lambda value: self.messages.append(value))
        self.extra = []
        self.dialog.show()
        self.app.processEvents()

    def tearDown(self):
        for widget in [self.dialog] + self.extra:
            if isValid(widget):
                widget.close()
                widget.deleteLater()
                QCoreApplication.sendPostedEvents(widget, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_nonmodal_descriptor_uses_exact_canonical_fields_and_literal_name(self):
        self.assertFalse(self.dialog.isModal())
        self.assertIs(self.dialog.descriptor, self.descriptor)
        self.assertEqual(self.dialog.filename_label._full_text, self.descriptor.file_name)
        self.assertEqual(self.dialog.filename_label.textFormat(), Qt.TextFormat.PlainText)
        self.assertIn('&lt;b&gt;', self.dialog.filename_label.toolTip())
        self.assertEqual(self.dialog.size_label.text(), '10.0 МБ')
        self.assertEqual(self.dialog.status_label.text(), 'Подключение…')
        self.assertEqual(self.dialog.progress_bar.maximum(), 0)
        with self.assertRaises(ValueError):
            FileDownloadDialog({'filename': 'alias is not accepted', 'size_bytes': 10})

    def test_progress_keeps_exact_large_bytes_and_does_not_claim_saved_before_commit(self):
        total = 2**42
        self.assertTrue(self.dialog.set_progress(total // 2, total))
        self.assertEqual(self.dialog._received, total // 2)
        self.assertEqual(self.dialog.progress_bar.value(), 500)
        self.assertEqual(self.dialog.status_label.text(), 'Скачивание…')
        self.assertTrue(self.dialog.set_progress(total, total))
        self.assertEqual(self.dialog.progress_bar.value(), 1000)
        self.assertNotIn('сохранён', self.dialog.status_label.text())
        self.assertTrue(self.dialog.set_status('saving'))
        self.assertEqual(self.dialog.status_label.text(), 'Сохранение файла…')
        self.assertTrue(self.dialog.set_status('done'))
        self.assertEqual(self.dialog.status_label.text(), 'Файл сохранён')

    def test_unknown_and_zero_totals_are_distinct_from_fabricated_completion(self):
        self.assertTrue(self.dialog.set_progress(0, None))
        self.assertEqual(self.dialog.progress_bar.maximum(), 0)
        self.assertEqual(self.dialog.progress_label.text(), '0 Б')
        self.assertTrue(self.dialog.set_progress(0, 0))
        self.assertEqual(self.dialog.progress_bar.maximum(), 1000)
        self.assertEqual(self.dialog.progress_bar.value(), 0)
        self.assertEqual(self.dialog.progress_label.text(), '0 Б из 0 Б')
        self.assertEqual(self.dialog.status_label.text(), 'Скачивание…')

    def test_invalid_and_regressing_progress_is_rejected(self):
        for received, total in ((True, 10), (-1, 10), (1.5, 10), ('1', 10),
                                (1, False), (1, -1), (11, 10), (1, float('inf'))):
            self.assertFalse(self.dialog.set_progress(received, total))
        self.assertTrue(self.dialog.set_progress(5, 10))
        self.assertFalse(self.dialog.set_progress(4, 10))
        self.assertFalse(self.dialog.set_status('connecting'))
        self.assertFalse(self.dialog.set_status([]))
        self.assertTrue(self.dialog.set_status('saving'))
        self.assertFalse(self.dialog.set_progress(6, 10))
        self.assertFalse(self.dialog.set_status('downloading'))
        self.assertEqual(self.dialog._received, 5)

    def test_cancel_is_one_request_until_controller_confirms_actual_outcome(self):
        self.dialog.cancel_button.click()
        self.dialog.cancel_button.click()
        self.assertEqual(self.cancelled, [True])
        self.assertEqual(self.dialog.status_label.text(), 'Отмена…')
        self.assertFalse(self.dialog.cancel_button.isEnabled())
        self.assertFalse(self.dialog.set_progress(2, 10))
        self.assertFalse(self.dialog.set_status('downloading'))
        self.assertTrue(self.dialog.set_status('cancelled'))
        self.assertEqual(self.dialog.status_label.text(), 'Скачивание отменено')
        self.assertEqual(self.dialog.cancel_button.text(), 'Закрыть')
        self.assertTrue(self.dialog.cancel_button.isEnabled())

    def test_done_can_win_local_cancel_race_but_terminal_outcome_cannot_regress(self):
        self.dialog.cancel_button.click()
        self.assertTrue(self.dialog.set_status('done'))
        self.assertFalse(self.dialog.set_status('cancelled'))
        self.assertFalse(self.dialog.set_status('error'))
        self.assertFalse(self.dialog.set_progress(1, 10))
        self.assertEqual(self.dialog.status_label.text(), 'Файл сохранён')

    def test_controller_cancelling_waits_for_worker_ack_without_emitting_another_command(self):
        self.dialog.set_progress(3, 10)
        self.assertTrue(self.dialog.set_status('cancelling'))
        self.assertEqual(self.dialog.status_label.text(), 'Отмена…')
        self.assertTrue(self.dialog._cancel_pending)
        self.assertFalse(self.dialog.cancel_button.isEnabled())
        self.dialog.cancel_button.click()
        self.assertEqual(self.cancelled, [])
        self.assertFalse(self.dialog.set_progress(4, 10))
        self.assertFalse(self.dialog.set_status('downloading'))
        self.assertTrue(self.dialog.set_status('cancelling'))
        self.assertTrue(self.dialog.set_status('error'))
        self.assertEqual(self.dialog.status_label.text(), 'Не удалось скачать файл')
        self.assertEqual(self.dialog.cancel_button.text(), 'Закрыть')

    def test_close_and_escape_cancel_once_and_reject_late_updates(self):
        QTest.keyClick(self.dialog, Qt.Key.Key_Escape)
        self.assertEqual(self.cancelled, [True])
        self.assertFalse(self.dialog.set_status('done'))
        self.assertFalse(self.dialog.set_progress(1, 2))
        self.assertEqual(self.dialog._state, 'connecting')

    def test_message_signal_keeps_original_descriptor_and_has_no_file_or_network_io(self):
        with patch('builtins.open', side_effect=AssertionError('UI must not open files')):
            self.dialog.message_button.click()
            self.dialog.set_progress(2, 10)
            self.dialog.set_status('error')
        self.assertEqual(len(self.messages), 1)
        self.assertIs(self.messages[0], self.descriptor)
        self.assertEqual(self.dialog.status_label.text(), 'Не удалось скачать файл')

    def test_queued_controller_signals_are_disconnected_when_receiver_is_destroyed(self):
        relay = Relay()
        relay.progress.connect(self.dialog.set_progress, Qt.ConnectionType.QueuedConnection)
        relay.status.connect(self.dialog.set_status, Qt.ConnectionType.QueuedConnection)
        relay.progress.emit(3, 10)
        self.app.processEvents()
        self.assertEqual(self.dialog._received, 3)
        relay.progress.emit(4, 10)
        relay.status.emit('done')
        self.dialog.deleteLater()
        QCoreApplication.sendPostedEvents(self.dialog, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        relay.progress.emit(5, 10)
        self.app.processEvents()
        self.assertFalse(isValid(self.dialog))
        self.assertFalse(self.dialog.set_progress(6, 10))

    def test_unsafe_confirmation_has_default_cancel_plain_name_and_no_remember_option(self):
        warning = UnsafeFileDownloadDialog(self.descriptor)
        self.extra.append(warning)
        warning.show()
        self.app.processEvents()
        self.assertTrue(warning.cancel_button.isDefault())
        self.assertFalse(warning.proceed_button.autoDefault())
        self.assertTrue(warning.cancel_button.hasFocus())
        self.assertEqual(warning.filename_label._full_text, self.descriptor.file_name)
        self.assertTrue(all(label.textFormat() == Qt.TextFormat.PlainText for label in warning.findChildren(QLabel)))
        QTest.keyClick(warning, Qt.Key.Key_Return)
        self.assertEqual(warning.result(), QDialog.DialogCode.Rejected)
        other = UnsafeFileDownloadDialog(self.descriptor)
        self.extra.append(other)
        other.proceed_button.click()
        self.assertEqual(other.result(), QDialog.DialogCode.Accepted)

    def test_confirm_download_returns_only_explicit_dialog_result(self):
        with patch.object(UnsafeFileDownloadDialog, 'exec', return_value=QDialog.DialogCode.Rejected):
            self.assertFalse(confirm_download(self.descriptor))
        with patch.object(UnsafeFileDownloadDialog, 'exec', return_value=QDialog.DialogCode.Accepted):
            self.assertTrue(FileDownloadDialog.confirm_download(self.descriptor))

    def test_cancel_receiver_can_destroy_dialog_during_close(self):
        def dispose():
            self.dialog.deleteLater()
            QCoreApplication.sendPostedEvents(self.dialog, QEvent.Type.DeferredDelete)
        self.dialog.cancel_requested.connect(dispose)
        self.dialog.close()
        self.assertFalse(isValid(self.dialog))
        self.assertEqual(self.cancelled, [True])

    def test_confirm_cleanup_accepts_parent_destroying_modal_during_nested_loop(self):
        def disposed_exec(dialog):
            dialog.deleteLater()
            QCoreApplication.sendPostedEvents(dialog, QEvent.Type.DeferredDelete)
            return QDialog.DialogCode.Rejected
        with patch.object(UnsafeFileDownloadDialog, 'exec', disposed_exec):
            self.assertFalse(confirm_download(self.descriptor))

    def test_scoped_light_dark_progress_and_warning_synthetic_captures(self):
        output = Path('.test-tmp')
        output.mkdir(exist_ok=True)
        self.dialog.set_progress(6553600, 10485760)
        for theme in ('light', 'dark'):
            self.dialog.apply_theme(theme)
            warning = UnsafeFileDownloadDialog(self.descriptor, theme=theme)
            self.extra.append(warning)
            warning.show()
            for width in (480, 320):
                self.dialog.resize(width, 264)
                warning.resize(width, 255)
                self.app.processEvents()
                for widget in (self.dialog, warning):
                    self.assertLessEqual(widget.filename_label.width(), widget.width() - 40)
                    for child in widget.findChildren(QLabel):
                        self.assertTrue(widget.rect().contains(child.mapTo(widget, child.rect().bottomRight())))
                self.assertTrue(self.dialog.grab().save(str(output / f'file_download_progress_{width}_{theme}.png')))
                self.assertTrue(warning.grab().save(str(output / f'file_download_warning_{width}_{theme}.png')))
            warning.hide()


if __name__ == '__main__':
    unittest.main()
