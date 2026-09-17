from django.db.models import Q

from users.models import User
from .models import BlockedUser, HiRequest


def lock_participants(first_id, second_id):
    """Call inside atomic(); all lifecycle writers take user locks in PK order."""
    return list(User.objects.select_for_update().filter(pk__in=[first_id, second_id]).order_by('pk'))


def lock_writable_conversation(first_id, second_id):
    users = lock_participants(first_id, second_id)
    if len(users) != 2 or any(not u.is_active or u.deletion_requested_at or u.retired_account_id for u in users):
        return False
    pair = Q(sender_id=first_id, receiver_id=second_id) | Q(sender_id=second_id, receiver_id=first_id)
    if BlockedUser.objects.filter(
        Q(blocker_id=first_id, blocked_id=second_id) | Q(blocker_id=second_id, blocked_id=first_id)
    ).exists():
        return False
    return HiRequest.objects.filter(pair, status='accepted').exists()
