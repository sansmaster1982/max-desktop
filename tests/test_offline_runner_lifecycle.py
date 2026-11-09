"""Offline runner isolation checks, synthetic widgets only; no application data."""
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication, QWidget, QStyleFactory
from shiboken6 import isValid

from qt_test_lifecycle import QtLifecycleTextResult, capture_qt_state, restore_qt_state


def run_cases(*cases):
    return unittest.TextTestRunner(stream=io.StringIO(), verbosity=0,
                                   resultclass=QtLifecycleTextResult).run(unittest.TestSuite(cases))


class OfflineRunnerLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_one_hundred_deferred_widgets_are_deleted_and_prior_root_is_preserved(self):
        prior = QWidget()
        born = []
        class Case(unittest.TestCase):
            def runTest(inner):
                born.extend(QWidget() for _ in range(100))
                for widget in born:
                    widget.close()
                    widget.deleteLater()
                self.app.processEvents()
                self.assertEqual(sum(isValid(widget) for widget in born), 100)
        try:
            result = run_cases(Case())
            self.assertTrue(result.wasSuccessful())
            self.assertTrue(isValid(prior))
            self.assertEqual(sum(isValid(widget) for widget in born), 0)
        finally:
            prior.deleteLater()
            QCoreApplication.sendPostedEvents(prior, QEvent.Type.DeferredDelete)

    def test_global_stylesheet_font_and_palette_are_restored(self):
        before = capture_qt_state()
        class Case(unittest.TestCase):
            def runTest(inner):
                self.app.setStyleSheet('QWidget { color: #f10203; }')
                self.app.setFont(QFont('Synthetic family', 27))
                palette = QPalette(self.app.palette())
                palette.setColor(QPalette.ColorRole.Window, QColor('#123456'))
                self.app.setPalette(palette)
                QWidget().show()
        result = run_cases(Case())
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(self.app.styleSheet(), before.stylesheet)
        self.assertEqual(self.app.font(), before.font)
        self.assertEqual(self.app.palette(), before.palette)

    def test_factory_style_is_restored_when_it_can_be_identified(self):
        before = capture_qt_state()
        try:
            self.app.setStyleSheet('')
            self.app.setStyle('Fusion')
            alternate = next((key for key in QStyleFactory.keys() if key.casefold() != 'fusion'), None)
            if alternate is None:
                self.skipTest('No second Qt factory style is available')
            class Case(unittest.TestCase):
                def runTest(inner):
                    self.app.setStyle(alternate)
            result = run_cases(Case())
            self.assertTrue(result.wasSuccessful())
            self.assertEqual(self.app.style().objectName().casefold(), 'fusion')
        finally:
            restore_qt_state(before)

    def test_original_failure_and_cleanup_error_both_remain_visible(self):
        class Case(unittest.TestCase):
            def runTest(inner):
                inner.fail('synthetic assertion failure')
        with patch('qt_test_lifecycle.restore_qt_state', side_effect=RuntimeError('synthetic cleanup error')):
            result = run_cases(Case())
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(len(result.errors), 1)
        self.assertFalse(result.wasSuccessful())

    def test_broken_close_does_not_prevent_other_deletes_or_global_restoration(self):
        before = capture_qt_state()
        born = []
        class BrokenClose(QWidget):
            def close(inner):
                raise RuntimeError('synthetic close failure')
        class Case(unittest.TestCase):
            def runTest(inner):
                born.extend((BrokenClose(), QWidget()))
                for widget in born:
                    widget.show()
                self.app.setStyleSheet('QWidget { color: #f10203; }')
                self.app.setFont(QFont('Synthetic family', 27))
        result = run_cases(Case())
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.errors), 1)
        self.assertIn('synthetic close failure', result.errors[0][1])
        self.assertTrue(all(not isValid(widget) for widget in born))
        self.assertEqual(self.app.styleSheet(), before.stylesheet)
        self.assertEqual(self.app.font(), before.font)
        self.assertEqual(self.app.palette(), before.palette)

    def test_uncaught_qt_timer_and_slot_errors_are_counted_with_original_tracebacks(self):
        problems = [RuntimeError('synthetic timer failure'), ValueError('synthetic signal failure')]
        captured = []
        previous_hook = sys.excepthook
        class Emitter(QObject):
            trigger = Signal()
        class Case(unittest.TestCase):
            def runTest(inner):
                def timer_error():
                    raise problems[0]
                def signal_error():
                    raise problems[1]
                emitter = Emitter()
                emitter.trigger.connect(signal_error)
                emitter.trigger.emit()
                QTimer.singleShot(0, timer_error)
                self.app.processEvents()
        class CaptureResult(QtLifecycleTextResult):
            def addError(inner, test, error):
                captured.append(error)
                super().addError(test, error)
        result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0,
                                         resultclass=CaptureResult).run(unittest.TestSuite([Case()]))
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.errors), 2)
        self.assertEqual({id(error[1]) for error in captured}, {id(error) for error in problems})
        self.assertTrue(all(error[2] is not None for error in captured))
        self.assertIs(sys.excepthook, previous_hook)

    def test_pending_delete_for_a_prior_root_is_not_flushed_by_cleanup(self):
        prior = QWidget()
        state = capture_qt_state()
        prior.deleteLater()
        new = QWidget()
        restore_qt_state(state)
        self.assertTrue(isValid(prior))
        self.assertFalse(isValid(new))
        QCoreApplication.sendPostedEvents(prior, QEvent.Type.DeferredDelete)

    def test_application_created_inside_test_has_all_new_roots_cleaned(self):
        code = '''
import sys
sys.path.insert(0, sys.argv[1])
from qt_test_lifecycle import capture_qt_state, restore_qt_state
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import isValid
assert QApplication.instance() is None
state = capture_qt_state()
app = QApplication([])
roots = [QWidget() for _ in range(10)]
restore_qt_state(state)
assert all(not isValid(widget) for widget in roots)
assert len(app.topLevelWidgets()) == 0
print('new application cleanup passed')
'''
        result = subprocess.run([sys.executable, '-c', code, str(Path(__file__).resolve().parent)],
                                capture_output=True, text=True, timeout=10,
                                env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen'})
        self.assertEqual(result.returncode, 0, 'Synthetic child cleanup failed')
        self.assertIn('new application cleanup passed', result.stdout)


if __name__ == '__main__':
    unittest.main()
