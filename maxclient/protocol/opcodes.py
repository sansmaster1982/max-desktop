"""Опкоды бинарного протокола MAX.

Источники:
  - test5.py (рабочий Python-клиент, реверс telega-to-max)
  - Flutter-клиент Maxim: lib/core/constants.dart
  - docs/MEDIA_OPCODES.md (декомпил APK: defpackage/ewc.java)

Уверенность по медиа-опкодам — см. комментарии. Запросные опкоды клиент→сервер
шлёт в поле opcode заголовка кадра; cmd=1 в ответе означает успех.
"""

# ─────────────────────────── keepalive ───────────────────────────
PING = 1              # keepalive; ewc.java: new ewc("PING", 0, 1). Сервер рвёт
                      # простаивающий сокет (SERVER_PING_TIMEOUT 11..61 c) — шлём
                      # PING чаще таймаута, иначе дроп -> reconnect -> шторм re-auth.

# ─────────────────────────── auth / session ───────────────────────────
INIT = 6              # handshake: {userAgent:{deviceType, locale, appVersion}, deviceId}
PROFILE = 16          # {} -> мой профиль
AUTH_REQUEST = 17     # {phone, type:"START_AUTH"} -> verify token (в raw)
AUTH_CONFIRM = 18     # {token, verifyCode, authTokenType:"CHECK_CODE"} -> auth token | 2FA challenge
LOGIN = 19            # {token, interactive, chatsCount, *Sync...}
LOGOUT = 20           # {}
TWO_FA = 115          # {trackId, password} -> auth token

# ─────────────────────────── contacts ───────────────────────────
CONTACT_INFO = 32          # {contactIds:[int]}
CONTACT_INFO_BY_PHONE = 46  # {phone:str}

# ─────────────────────────── chats / messages ───────────────────────────
CHAT_INFO = 48        # {chatIds:[int]}
CHAT_HISTORY = 49     # {chatId, from, forward|backward}
CHAT_MEDIA = 51       # {chatId, attachTypes:[...], forward, backward, messageId?}
SEND_MESSAGE = 64     # {chatId, message:{text, attaches?}, randomId, notify?}
TYPING = 65           # {chatId, typing:bool}
MSG_EDIT = 67         # {chatId, messageId, text, attachments?}

# ─────────────────────────── media (двухступенчатый upload) ───────────────────────────
PHOTO_UPLOAD = 80     # {count, profile:bool} -> upload URL + photoToken     (уверенность: высокая)
STICKER_UPLOAD = 81   # поля не выявлены                                      (уверенность: средняя)
VIDEO_UPLOAD = 82     # {type, count, uploaderType} uploaderType:VIDEO/VIDEO_MSG/AUDIO (высокая)
VIDEO_PLAY = 83       # {videoId, chatId?, messageId?, token?} -> play URL    (высокая)
FILE_UPLOAD = 87      # {count} (поля не подтверждены полностью)              (средняя)
FILE_DOWNLOAD = 88    # {fileId, chatId, messageId} -> {url, unsafe}          (высокая)
TRANSCRIBE_MEDIA = 202  # {mediaId, chatId, messageId}                        (высокая)

# ─────────────────────────── 2FA management (декомпил ewc.java) ───────────────────────────
AUTH_2FA_DETAILS = 104     # {} -> {password:{enabled:bool, hint:str, email:str}}
AUTH_VALIDATE_PASSWORD = 107  # {trackId, password}
AUTH_VALIDATE_HINT = 108      # {trackId, hint}
AUTH_VERIFY_EMAIL = 109    # {trackId, email?} -> шлёт код на email; {codeLength, blockingDuration}
AUTH_CHECK_EMAIL = 110     # {trackId, verifyCode} -> подтверждение; {trackId, email}
AUTH_SET_2FA = 111         # {trackId, password, hint?, expectedCapabilities:[1], remove2fa?}
AUTH_CREATE_TRACK = 112    # {type:0} -> trackId (uuid)
AUTH_CHECK_PASSWORD = 113  # {trackId, password} проверить текущий пароль 2FA

