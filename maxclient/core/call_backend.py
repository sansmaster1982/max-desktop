"""Confirmed MAX session adapter; no call credential exists until a UI gesture."""
from .call_runtime import CallConfig, CallRuntimeError
from ..protocol.client import ConnectionState, MaxWriteOutcomeUnknown

# Public application identifier in pinned B92rDWM7.js char2519, not a user secret.
PUBLIC_CALLS_APPLICATION_KEY = 'CNHIJPLGDIHBABABA'


class MaxCallBackend:
    def __init__(self, service):
        self.service = service
        self.client = service.client
        self.owner = service._confirmed_profile_id()
        self.device_id = service.client.device_id

    def is_authorized(self):
        try:
            return (self.service._confirmed_profile_id() == self.owner and
                    self.service.client is self.client and
                    self.service.client.device_id == self.device_id and
                    self.service.client.state == ConnectionState.CONNECTED)
        except Exception:
            return False

    def config(self):
        if not self.is_authorized():
            raise CallRuntimeError('Нет подтверждённого соединения MAX.')
        server = self.service.store.load_config(self.owner).get('server', {})
        if not self.is_authorized():
            raise CallRuntimeError('Сессия MAX изменилась.')
        if server.get('p2p-calls-enabled') is not True:
            raise CallRuntimeError('Звонки недоступны в текущей сессии MAX.')
        config = CallConfig(app_version=self.service.settings.web_app_version,
                            api_base_url=server.get('calls-endpoint', ''),
                            api_key=PUBLIC_CALLS_APPLICATION_KEY,
                            domain=server.get('calls-test-domain', ''),
                            fast_hangup=server.get('calls-fast-hangup') is True)
        config.public_dict()
        return config

    def rpc(self, opcode, payload):
        if type(opcode) is not int or opcode not in (158, 78, 166, 167) or not self.is_authorized():
            raise CallRuntimeError('Запрос звонка недоступен.')
        if opcode == 158 and payload is not None:
            raise CallRuntimeError('Некорректный запрос авторизации звонка.')
        # Runtime validates exact fields and binds peer/conversation to the user action.
        # This transport issues one request; it never retries an unknown call start.
        try:
            frame = self.service.client._write_frame(opcode, payload)
        except Exception:
            raise MaxWriteOutcomeUnknown('Результат запроса звонка неизвестен.') from None
        if not self.is_authorized():
            raise MaxWriteOutcomeUnknown('Сессия MAX изменилась во время звонка.')
        if not frame.ok:
            raise CallRuntimeError('MAX отклонил запрос звонка.')
        if frame.opcode != opcode or frame.cmd != 1 or not isinstance(frame.decoded, dict):
            raise MaxWriteOutcomeUnknown('MAX не подтвердил ответ звонка.')
        return frame.decoded
