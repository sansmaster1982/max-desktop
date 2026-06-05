"""Точка входа GUI: бутстрап QApplication, навигация login ↔ main, темы."""
from __future__ import annotations

import sys
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from .core.config import AppPaths, Session, Settings
from .core.service import MaxService
from .ui.assets import app_icon, logo_pixmap
from .ui.async_runner import run_async
from .ui.login_window import LoginWindow
from .ui.main_window import MainWindow
from .ui.theme import build_stylesheet


class _Splash(QWidget):
    def __init__(self, accent: str) -> None:
        super().__init__()
        self.setWindowTitle("MAX Desktop")
        self.resize(560, 720)
        lay = QVBoxLayout(self)
        lay.addStretch(1)
        logo = QLabel()
        pix = logo_pixmap(96)
        if pix is not None:
            logo.setPixmap(pix)
        else:
            logo.setText("MAX")
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


class MaxApp:
    def __init__(self) -> None:
        self.paths = AppPaths()
        self.settings = Settings.load(self.paths)
        self.session = Session.load(self.paths)
        self.service = MaxService(self.paths, self.settings, self.session)

        self.app = QApplication.instance() or QApplication(sys.argv)
        self.app.setApplicationName("MAX Desktop")
        self.app.setWindowIcon(app_icon())
        self.app.setStyle("Fusion")
        self.app.setFont(QFont("Segoe UI", 10))
        self.apply_theme()

        self._login: Optional[LoginWindow] = None
        self._main: Optional[MainWindow] = None
        self._splash: Optional[_Splash] = None

    def apply_theme(self) -> None:
        self.app.setStyleSheet(build_stylesheet(self.settings.theme, self.settings.accent))

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
        if self._splash:
            self._splash.close()
            self._splash = None
        if ok:
            self._show_main()
        else:
            self._show_login()

    def _show_login(self) -> None:
        self._login = LoginWindow(self.service, self.settings)
        self._login.logged_in.connect(self._on_logged_in)
        self._login.show()

    def _on_logged_in(self) -> None:
        self.service.log("app: logged_in received -> building main window")
        try:
            self._show_main()
            self.service.log("app: main window shown")
        except Exception as e:  # noqa: BLE001
            import traceback
            self.service.log(f"app: main window FAILED: {e!r}\n{traceback.format_exc()}")
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.critical(
                None, "Ошибка",
                f"Вход прошёл, но не удалось открыть главное окно:\n{e}",
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
        app.service.shutdown()
        lines.append("SELFTEST OK")
    except Exception as e:  # noqa: BLE001
        import traceback

        lines.append("SELFTEST FAIL: " + repr(e))
        lines.append(traceback.format_exc())
        rc = 1

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
    return MaxApp().start()


if __name__ == "__main__":
    sys.exit(main())
