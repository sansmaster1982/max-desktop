"""Точка входа GUI: бутстрап QApplication, навигация login ↔ main, темы."""
from __future__ import annotations

import sys
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QVBoxLayout, QWidget

from . import __app_id__, __app_name__
from .core.config import AppPaths, Session, Settings
from .core.instance_lock import AlreadyRunning, DataDirectoryLock, InstanceLockError
from .core.service import MaxService
from .ui.assets import app_icon, logo_pixmap
from .ui.async_runner import run_async
from .ui.login_window import LoginWindow
from .ui.main_window import MainWindow
from .ui.theme import build_stylesheet


# Service.shutdown() is not a barrier for every asynchronous job. Keep ownership
# until process teardown, including startup failure, logout and window changes.
_PROCESS_DIRECTORY_LOCKS: list[DataDirectoryLock] = []


class _Splash(QWidget):
    def __init__(self, accent: str) -> None:
        super().__init__()
        self.setWindowTitle(__app_name__)
        self.resize(560, 720)
        lay = QVBoxLayout(self)
        lay.addStretch(1)
        logo = QLabel()
        pix = logo_pixmap(96)
        if pix is not None:
            logo.setPixmap(pix)
        else:
            logo.setText(__app_name__)
            logo.setFixedSize(84, 84)
            logo.setStyleSheet(
                f"background:{accent}; color:white; border-radius:24px;"
                "font-size:24px; font-weight:800;"
            )
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(logo, 0, Qt.AlignmentFlag.AlignHCenter)
        hint = QLabel("Подключение…")
        hint.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        hint.setObjectName("ChatSubtitle")
        lay.addWidget(hint)
        lay.addStretch(2)


def _set_windows_app_identity() -> bool:
    """Group MAXIM windows separately on the taskbar; never changes stored data."""
    if sys.platform != 'win32':
        return False
    try:
        import ctypes
        set_id = ctypes.WinDLL('shell32').SetCurrentProcessExplicitAppUserModelID
        set_id.argtypes = [ctypes.c_wchar_p]
        set_id.restype = ctypes.c_long
        return set_id(__app_id__) == 0
    except (AttributeError, OSError):
        return False


class MaxApp:
    def __init__(self) -> None:
        _set_windows_app_identity()
        if QApplication.instance() is None:
            QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
        self.app = QApplication.instance() or QApplication(sys.argv)
        self.app.setApplicationName(__app_name__)
        self.app.setWindowIcon(app_icon())
        try:
            self.paths = AppPaths()
        except OSError:
            raise InstanceLockError('Data directory is unavailable') from None
        self._instance_lock = DataDirectoryLock.acquire(self.paths.dir)
        _PROCESS_DIRECTORY_LOCKS.append(self._instance_lock)
        self.settings = Settings.load(self.paths)
        self.session = Session.load(self.paths)
        self.service = MaxService(self.paths, self.settings, self.session)

        self.app.setStyle("Fusion")
        self.app.setFont(QFont("Segoe UI", 10))
        self.apply_theme()

        self._login: Optional[LoginWindow] = None
        self._main: Optional[MainWindow] = None
        self._splash: Optional[_Splash] = None
        self._prefer_qr = '--qr' in sys.argv
        self.app.styleHints().colorSchemeChanged.connect(self._system_theme_changed)

    def apply_theme(self) -> None:
        self.app.setStyleSheet(build_stylesheet(self.settings.theme, self.settings.accent))

    def _system_theme_changed(self, _scheme) -> None:
        if self.settings.theme != 'system':
            return
        if self._main is not None:
            self._main._apply_local_theme()
        else:
            self.apply_theme()

    def start(self) -> int:
        if self.session.token:
            self._splash = _Splash(self.settings.accent)
            self._splash.show()
            run_async(self.service.try_restore, on_done=self._after_restore,
                      on_error=lambda _e: self._after_restore(False))
        else:
            self._show_login()
        self.app.aboutToQuit.connect(self._cleanup)
        return self.app.exec()

    def _after_restore(self, ok: bool) -> None:
        splash = self._splash
        if ok:
            self._show_main()
        else:
            self._show_login()
            error = getattr(self.service, 'last_restore_error', None)
            if error and self._login:
                self._login._set_error(str(error))
        # Keep one visible window throughout restoration. Closing the splash
        # first can trigger QApplication's quitOnLastWindowClosed handling.
        if splash:
            splash.close()
            splash.deleteLater()
            self._splash = None

    def _show_login(self) -> None:
        self._login = LoginWindow(self.service, self.settings)
        self._login.logged_in.connect(self._on_logged_in)
        self._login.show()
        if self._prefer_qr:
            self._login.btn_qr.setChecked(True)
            self._login._set_qr_mode()

    def _on_logged_in(self) -> None:
        self.service.log("app: logged_in received -> building main window")
        try:
            self._show_main()
            self.service.log("app: main window shown")
        except Exception as e:  # noqa: BLE001
            self.service.log('app: main window failed: ' + type(e).__name__)
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.critical(
                None, "Ошибка",
                'Вход подтверждён сервером, но не удалось открыть список чатов.',
            )
            return
        # Главное окно построено успешно — только теперь закрываем окно входа.
        if self._login:
            self._login.close()
            self._login = None

    def _show_main(self) -> None:
        self._main = MainWindow(self.service, self.settings)
        self._main.theme_changed.connect(self.apply_theme)
        self._main.logged_out.connect(self._on_logged_out)
        self._main.show()

    def _on_logged_out(self) -> None:
        if self._main:
            self._main.close()
            self._main = None
        self._show_login()

    def _cleanup(self) -> None:
        try:
            self.service.shutdown()
        except Exception:
            pass


def _selftest() -> int:
    """Проверка собранного exe без дисплея: грузит все модули и плагины,
    строит окно входа, проверяет QtMultimedia, пишет лог и завершается."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QTimer

    lines = []
    rc = 0
    app = None
    try:
        app = MaxApp()
        win = LoginWindow(app.service, app.settings)
        win.show()
        from .ui.media import MULTIMEDIA_AVAILABLE

        lines.append(f"multimedia={MULTIMEDIA_AVAILABLE}")
        if MULTIMEDIA_AVAILABLE:
            from PySide6.QtMultimedia import QMediaPlayer

            QMediaPlayer()
            lines.append("QMediaPlayer=ok")
        QTimer.singleShot(300, app.app.quit)
        app.app.exec()
        lines.append("SELFTEST OK")
    except Exception as e:  # noqa: BLE001
        import traceback

        lines.append("SELFTEST FAIL: " + repr(e))
        lines.append(traceback.format_exc())
        rc = 1
    finally:
        if app is not None:
            app._cleanup()

    report = "\n".join(lines)
    print(report)
    log_path = os.environ.get("MAXDESKTOP_SELFTEST_LOG")
    if log_path:
        try:
            with open(log_path, "w", encoding="utf-8") as fh:
                fh.write(report + "\n")
        except OSError:
            pass
    return rc


def main() -> int:
    if "--selftest" in sys.argv:
        return _selftest()
    try:
        app = MaxApp()
    except AlreadyRunning:
        QMessageBox.information(
            None, __app_name__,
            'MAXIM уже запущен с этим профилем. Откройте существующее окно приложения.',
        )
        return 0
    except InstanceLockError:
        QMessageBox.critical(
            None, __app_name__,
            'Не удалось защитить каталог данных MAXIM от одновременного запуска. '
            'Проверьте доступ к каталогу и попробуйте ещё раз.',
        )
        return 1
    return app.start()


if __name__ == "__main__":
    sys.exit(main())
