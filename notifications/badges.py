"""Navigation counts without loading notifications, messages or user profiles."""
from django.db.models import Count, Exists, OuterRef, Q, Subquery, Value
from django.db.models.functions import Coalesce

from friends.models import BlockedUser, FriendRequest, HiRequest, Message
from users.models import User
from .models import Notification


def navigation_badges(user):
    # Apply blocks at read time in both directions. EXISTS avoids materializing
    # an arbitrarily large block list and does not duplicate counted rows.
    blocked = BlockedUser.objects.filter(
        Q(blocker_id=user.pk, blocked_id=OuterRef('sender_id'))
        | Q(blocked_id=user.pk, blocker_id=OuterRef('sender_id'))
    )

    def count(qs, recipient_field):
        grouped = (qs.filter(~Exists(blocked)).order_by()
                   .values(recipient_field).annotate(total=Count('pk'))
                   .values('total'))
        return Coalesce(Subquery(grouped[:1]), Value(0))

    counts = User.objects.filter(pk=user.pk).annotate(
        chat_count=count(Message.objects.filter(
            receiver_id=user.pk, is_read=False, deleted_for_receiver=False,
        ), 'receiver_id'),
        hi_count=count(HiRequest.objects.filter(
            receiver_id=user.pk, status=HiRequest.Status.PENDING,
        ), 'receiver_id'),
        friend_count=count(FriendRequest.objects.filter(
            receiver_id=user.pk, status=FriendRequest.Status.PENDING,
        ), 'receiver_id'),
        activity_count=count(Notification.objects.filter(
            recipient_id=user.pk, is_read=False,
        ).exclude(notification_type__in=(
            'proximity', 'nearby', 'nearby_user', 'wave',
        )), 'recipient_id'),
    ).values('chat_count', 'hi_count', 'friend_count', 'activity_count').get()
    return {
        'chat': counts['chat_count'],
        'alerts': counts['hi_count'] + counts['friend_count'] + counts['activity_count'],
    }
