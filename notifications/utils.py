from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone
from datetime import timedelta

from .models import Notification
from .push import (  # noqa: F401
    EXPO_PUSH_URL, push_to_devices, push_to_user, push_to_users,
)

NEARBY_COOLDOWN_MINUTES = 60


def send_push_notification(user, title, message, *, data=None, category='default'):
    """Fire-and-forget push to one person.

    The transport moved to `notifications.push`; this stays because three
    call sites import it by this name, and because "push one person" is
    still the common case. `data` is the deep link — see push.push_to_users.
    """
    push_to_user(user, title, message, data=data, category=category)


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
        send_push_notification(
            recipient, push_title, message,
            data={
                'type': 'story',
                'story_id': story.pk if story else None,
                'user_id': sender.pk,
                'username': sender.username,
            },
            category='story',
        )
    except Exception as exc:  # noqa: BLE001 — push is never worth a 500
        print(f'[Push] story alert failed: {exc}')
    return notification


def notify_story_reaction(story, sender):
    """Someone liked `story`.

    `StoryReaction` allows one per person per story — so one person is one
    piece of news here, however many times they tap. An unread alert from the
    same person on the same story is rewritten in place rather than joined by
    another: the author sees that this person liked it, once, and is not
    pushed at again for tapping it a second time.
    """
    message = f'@{sender.username} liked your story'

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


#: A music zone is a place you stand in, not an event, so someone pacing across
#: its edge must not produce a stream of alerts. Longer than the nearby cooldown
#: because a track is the same track an hour later.
ZONE_MUSIC_COOLDOWN_MINUTES = 90


def _recently_alerted(recipient, sender, notification_type, minutes):
    cutoff = timezone.now() - timedelta(minutes=minutes)
    return Notification.objects.filter(
        recipient=recipient,
        sender=sender,
        notification_type=notification_type,
        created_at__gte=cutoff,
    ).exists()


def _zone_music_alert(recipient, sender, message, push_title):
    if _recently_alerted(
        recipient, sender, Notification.Type.ZONE_MUSIC, ZONE_MUSIC_COOLDOWN_MINUTES,
    ):
        return None
    notification = Notification.objects.create(
        recipient=recipient,
        sender=sender,
        notification_type=Notification.Type.ZONE_MUSIC,
        message=message,
    )
    try:
        send_push_notification(
            recipient, push_title, message,
            data={'type': 'profile', 'user_id': sender.pk, 'username': sender.username},
            category='zone_music',
        )
    except Exception as exc:  # noqa: BLE001 — push is never worth a 500
        print(f'[Push] zone music alert failed: {exc}')
    return notification


def notify_zone_music_entry(visitor, owner, distance_m: float):
    """`visitor` is standing inside `owner`'s music zone.

    Two alerts, because the two people learn different things:

      - the owner learns a friend is close enough to hear what they are playing,
      - the visitor learns there is something here to play at all, which is the
        half that used to be a banner floating over the map and autoplaying
        before anyone had read it.

    Neither is sent to someone who turned music-zone alerts off, and the
    visitor's half additionally requires that they are allowed to hear the
    track — being told about music you are not permitted to play is worse than
    silence. Both sides share one cooldown row per direction.
    """
    from users.visibility import blocked_ids_for, can_view

    if visitor.pk == owner.pk:
        return
    # A track, and a music zone switched on to play it through. Still not
    # `zone_active`: that governs whether the *map* zone is drawn around
    # someone, and reading it here meant removing that circle also stopped the
    # alerts for music the owner was still broadcasting. Switching the music
    # zone off silences both halves — there is no zone left to walk into.
    if not owner.broadcasts_zone_track:
        return
    if visitor.pk in blocked_ids_for(owner):
        return

    # An upload has no name worth printing, so both sentences are written to
    # survive an empty title rather than filling it with a filename.
    title = owner.zone_track_title
    near = f'{int(round(distance_m))}m' if distance_m < 1000 else f'{distance_m / 1000:.1f}km'

    if owner.music_zone_alerts:
        heard = f'in earshot of {title}' if title else 'in earshot of your track'
        _zone_music_alert(
            recipient=owner,
            sender=visitor,
            message=f'@{visitor.username} is in your music zone — {near} away, {heard}.',
            push_title='🎵 Someone is in your music zone',
        )

    if visitor.music_zone_alerts and can_view(visitor, owner, 'zone_audio'):
        playing = f'{title} is playing here' if title else 'there is something playing here'
        _zone_music_alert(
            recipient=visitor,
            sender=owner,
            message=f"You're in @{owner.username}'s music zone — {playing}.",
            push_title='🎵 There is music where you are',
        )


