"""Owner/session-bound invitation text. No request, write, share, or clipboard."""
from ..protocol.client import AuthenticatedSession
from ..protocol.invite_link_contracts import invite_link_text


class InviteLinkServiceMixin:
    def invite_link_text(self, *, context):
        """UI captures context before queueing and copies only on explicit click.

        Contacts+ and Settings both use InviteLinksModal in the retained WEB
        source. Its invite-long is supplied service text, not the profile link.
        """
        if not isinstance(context, AuthenticatedSession):
            raise ValueError('Для приглашения нужна подтверждённая сессия MAX.')
        binding = self._contact_lookup_binding(context)
        return self._history_cache_write(binding, lambda: invite_link_text(
            self.store.load_config(binding[0]).get('server', {})))
