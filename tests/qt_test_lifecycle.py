"""Qt resource isolation for the shared offline test process, never product code."""
from dataclasses import dataclass
import sys
import unittest


def _application():
    widgets = sys.modules.get('PySide6.QtWidgets')
    return widgets.QApplication.instance() if widgets is not None else None


def _valid(widget):
    from shiboken6 import isValid
    return isValid(widget)


def _created_roots(app, prior):
    from shiboken6 import getCppPointer
    # Retaining prior wrappers prevents wrapper collection; checking only valid
    # C++ identities also handles a different wrapper or an address reused after
    # the prior widget was intentionally destroyed by the test.
    prior_addresses = {getCppPointer(widget)[0] for widget in prior if _valid(widget)}
    return [widget for widget in app.topLevelWidgets()
            if getCppPointer(widget)[0] not in prior_addresses]


def _style_name(app):
    from PySide6.QtWidgets import QStyleFactory
    name = app.style().objectName().casefold()
    return next((key for key in QStyleFactory.keys() if key.casefold() == name), None)


@dataclass
class QtState:
    app: object = None
    roots: tuple = ()
    stylesheet: str | None = None
    font: object = None
    palette: object = None
    style_name: str | None = None


def capture_qt_state():
    app = _application()
    if app is None:
        return QtState()
    from PySide6.QtGui import QFont, QPalette
    return QtState(app, tuple(app.topLevelWidgets()), app.styleSheet(),
                   QFont(app.font()), QPalette(app.palette()), _style_name(app))


def restore_qt_state(state):
    """Dispose only roots born in this test and restore pre-existing app globals.

    processEvents alone does not deliver DeferredDelete outside app.exec().
    Flush specifically owned receivers so a prior root's pending delete is not
    executed merely as a side effect of this helper.
    """
    app = _application()
    if app is None:
        return
    if state.app is not None and state.app is not app:
        raise AssertionError('The test replaced its pre-existing QApplication')
    from PySide6.QtCore import QCoreApplication, QEvent
    prior = state.roots if state.app is app else ()
    errors = []
    def attempt(operation):
        try:
            operation()
        except Exception as error:
            errors.append(error)
    for _pass in range(3):
        created = _created_roots(app, prior)
        if not created:
            break
        for widget in created:
            if _valid(widget):
                # Hidden widgets have already been closed or never shown. Do not
                # replay their closeEvent after their test's mocks were removed.
                if widget.isVisible():
                    attempt(widget.close)
                if _valid(widget):
                    attempt(widget.deleteLater)
        for widget in created:
            if _valid(widget):
                attempt(lambda target=widget: QCoreApplication.sendPostedEvents(
                    target, QEvent.Type.DeferredDelete))
        attempt(app.processEvents)
    leftovers = _created_roots(app, prior)
    if leftovers:
        errors.append(AssertionError(f'{len(leftovers)} newly-created Qt roots survived cleanup'))
    if state.app is app:
        def restore_base_style():
            if state.style_name is not None:
                # A stylesheet proxy hides the base style name. Remove its temporary
                # sheet before checking, then restore the exact previous sheet below.
                if _style_name(app) is None and app.styleSheet() != state.stylesheet:
                    app.setStyleSheet('')
                if _style_name(app) is not None and _style_name(app) != state.style_name:
                    app.setStyle(state.style_name)
        attempt(restore_base_style)
        if app.styleSheet() != state.stylesheet:
            attempt(lambda: app.setStyleSheet(state.stylesheet))
        if app.font() != state.font:
            attempt(lambda: app.setFont(state.font))
        if app.palette() != state.palette:
            attempt(lambda: app.setPalette(state.palette))
        attempt(app.processEvents)
    # When a test created the first QApplication, there are no preceding globals
    # to restore or guess. Its widgets still receive exactly the same cleanup.
    if errors:
        raise ExceptionGroup('Qt test cleanup failed', errors)


class QtLifecycleTextResult(unittest.TextTestResult):
    def startTest(self, test):
        super().startTest(test)
        self._qt_async_errors = []
        self._qt_previous_excepthook = sys.excepthook
        def capture_uncaught(kind, error, traceback):
            # PySide reports uncaught timer/slot exceptions here instead of
            # raising through unittest. Keep the original objects/traceback and
            # count them below, rather than merely printing an uncounted error.
            self._qt_async_errors.append((kind, error, traceback))
        sys.excepthook = capture_uncaught
        self._qt_state = capture_qt_state()

    def stopTest(self, test):
        try:
            restore_qt_state(self._qt_state)
        except Exception:
            # A cleanup defect is an actual test error, never a passing run or an
            # excuse to suppress the original assertion failure.
            self.addError(test, sys.exc_info())
        finally:
            sys.excepthook = self._qt_previous_excepthook
            for error in self._qt_async_errors:
                self.addError(test, error)
            self._qt_async_errors = []
            self._qt_state = None
            super().stopTest(test)