def notify_nearby_matches(user, matches):
    """Nearby-match alerts for one sweep, in a fixed number of queries.

    `matches` is `[(other_user, distance_km), ...]` — everyone `user` was found
    standing near on this location report.

    The per-pair version of this ran roughly ten queries each: two relationship
    lookups of three queries apiece, two cooldown checks, and the writes. On the
    endpoint every phone hits every thirty seconds, with a few dozen people in
    range, that was the single most expensive thing the backend did. Nothing
    about *which* alerts get written changes here — only how many round trips it
    takes to decide.

    Two facts make the batching exact rather than approximate. Blocking,
    friendship and connection are all symmetric, so one relationship map read
    from `user`'s side settles both directions of every pair. And every pair has
    `user` on one side, so a single query finds every standing cooldown row.
    """
    from users.visibility import STRANGER, can_surface_in_proximity, relationship_map

    matches = [(other, distance) for other, distance in matches if other.pk != user.pk]
    if not matches:
        return

    other_ids = [other.pk for other, _ in matches]
    rels = relationship_map(user, other_ids)

    # Both directions are written, but each is gated on the *sender's* own
    # settings. Without the second check, someone who opted out of interest
    # discovery would still be announced to a stranger the moment that stranger
    # moved. `can_surface_in_proximity` reads only the sender and the tier when
    # a tier is handed to it, so no query is issued here.
    wanted = []
    for other, distance in matches:
        rel = rels.get(other.pk, STRANGER)
        if can_surface_in_proximity(user, other, rel):
            wanted.append((user, other, distance))
        if can_surface_in_proximity(other, user, rel):
            wanted.append((other, user, distance))
    if not wanted:
        return

    cutoff = timezone.now() - timedelta(minutes=NEARBY_COOLDOWN_MINUTES)
    already = set(
        Notification.objects.filter(
            notification_type=Notification.Type.NEARBY_USER,
            created_at__gte=cutoff,
        ).filter(
            Q(recipient=user, sender_id__in=other_ids)
            | Q(sender=user, recipient_id__in=other_ids)
        ).values_list('recipient_id', 'sender_id')
    )

    Notification.objects.bulk_create([
        Notification(
            recipient=recipient,
            sender=sender,
            notification_type=Notification.Type.NEARBY_USER,
            message=f'{sender.username} with shared interests is {distance} km away.',
        )
        for recipient, sender, distance in wanted
        if (recipient.pk, sender.pk) not in already
    ])


def notify_nearby_match(user, nearby_user, distance_km: float):
    """One pair's worth of `notify_nearby_matches`.

    Kept because a caller that genuinely has one pair should not have to build a
    list, and because it is the shape the tests describe the rule in.
    """
    notify_nearby_matches(user, [(nearby_user, distance_km)])


# ── Conversations ─────────────────────────────────────────────────────────────
# Messages push but write no Notification row. The inbox and the club's own
# chat are already the durable list of who said what; a row per message would
# make the alerts feed a worse copy of both and drown the story and meetup
# alerts that have nowhere else to appear.
#
# What a row *would* have given for free is a cooldown, since it can be asked
# "have these two had one of these lately". Without rows that question goes to
# the cache, which is the better place for it anyway: an answer that expires on
# its own and never grows the table.

