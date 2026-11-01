"""Saying that somebody joined a club: a line in its chat, and a push.

Both best-effort. Joining is what the member asked for, and a channel layer or
push service that is down must not undo it — so a failure here is logged and
the membership stands.
"""
import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from notifications.utils import notify_club_member_joined

from .models import ClubMessage

logger = logging.getLogger(__name__)


def announce(club, user):
    try:
        from .serializers import ClubMessageSerializer

        message = ClubMessage.objects.create(
            club=club, sender=user, message_type=ClubMessage.MessageType.MEMBER_JOINED,
        )
        async_to_sync(get_channel_layer().group_send)(
            f'club_{club.pk}',
            {'type': 'club.message', 'message_data': dict(ClubMessageSerializer(message).data)},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning('club_join_message_failed', extra={'error_type': type(exc).__name__})
    try:
        notify_club_member_joined(club, user)
    except Exception as exc:  # noqa: BLE001
        logger.warning('club_join_alert_failed', extra={'error_type': type(exc).__name__})
