from django.utils import timezone
from datetime import timedelta
from .models import Notification

NEARBY_COOLDOWN_MINUTES = 60


def notify_nearby_match(user, nearby_user, distance_km: float):
    """
    Create a NEARBY_USER notification for both users.
    Skipped if the same pair was already notified within the cooldown window.
    """
    cutoff = timezone.now() - timedelta(minutes=NEARBY_COOLDOWN_MINUTES)

    pairs = [
        (user, nearby_user, nearby_user.username),
        (nearby_user, user, user.username),
    ]
    for recipient, sender, sender_name in pairs:
        already_notified = Notification.objects.filter(
            recipient=recipient,
            sender=sender,
            notification_type=Notification.Type.NEARBY_USER,
            created_at__gte=cutoff,
        ).exists()
        if not already_notified:
            Notification.objects.create(
                recipient=recipient,
                sender=sender,
                notification_type=Notification.Type.NEARBY_USER,
                message=f'{sender_name} with shared interests is {distance_km} km away.',
            )