#: A conversation is one piece of news, not one per line typed. Somebody typing
#: five short messages in a row is the normal way people write, and it has to
#: arrive as one buzz rather than five.
DM_COOLDOWN_SECONDS = 60

#: Longer for clubs, because a club is many people talking at once and each of
#: them would otherwise be a separate reason to buzz every other member.
CLUB_COOLDOWN_SECONDS = 5 * 60


def _cooldown_passed(key: str, seconds: int) -> bool:
    """True the first time this key is seen in `seconds`, False after.

    `cache.add` only sets when the key is absent and reports whether it did, so
    the check and the claim are one atomic operation — two web workers handling
    two messages in the same instant cannot both decide they are first.

    Fails *open*: if the cache is unreachable, `add` raises and the caller
    should still push. A missed notification is the worse failure here.
    """
    try:
        return bool(cache.add(key, 1, timeout=seconds))
    except Exception as exc:  # noqa: BLE001 — a dead cache must not mute the app
        print(f'[Push] cooldown check failed, sending anyway: {exc}')
        return True


def _preview(text: str, image=None, limit: int = 120) -> str:
    """What a message looks like on a lock screen."""
    body = (text or '').strip()
    if body:
        return body if len(body) <= limit else body[:limit].rstrip() + '…'
    return '📷 Photo' if image else 'Sent a message'


def notify_direct_message(sender, recipient, content='', *, image=None):
    """`sender` sent `recipient` a DM.

    Called from both paths a message can arrive on — the socket, which is what
    the app actually uses, and the REST endpoint it falls back to and sends
    photos through. It was wired to the REST path alone, which is why a typed
    message notified nobody: the consumer saved it and broadcast it to whoever
    already had the conversation open, and a phone in a pocket learned nothing.

    Silent when the recipient is the sender, has blocked them, or has already
    been told about this conversation in the last minute.
    """
    from users.visibility import blocked_ids_for

    if recipient.pk == sender.pk:
        return False
    if sender.pk in blocked_ids_for(recipient):
        return False
    if not _cooldown_passed(f'push:dm:{recipient.pk}:{sender.pk}', DM_COOLDOWN_SECONDS):
        return False

    send_push_notification(
        recipient,
        f'💬 {sender.username}',
        _preview(content, image),
        data={'type': 'message', 'user_id': sender.pk, 'username': sender.username},
        category='message',
    )
    return True


def notify_club_message(club, sender, content='', *, image=None):
    """Someone posted in `club`. Everyone else in it hears about it once.

    One Expo request for the whole club rather than one per member, and one
    cooldown per *recipient* per club — so a member being caught up on by six
    people gets one notification, and a member who muted club chat gets none.
    The sender is never a recipient of their own message.
    """
    from users.models import User

    # Only members who could actually receive one: no token, no message, and
    # the query need not walk the rest of the club to find that out.
    # `notify_club_messages` is re-checked inside push_to_users, but filtering
    # here also keeps cooldown keys from being claimed by people who are opted
    # out — otherwise turning the setting back on would be silent for a while.
    recipients = list(
        User.objects
        .filter(club_memberships__club=club, notify_club_messages=True)
        .exclude(pk=sender.pk)
        .exclude(push_token__isnull=True)
        .exclude(push_token='')
        # Someone who blocked the sender does not want their club chatter
        # either. Symmetric, like every other block check in the app.
        .exclude(blocked_by__blocker=sender)
        .exclude(blocking__blocked=sender)
        .only('pk', 'push_token', 'notify_club_messages')
    )
    fresh = [
        user for user in recipients
        if _cooldown_passed(f'push:club:{user.pk}:{club.pk}', CLUB_COOLDOWN_SECONDS)
    ]
    if not fresh:
        return 0

    return push_to_users(
        fresh,
        f'💬 {club.name}',
        f'{sender.username}: {_preview(content, image)}',
        data={'type': 'club_message', 'club_id': club.pk, 'club_name': club.name},
        category='club_message',
    )


