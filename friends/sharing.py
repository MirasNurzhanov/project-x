"""Sending a Swift to somebody privately.

Forwarding is a direct message that happens to carry a Swift, so all of the
DM rules apply unchanged: you may only send to somebody you are connected to,
a block stops it in either direction, and the conversation is locked while the
row is written. What this module adds is the one rule a text message does not
have.

## The recipient's own right to watch it, checked at send time

A Swift is not public by default. Forwarding a friends-only Swift to somebody
who is not the author's friend would hand them a video the audience rules say
they cannot have — and it would do it from inside a feature that looks like
sharing a link.

So `share` checks the *recipient* against `swifts.visibility.watchable`, not
just the sender. A Swift the recipient could not have found on their own is
refused, and the sender is told plainly rather than the send silently
succeeding into a card that renders as "unavailable".

That check is re-run at read time as well — see `friends.serializers` — because
audience can change after a message is sent, and the message must not become a
standing grant.
"""
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction

from notifications.utils import notify_direct_message

from .lifecycle import lock_writable_conversation
from .models import Message


class ShareRefused(Exception):
    """Why one recipient did not get the Swift.

    Carries a reason a person can read. Raised per recipient rather than for
    the whole call: sharing to five friends where one has since blocked you
    should reach the other four, and say so.
    """

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def can_watch(user, swift):
    """Whether `user` would be allowed to open this Swift on their own.

    Delegates to the feed's own gate rather than re-deriving it, so a change to
    the audience rules cannot leave forwarding behind on an older answer.
    """
    from swifts import visibility as swift_visibility

    return swift_visibility.watchable(user).filter(pk=swift.pk).exists()


def share(sender, recipient, swift, note=''):
    """Send `swift` to `recipient` as a direct message. Returns the Message.

    Raises `ShareRefused` with a readable reason when the send is not allowed.
    The caller is expected to catch it per recipient — see the class note.

    The ordering here matters and mirrors `MessagesView.post`: everything that
    can refuse the send is checked first, the conversation is then locked, and
    the standing checks are re-run inside that lock before a row is written.
    Between the first check and the insert a recipient can block the sender or
    delete their account, and the lock is what makes that a refusal rather than
    a message nobody should have received.
    """
    from .views import _are_connected

    if recipient.pk == sender.pk:
        raise ShareRefused('You cannot send a Swift to yourself.')
    if not _are_connected(sender, recipient):
        raise ShareRefused('You are not connected to this person.')
    if not can_watch(recipient, swift):
        # Deliberately vague about *why*. "They are not friends with the
        # author" would tell the sender about a relationship between two other
        # people that neither of them published.
        raise ShareRefused('They cannot watch this Swift.')

    with transaction.atomic():
        if not lock_writable_conversation(sender.pk, recipient.pk):
            raise ShareRefused('This conversation is no longer writable.')
        message = Message.objects.create(
            sender=sender, receiver=recipient, content=note, swift=swift,
        )

    _announce(sender, recipient, message, swift)
    return message


def _announce(sender, recipient, message, swift):
    """Push and live-update, neither of which may fail the send.

    The message is already stored by the time this runs. A push that times out
    or a channel layer that is down is a notification problem, not a reason to
    tell somebody their share did not go through — the recipient will see it
    the next time the thread loads either way.
    """
    try:
        notify_direct_message(sender, recipient, 'sent you a Swift')
    except Exception:  # noqa: BLE001 — an alert is never worth a failed send
        pass

    room_group = f'chat_{min(sender.pk, recipient.pk)}_{max(sender.pk, recipient.pk)}'
    try:
        async_to_sync(get_channel_layer().group_send)(
            room_group,
            {
                'type': 'chat.message',
                'id': message.id,
                'client_id': None,
                'sender_id': sender.pk,
                'content': message.content,
                'image_url': None,
                # Enough for the open thread to draw the card without a refetch.
                # The full payload still comes from the serializer on reload —
                # this is a hint, not the record.
                'swift_id': swift.pk,
                'created_at': message.created_at.isoformat(),
            },
        )
    except Exception:  # noqa: BLE001 — a live update is never worth a failed send
        pass
