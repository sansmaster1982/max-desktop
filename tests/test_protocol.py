"""Логические тесты парсинга протокола на синтетических данных (без сети)."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["APPDATA"] = tempfile.mkdtemp(prefix="maxlogic_")

from maxclient.protocol import raw_parsers as rp
from maxclient.protocol.client import MaxFrame
from maxclient.protocol.uploader import _extract_token, _extract_upload_url, build_attach, AttachKind, UploadResult
from maxclient.core.config import AppPaths, Session, Settings
from maxclient.core.service import MaxService
from maxclient.core.models import Message

fails = []


def eq(name, got, want):
    if got == want:
        print(f"PASS  {name}")
    else:
        print(f"FAIL  {name}: got {got!r} want {want!r}")
        fails.append(name)


# 1. chat ids из LOGIN raw
raw = (
    b"\xa2id\xd2" + (12345).to_bytes(4, "big") + b"\xa4typeDIALOG"
    + b"\xa6chatId\xd2" + (67890).to_bytes(4, "big")
)
ids = rp.extract_chat_ids_from_login_raw(raw)
eq("login chat ids", sorted(ids), [12345, 67890])

# 2. read int/str after key
import msgpack
blob = msgpack.packb({"id": 9, "text": "Привет мир", "sender": 1000000}, use_bin_type=True)
eq("read text cyrillic", rp.read_str_after_key(blob, b"\xa4text"), "Привет мир")
eq("read big int", rp.read_int_after_key(blob, b"\xa6sender"), 1000000)

# 3. push parsing
paths = AppPaths()
svc = MaxService(paths, Settings.load(paths), Session.load(paths))
frame = MaxFrame(cmd=0, seq=999, opcode=128, body=b"",
                 decoded={"chatId": 42, "message": {"text": "hi", "sender": 7, "id": 100, "time": 1716900000000}})
msg = svc._parse_push(frame)
eq("push chatId", msg.chat_id, 42)
eq("push text", msg.text, "hi")
eq("push sender", msg.sender, 7)
eq("push id", msg.id, 100)

# push без контента -> None
empty = MaxFrame(cmd=0, seq=1, opcode=130, body=b"", decoded={"chatId": 42, "message": {}})
eq("push empty -> None", svc._parse_push(empty), None)

# 4. Message.from_server с attaches
m = Message.from_server(42, {"id": 5, "sender": 7, "text": "", "time": 1,
    "attaches": [{"_type": "PHOTO", "photoToken": "tok", "baseUrl": "https://i.oneme.ru/x"}]}, my_id=None)
eq("msg attach type", m.attaches[0].type, "PHOTO")
eq("msg attach token", m.attaches[0].token, "tok")
eq("msg attach baseUrl", m.attaches[0].base_url, "https://i.oneme.ru/x")
eq("msg preview", m.preview, "[Фото]")

# 5. uploader token extraction
eq("token photoToken", _extract_token('{"photoToken":"abc"}').token, "abc")
r = _extract_token('{"token":"xyz","videoId":5}')
eq("token videoId", (r.token, r.file_id), ("xyz", 5))
eq("token plain", _extract_token("rawtoken123").token, "rawtoken123")
eq("token result.tokens", _extract_token('{"result":{"tokens":["t1"]}}').token, "t1")

# 6. uploader url extraction
eq("url direct", _extract_upload_url({"url": "http://x"}), "http://x")
eq("url list", _extract_upload_url({"urls": ["http://y"]}), "http://y")
eq("url nested", _extract_upload_url({"result": {"endpoint": "http://z"}}), "http://z")

# 7. build_attach formats
eq("attach PHOTO", build_attach(AttachKind.PHOTO, UploadResult(token="p")), {"_type": "PHOTO", "photoToken": "p"})
eq("attach AUDIO", build_attach(AttachKind.AUDIO, UploadResult(token="a"), duration_ms=3000),
   {"_type": "AUDIO", "token": "a", "duration": 3000})
eq("attach FILE id", build_attach(AttachKind.FILE, UploadResult(file_id=7)), {"_type": "FILE", "fileId": 7})

svc.shutdown()
print(f"\n=== {'ALL PASS' if not fails else str(len(fails)) + ' FAILED'} ===")
sys.exit(1 if fails else 0)