# ── Meetups ───────────────────────────────────────────────────────────────────
# These do write rows. A meetup proposed on Tuesday for Saturday is exactly the
# thing someone needs to find again after the push has been swiped away, and
# unlike a message it appears nowhere else in the app.

def _club_alert(recipients, notification_type, message, *, sender, club,
                event=None, push_title, category, data):
    """Write one alert each, then push them all in a single Expo request."""
    sender_pk = sender.pk if sender else None
    recipients = [user for user in recipients if user.pk != sender_pk]
    if not recipients:
        return []

    created = Notification.objects.bulk_create([
        Notification(
            recipient=user,
            sender=sender,
            club=club,
            event=event,
            notification_type=notification_type,
            message=message,
        )
        for user in recipients
    ])
    try:
        push_to_users(recipients, push_title, message, data=data, category=category)
    except Exception as exc:  # noqa: BLE001 — push is never worth a 500
        print(f'[Push] club alert failed: {exc}')
    return created


def notify_club_event_created(event):
    """A meetup was proposed in a club. Every other member is told.

    Told at proposal rather than only at approval on purpose: a proposal nobody
    sees is one nobody turns up to, and the owner who has to approve it is the
    person most in need of knowing it exists.
    """
    from users.models import User

    club = event.club
    members = list(
        User.objects
        .filter(club_memberships__club=club)
        .exclude(pk=event.created_by_id)
        .exclude(blocked_by__blocker=event.created_by)
        .exclude(blocking__blocked=event.created_by)
        .only('pk', 'push_token', 'notify_club_events')
    )
    return _club_alert(
        members,
        Notification.Type.CLUB_EVENT,
        f'@{event.created_by.username} proposed "{event.title}" in {club.name}',
        sender=event.created_by,
        club=club,
        event=event,
        push_title=f'📅 New meetup in {club.name}',
        category='club_event',
        data={
            'type': 'club_event',
            'club_id': club.pk,
            'event_id': event.pk,
            'club_name': club.name,
        },
    )


def notify_club_event_decided(event, decided_by, approved: bool):
    """An owner approved or rejected a meetup.

    On approval the whole club hears — the event is real now and on the map,
    which is news to everyone who might go. On rejection only the person who
    proposed it does; a meetup that will not happen is not news to the club,
    and announcing every refusal to everybody would make proposing one costly.
    """
    from users.models import User

    club = event.club
    if approved:
        recipients = list(
            User.objects
            .filter(club_memberships__club=club)
            .exclude(pk=decided_by.pk)
            .only('pk', 'push_token', 'notify_club_events')
        )
        message = f'"{event.title}" is happening in {club.name}'
        push_title = f'✅ Meetup confirmed — {club.name}'
    else:
        # An owner rejecting their own proposal already knows the outcome.
        if event.created_by_id == decided_by.pk:
            return []
        recipients = [event.created_by]
        message = f'Your meetup "{event.title}" was not approved in {club.name}'
        push_title = f'Meetup declined — {club.name}'

    return _club_alert(
        recipients,
        Notification.Type.CLUB_EVENT_STATUS,
        message,
        sender=decided_by,
        club=club,
        event=event,
        push_title=push_title,
        category='club_event',
        data={
            'type': 'club_event',
            'club_id': club.pk,
            'event_id': event.pk,
            'club_name': club.name,
        },
    )


def notify_club_invite(invitation):
    """Ask the invitee to join. Push only, like a Hi: the invitation itself is the request they answer."""
    club = invitation.club
    return send_push_notification(
        invitation.receiver, '👥 Club invitation',
        f'@{invitation.sender.username} invited you to join {club.name}',
        data={'type': 'request', 'club_id': club.pk, 'club_name': club.name},
        category='social',
    )


