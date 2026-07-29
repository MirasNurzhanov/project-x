import requests
from django.utils import timezone
from datetime import timedelta
from .models import Notification

NEARBY_COOLDOWN_MINUTES = 60
EXPO_PUSH_URL = 'https://exp.host/--/api/v2/push/send'


def send_push_notification(user, title, message):
    token = user.push_token
    if not token:
        return
    print(f'[Push] sending to token: {token}')
    print(f'[Push] title: {title}, body: {message}')
    response = requests.post(
        EXPO_PUSH_URL,
        json={'to': token, 'title': title, 'body': message},
        headers={'Content-Type': 'application/json'},
        timeout=5,
    )
    print(f'[Push] Expo API response: {response.json()}')


REACTION_VERB = {
    'like': 'liked',
    'funny': 'found your story funny',
    'helpful': 'found your story helpful',
    'interesting': 'found your story interesting',
}


def _story_alert(recipient, sender, story, notification_type, message, push_title):
    """Create a story alert, unless the two are blocked or it is self-directed.

    Reacting to or replying to your own story is not news, and a block hides
    both directions — so neither produces a row. Push is best-effort: a failed
    Expo call must not roll back the reaction or the reply that caused it.
    """
    from users.visibility import blocked_ids_for

    if recipient.pk == sender.pk:
        return None
    if sender.pk in blocked_ids_for(recipient):
        return None

    notification = Notification.objects.create(
        recipient=recipient,
        sender=sender,
        story=story,
        notification_type=notification_type,
        message=message,
    )
    try:
        send_push_notification(recipient, push_title, message)
    except Exception as exc:  # noqa: BLE001 — push is never worth a 500
        print(f'[Push] story alert failed: {exc}')
    return notification


def notify_story_reaction(story, sender, reaction: str):
    """Someone reacted to `story`.

    A reaction toggles and can be switched, and `StoryReaction` allows one per
    person per story — so one person is one piece of news here, however many
    times they tap. An unread alert from the same person on the same story is
    rewritten in place rather than joined by another: the author sees their
    current reaction, once, and is not pushed at again for changing their mind.
    """
    verb = REACTION_VERB.get(reaction, 'reacted to your story')
    message = f'@{sender.username} {verb}' + (' your story' if reaction == 'like' else '')

    standing = Notification.objects.filter(
        recipient=story.user,
        sender=sender,
        story=story,
        notification_type=Notification.Type.STORY_REACTION,
        is_read=False,
    )
    # `created_at` is auto_now_add, so it only moves through an UPDATE — which
    # is what keeps a re-reaction at the top of the list where it belongs.
    if standing.update(message=message, created_at=timezone.now()):
        return None

    return _story_alert(
        recipient=story.user,
        sender=sender,
        story=story,
        notification_type=Notification.Type.STORY_REACTION,
        message=message,
        push_title='❤️ Story reaction',
    )


def notify_story_reply(story, sender, comment: str):
    """Someone replied to `story`. The reply itself travels as a DM; this is the
    trace that ties that message back to the story it answers."""
    excerpt = (comment or '').strip()
    if len(excerpt) > 80:
        excerpt = excerpt[:80].rstrip() + '…'
    message = f'@{sender.username} replied to your story'
    if excerpt:
        message = f'{message}: {excerpt}'
    return _story_alert(
        recipient=story.user,
        sender=sender,
        story=story,
        notification_type=Notification.Type.STORY_REPLY,
        message=message,
        push_title='💬 Story reply',
    )


def notify_nearby_match(user, nearby_user, distance_km: float):
    """
    Create a NEARBY_USER notification for both users.
    Skipped if the same pair was already notified within the cooldown window.
    """
    from users.visibility import can_surface_in_proximity

    cutoff = timezone.now() - timedelta(minutes=NEARBY_COOLDOWN_MINUTES)

    # This writes to *both* accounts, but only one direction was checked by the
    # caller's queryset. Without the second check, someone who opted out of
    # interest discovery would still be announced to a stranger the moment that
    # stranger moved.
    pairs = [
        (user, nearby_user, nearby_user.username),
        (nearby_user, user, user.username),
    ]
    for recipient, sender, sender_name in pairs:
        if not can_surface_in_proximity(recipient, sender):
            continue
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
