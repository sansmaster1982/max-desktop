"""Offscreen smoke test — строит весь UI без дисплея и без сети."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="maxsmoke_")

from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtCore import QTimer  # noqa: E402

from maxclient.core.config import AppPaths, Session, Settings  # noqa: E402
from maxclient.core.service import MaxService  # noqa: E402
from maxclient.core.models import Attach, Chat, Message  # noqa: E402
from maxclient.ui.theme import build_stylesheet  # noqa: E402

results = []


def check(name, fn):
    try:
        fn()
        results.append((name, True, ""))
        print(f"PASS  {name}")
    except Exception as e:  # noqa: BLE001
        import traceback
        results.append((name, False, str(e)))
        print(f"FAIL  {name}: {e}")
        traceback.print_exc()


_held = []


def _hold(w):
    _held.append(w)
    return w


app = QApplication.instance() or QApplication(sys.argv)
app.setStyle("Fusion")

paths = AppPaths()
settings = Settings.load(paths)
session = Session.load(paths)

check("stylesheet dark", lambda: build_stylesheet("dark", "#2B7FFF"))
check("stylesheet light", lambda: build_stylesheet("light", "#7C5CFF"))
app.setStyleSheet(build_stylesheet(settings.theme, settings.accent))

service = MaxService(paths, settings, session)

# Засеять кэш фейковым чатом и сообщениями всех типов.
def seed():
    service.store.upsert_chat(Chat(id=42, title="Тест Контакт", last_preview="привет"))
    service.store.insert_message(Message(chat_id=42, id=1, sender=7, text="Привет!", time_ms=1716900000000))
    service.store.insert_message(Message(chat_id=42, id=2, sender=None, text="Как дела?", time_ms=1716900100000, outgoing=True, status="sent"))
    # картинка с локальным путём
    img = os.path.join(paths.media_dir, "t.png")
    try:
        from PIL import Image
        Image.new("RGB", (64, 48), (90, 140, 255)).save(img)
    except Exception:
        img = ""
    service.store.insert_message(Message(chat_id=42, id=3, sender=7, time_ms=1716900200000,
        attaches=[Attach(type="PHOTO", local_path=img)]))
    service.store.insert_message(Message(chat_id=42, id=4, sender=7, time_ms=1716900300000,
        attaches=[Attach(type="AUDIO", duration_ms=4200)]))
    service.store.insert_message(Message(chat_id=42, id=5, sender=7, time_ms=1716900400000,
        attaches=[Attach(type="FILE", file_name="doc.pdf", size=120000, file_id=99)]))
check("seed store", seed)

check("login window", lambda: _hold(__import__("maxclient.ui.login_window", fromlist=["LoginWindow"]).LoginWindow(service, settings)))
check("main window", lambda: _hold(__import__("maxclient.ui.main_window", fromlist=["MainWindow"]).MainWindow(service, settings)))
check("settings dialog", lambda: _hold(__import__("maxclient.ui.settings_dialog", fromlist=["SettingsDialog"]).SettingsDialog(service, settings)))
check("contacts dialog", lambda: _hold(__import__("maxclient.ui.contacts_dialog", fromlist=["ContactsDialog"]).ContactsDialog(service)))


def chat_view_open():
    from maxclient.ui.chat_view import ChatView
    cv = ChatView(service, settings)
    cv.open_chat(service.store.get_chat(42))
    _hold(cv)
check("chat view + render all bubbles", chat_view_open)


def media_viewer_image():
    from maxclient.ui.media import MediaViewer, MULTIMEDIA_AVAILABLE
    assert MULTIMEDIA_AVAILABLE, "QtMultimedia should be available"
    img = os.path.join(paths.media_dir, "t.png")
    if os.path.isfile(img):
        _hold(MediaViewer(img, Attach(type="PHOTO", local_path=img), settings))
check("media viewer (image)", media_viewer_image)


def voice_recorder_build():
    from maxclient.ui.media import VoiceRecorder, MULTIMEDIA_AVAILABLE
    if MULTIMEDIA_AVAILABLE and VoiceRecorder is not None:
        VoiceRecorder(settings)  # только конструирование, без записи
check("voice recorder build", voice_recorder_build)

# Прокрутить очередь событий, чтобы отработали run_async-задачи и таймеры.
QTimer.singleShot(600, app.quit)
app.exec()

service.shutdown()

ok = sum(1 for _, p, _ in results if p)
print(f"\n=== {ok}/{len(results)} passed ===")
sys.exit(0 if ok == len(results) else 1)
