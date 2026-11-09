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
USER_SETTINGS = 22    # {settings:{user:patch}}; optional config ACK
TWO_FA = 115          # {trackId, password} -> auth token
QR_GET = 288          # no payload -> server-issued qrLink/trackId/ttl/pollingInterval
QR_STATUS = 289       # {trackId} -> {status:{loginAvailable?,expiresAt?,ttl?}}
QR_LOGIN = 291        # {trackId} -> {tokenAttrs,profile?,passwordChallenge?}

# ─────────────────────────── contacts ───────────────────────────
CONTACT_INFO = 32          # {contactIds:[int]}
CONTACT_ACTION = 34        # {contactId,action:BLOCK|UNBLOCK}; ACK contact optional
CONTACTS_LIST = 36         # {status:BLOCKED,from:0,count:100}; first page
BOT_STOP = 119             # {botId:int64,value:true}; positive ACK is VOID
BOT_WEBAPP_OPEN = 160      # {botId,chatId?,startParam?} -> {url,query_id}; scoped app launch
CONTACT_INFO_BY_PHONE = 46  # {phone:str}
CONTACT_PHOTOS = 39       # {contactId, from?, count?}; documented historical schema

# ─────────────────────────── chats / messages ───────────────────────────
CHAT_INFO = 48        # {chatIds:[int]}
CHAT_HISTORY = 49     # {chatId, from?:timestamp, forward?, backward?, getMessages?, getChat?}
CHAT_MARK = 50        # {chatId, type:READ_MESSAGE, messageId?, mark?}
CHATS_LIST = 53       # {marker, count?, filterUnanswered?, noMessages?, chatTypes?}
CHAT_MEDIA = 51       # {chatId, attachTypes:[...], forward, backward, messageId?}
GLOBAL_SEARCH = 60    # {query,count,marker?:int64,type?:ALL|CHANNELS|PUBLIC_CHATS}
MESSAGE_SEARCH = 68   # {query,count,marker?:string}; distinct opaque cursor
COMMON_CHATS = 198    # {userIds:[int64],count?:int32,marker?:timestamp}
SEND_MESSAGE = 64     # {chatId?|userId?, message:{cid?,text?,attaches?,link?}, notify?}
TYPING = 65           # {chatId, type?:TEXT|PHOTO|VIDEO|AUDIO|FILE|STICKER|VIDEO_MSG}
MSG_EDIT = 67         # {chatId, messageId, text, attachments?}
MSG_DELETE = 66       # {chatId, messageIds, postId?, forMe?}; current public web schema
MSG_GET_BY_IDS = 71   # {chatId, messageIds, postId?}
CHAT_TYPING_STOP = 196  # {chatId}
REACTION_SET = 178    # {chatId, messageId, reaction:{reactionType,id}, postId?}
REACTION_REMOVE = 179  # {chatId, messageId, postId?}
REACTIONS_GET = 180   # {chatId, messageIds, postId?}
REACTION_USERS = 181  # {chatId,messageId,reaction?,marker?,count?}; no postId
CALL_HISTORY = 163    # {callHistorySync?}; read-only log, not call initiation
STORY_ARCHIVE = 219   # {owner?:{type,ownerId},count?,marker?:timestamp}; archive read
FOLDERS_LIST = 272    # {folderSync?:timestamp}; list metadata and optional folders
FOLDER_DETAILS = 273  # {folderIds:[str]}; read folder DTOs, not a mutation
FOLDER_UPDATE = 274   # current ordinary create/update folder payload
FOLDERS_ORDER = 275   # {foldersOrder:[str]}
FOLDERS_DELETE = 276  # {folderIds:[str]}

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
SESSIONS_CLOSE = 97   # {} closes all others; ACK {token}; no individual session IDs

# ─────────────────────────── server push (приходят без нашего seq) ───────────────────────────
NOTIF_MESSAGE = 128         # новое сообщение (с attaches внутри)
NOTIF_MARK = 130            # обновление прочитанности
NOTIF_CONFIG = 134          # {config}; owner-bound supplied user/server config
NOTIF_ATTACH = 136          # обновление статуса attach (видео доконвертилось и т.п.)
NOTIF_CALL_START = 137      # typed incoming Fie; no accepted/terminal/expiry claim
NOTIF_MSG_DELETE = 142      # сообщение удалено
NOTIF_REACTIONS = 155       # реакции изменились
NOTIF_TRANSCRIPTION = 293   # транскрипция аудио готова

PUSH_OPCODES = frozenset(
    {
        NOTIF_MESSAGE,
        NOTIF_MARK,
        NOTIF_CONFIG,
        NOTIF_ATTACH,
        NOTIF_CALL_START,
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
    USER_SETTINGS: "USER_SETTINGS",
    TWO_FA: "TWO_FA",
    QR_GET: "QR_GET",
    QR_STATUS: "QR_STATUS",
    QR_LOGIN: "QR_LOGIN",
    CONTACT_INFO: "CONTACT_INFO",
    BOT_STOP: "BOT_STOP",
    BOT_WEBAPP_OPEN: "BOT_WEBAPP_OPEN",
    CONTACT_ACTION: "CONTACT_ACTION",
    CONTACTS_LIST: "CONTACTS_LIST",
    CONTACT_INFO_BY_PHONE: "CONTACT_INFO_BY_PHONE",
    CONTACT_PHOTOS: "CONTACT_PHOTOS",
    CHAT_INFO: "CHAT_INFO",
    CHAT_HISTORY: "CHAT_HISTORY",
    CHAT_MARK: "CHAT_MARK",
    CHATS_LIST: "CHATS_LIST",
    CHAT_MEDIA: "CHAT_MEDIA",
    GLOBAL_SEARCH: "GLOBAL_SEARCH",
    MESSAGE_SEARCH: "MESSAGE_SEARCH",
    COMMON_CHATS: "COMMON_CHATS",
    SEND_MESSAGE: "SEND_MESSAGE",
    TYPING: "TYPING",
    MSG_EDIT: "MSG_EDIT",
    MSG_DELETE: "MSG_DELETE",
    MSG_GET_BY_IDS: "MSG_GET_BY_IDS",
    CHAT_TYPING_STOP: "CHAT_TYPING_STOP",
    REACTION_SET: "REACTION_SET",
    REACTION_REMOVE: "REACTION_REMOVE",
    REACTIONS_GET: "REACTIONS_GET",
    REACTION_USERS: "REACTION_USERS",
    CALL_HISTORY: "CALL_HISTORY",
    STORY_ARCHIVE: "STORY_ARCHIVE",
    FOLDERS_LIST: "FOLDERS_LIST",
    FOLDER_DETAILS: "FOLDER_DETAILS",
    FOLDER_UPDATE: "FOLDER_UPDATE",
    FOLDERS_ORDER: "FOLDERS_ORDER",
    FOLDERS_DELETE: "FOLDERS_DELETE",
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
    NOTIF_CONFIG: "NOTIF_CONFIG",
    NOTIF_ATTACH: "NOTIF_ATTACH",
    NOTIF_CALL_START: "NOTIF_CALL_START",
    NOTIF_MSG_DELETE: "NOTIF_MSG_DELETE",
    NOTIF_REACTIONS: "NOTIF_REACTIONS",
    NOTIF_TRANSCRIPTION: "NOTIF_TRANSCRIPTION",
}


def name(opcode: int) -> str:
    return NAMES.get(opcode, f"OP_{opcode}")
