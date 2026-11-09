"""Passive incoming-call notices. No sockets, SDK, media or server writes."""
from dataclasses import dataclass, field

from ..protocol.client import AuthenticatedSession


@dataclass(frozen=True)
class IncomingCallNotice:
    owner_id: int
    source_client: object = field(repr=False, compare=False)
    session_context: AuthenticatedSession = field(repr=False, compare=False)
    caller_id: int
    chat_id: int
    conversation_id: str = field(repr=False)
    call_type: str
    vcp: str = field(repr=False)

    def sdk_payload(self):
        """Only explicit user action may pass these opaque parameters to SDK."""
        return {'conversationId': self.conversation_id, 'vcp': self.vcp}


def parse_incoming_call(data, *, source_client, session_context):
    """Project required Fie137 fields, ignoring unsupported optional metadata.

    Bounds are local resource limits. Fie has no ringing timestamp/expiry; a
    notice is not proof that the remote call is still active or accepted.
    """
    if not isinstance(data, dict) or not isinstance(session_context, AuthenticatedSession):
        return None
    if any(type(data.get(key)) is not int or not -(2**63) <= data[key] < 2**63
           for key in ('callerId', 'chatId')):
        return None
    if data.get('type') not in ('AUDIO', 'VIDEO'):
        return None
    for key, maximum in (('conversationId', 256), ('vcp', 262144)):
        if not isinstance(data.get(key), str) or not 1 <= len(data[key]) <= maximum:
            return None
    return IncomingCallNotice(session_context.owner_id, source_client, session_context,
                              data['callerId'], data['chatId'], data['conversationId'],
                              data['type'], data['vcp'])
