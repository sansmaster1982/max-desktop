"""Session-bound local message delivery; never contains a LOGIN credential."""
from dataclasses import dataclass, field

from ..protocol.client import AuthenticatedSession
from .models import Message


@dataclass(frozen=True)
class MessageDelivery:
    owner_id: int
    source_client: object = field(repr=False, compare=False)
    session_context: AuthenticatedSession = field(repr=False, compare=False)
    message: Message = field(repr=False, compare=False)


@dataclass(frozen=True)
class MessageChange:
    """Cache invalidation for exact server IDs, without message bodies."""
    owner_id: int
    source_client: object = field(repr=False, compare=False)
    session_context: AuthenticatedSession = field(repr=False, compare=False)
    chat_id: int
    message_ids: tuple[int, ...]