# ─── Swifts ───────────────────────────────────────────────────────────────────
# Same three rules as the story alerts above, for the same reasons: nothing is
# news to you about your own Swift, a block hides both directions, and one
# person is one piece of news however many times they tap.
#
# The coalescing is per (recipient, sender, swift, type), which is why the three
# kinds are three types rather than one `swift_activity`. Folding them together
# would mean a like from someone who had already commented silently rewrote the
# comment alert, and the author would never learn about the comment.
def _swift_alert(sender, swift, notification_type, message, push_title):
    """Create — or refresh — one Swift alert.

    An unread alert from the same person, on the same Swift, of the same kind
    is rewritten in place rather than joined by another: the author sees the
    current state once, and is not pushed at again for a change of mind.

    `created_at` is `auto_now_add`, so it only moves through an explicit UPDATE
    — which is what keeps a refreshed alert at the top of the list where it
    belongs.
    """
    from users.visibility import blocked_ids_for

    recipient = swift.author
    if recipient.pk == sender.pk:
        return None
    if sender.pk in blocked_ids_for(recipient):
        return None

    standing = Notification.objects.filter(
        recipient=recipient, sender=sender, swift=swift,
        notification_type=notification_type, is_read=False,
    )
    if standing.update(message=message, created_at=timezone.now()):
        return None

    notification = Notification.objects.create(
        recipient=recipient, sender=sender, swift=swift,
        notification_type=notification_type, message=message,
    )
    try:
        send_push_notification(
            recipient, push_title, message,
            data={
                'type': 'swift',
                'swift_id': swift.pk,
                'user_id': sender.pk,
                'username': sender.username,
            },
            category='swift',
        )
    except Exception as exc:  # noqa: BLE001 — push is never worth a 500
        print(f'[Push] swift alert failed: {exc}')
    return notification


def notify_swift_like(swift, sender):
    return _swift_alert(
        sender=sender,
        swift=swift,
        notification_type=Notification.Type.SWIFT_LIKE,
        message=f'@{sender.username} liked your Swift',
        push_title='❤️ Swift like',
    )


def notify_swift_comment(swift, sender, text):
    """Someone commented. The excerpt is what makes the alert worth opening."""
    excerpt = (text or '').strip()
    if len(excerpt) > 80:
        excerpt = excerpt[:80].rstrip() + '…'
    message = f'@{sender.username} commented on your Swift'
    if excerpt:
        message = f'{message}: {excerpt}'
    return _swift_alert(
        sender=sender,
        swift=swift,
        notification_type=Notification.Type.SWIFT_COMMENT,
        message=message,
        push_title='💬 Swift comment',
    )


def notify_swift_repost(swift, sender):
    return _swift_alert(
        sender=sender,
        swift=swift,
        notification_type=Notification.Type.SWIFT_REPOST,
        message=f'@{sender.username} reposted your Swift',
        push_title='🔁 Swift repost',
    )


def notify_new_device(device):
    """This account was just signed in to on a device it had not been seen on.

    Goes to the account's *other* devices and to its alerts feed — never to the
    one that triggered it. Whoever is holding the new phone already knows they
    signed in; the whole value of this is reaching the phone in someone else's
    pocket, so `push_to_devices` is given an explicit list rather than letting
    `push_to_users` fan out across everything the account owns.

    Sole alert with a null `sender`: it is about the account rather than about
    something a person did, and there is no profile to open from it.

    Silent when this is the only device, which is the ordinary case — a first
    sign-in has nobody to warn, and telling someone about their own only phone
    is noise that teaches them to ignore the one that matters.
    """
    from users.models import Device

    others = list(
        Device.objects.filter(user=device.user).exclude(pk=device.pk)
    )
    if not others:
        return None

    message = f'Your account was signed in to on a new device ({device.label}).'
    notification = Notification.objects.create(
        recipient=device.user,
        sender=None,
        notification_type=Notification.Type.NEW_DEVICE,
        message=message,
    )
    try:
        push_to_devices(
            others,
            '🔐 New sign-in',
            message,
            data={'type': 'new_device', 'device_id': device.pk},
            category='security',
        )
    except Exception as exc:  # noqa: BLE001 — push is never worth a 500
        print(f'[Push] new-device alert failed: {exc}')
    return notification