# ─────────────────────────── sessions / devices ───────────────────────────
SESSIONS_INFO = 96    # {}
SESSIONS_CLOSE = 97   # {sessionIds:[int]}

# ─────────────────────────── server push (приходят без нашего seq) ───────────────────────────
NOTIF_MESSAGE = 128         # новое сообщение (с attaches внутри)
NOTIF_MARK = 130            # обновление прочитанности
NOTIF_ATTACH = 136          # обновление статуса attach (видео доконвертилось и т.п.)
NOTIF_MSG_DELETE = 142      # сообщение удалено
NOTIF_REACTIONS = 155       # реакции изменились
NOTIF_TRANSCRIPTION = 293   # транскрипция аудио готова

PUSH_OPCODES = frozenset(
    {
        NOTIF_MESSAGE,
        NOTIF_MARK,
        NOTIF_ATTACH,
        NOTIF_MSG_DELETE,
        NOTIF_REACTIONS,
        NOTIF_TRANSCRIPTION,
    }
)

# Человекочитаемые имена для логов/отладки.
NAMES = {
    PING: "PING",
    INIT: "INIT",
    PROFILE: "PROFILE",
    AUTH_REQUEST: "AUTH_REQUEST",
    AUTH_CONFIRM: "AUTH_CONFIRM",
    LOGIN: "LOGIN",
    LOGOUT: "LOGOUT",
    TWO_FA: "TWO_FA",
    CONTACT_INFO: "CONTACT_INFO",
    CONTACT_INFO_BY_PHONE: "CONTACT_INFO_BY_PHONE",
    CHAT_INFO: "CHAT_INFO",
    CHAT_HISTORY: "CHAT_HISTORY",
    CHAT_MEDIA: "CHAT_MEDIA",
    SEND_MESSAGE: "SEND_MESSAGE",
    TYPING: "TYPING",
    MSG_EDIT: "MSG_EDIT",
    PHOTO_UPLOAD: "PHOTO_UPLOAD",
    STICKER_UPLOAD: "STICKER_UPLOAD",
    VIDEO_UPLOAD: "VIDEO_UPLOAD",
    VIDEO_PLAY: "VIDEO_PLAY",
    FILE_UPLOAD: "FILE_UPLOAD",
    FILE_DOWNLOAD: "FILE_DOWNLOAD",
    TRANSCRIBE_MEDIA: "TRANSCRIBE_MEDIA",
    AUTH_2FA_DETAILS: "AUTH_2FA_DETAILS",
    AUTH_VALIDATE_PASSWORD: "AUTH_VALIDATE_PASSWORD",
    AUTH_VALIDATE_HINT: "AUTH_VALIDATE_HINT",
    AUTH_VERIFY_EMAIL: "AUTH_VERIFY_EMAIL",
    AUTH_CHECK_EMAIL: "AUTH_CHECK_EMAIL",
    AUTH_SET_2FA: "AUTH_SET_2FA",
    AUTH_CREATE_TRACK: "AUTH_CREATE_TRACK",
    AUTH_CHECK_PASSWORD: "AUTH_CHECK_PASSWORD",
    SESSIONS_INFO: "SESSIONS_INFO",
    SESSIONS_CLOSE: "SESSIONS_CLOSE",
    NOTIF_MESSAGE: "NOTIF_MESSAGE",
    NOTIF_MARK: "NOTIF_MARK",
    NOTIF_ATTACH: "NOTIF_ATTACH",
    NOTIF_MSG_DELETE: "NOTIF_MSG_DELETE",
    NOTIF_REACTIONS: "NOTIF_REACTIONS",
    NOTIF_TRANSCRIPTION: "NOTIF_TRANSCRIPTION",
}


def name(opcode: int) -> str:
    return NAMES.get(opcode, f"OP_{opcode}")
