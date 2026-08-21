"""Sending push notifications to Expo, and deciding who is willing to get one.

Split out of `notifications.utils` because the two answer different questions.
`utils` decides *what is news* — cooldowns, blocks, whether an alert row is
written. This decides *how a message reaches a phone*: which people have a
token and a preference that allows it, how many HTTP calls that takes, and what
payload the app needs to open the right screen when the notification is tapped.

Two things here that the single-recipient original could not do:

  - **One request for a crowd.** A club message is news to every member at
    once. `send_push_notification` in a loop meant one thread and one HTTPS
    round trip per member; Expo accepts up to 100 messages in a single POST,
    so a 60-person club now costs one call instead of sixty.
  - **A destination.** Every push carries a `data` payload naming the screen it
    came from. Without it, tapping "@miras: hey" could only open the app on
    whatever screen it was last on — the notification named a conversation the
    user then had to go and find. See `services/notifications.ts` for the
    routing side.
"""
import threading

import requests
from django.conf import settings

EXPO_PUSH_URL = 'https://exp.host/--/api/v2/push/send'

#: Expo's documented ceiling for one request. Larger lists are chunked.
MAX_PER_REQUEST = 100

#: How long to wait on Expo before giving up. A push is never worth holding a
#: request cycle open for, and the send already runs off-thread.
TIMEOUT_SECONDS = 10


#: Which preference field decides whether a given category may push, and which
#: Android channel it lands in. A category with no entry here always sends —
#: that is the deliberate default for anything a user asked for directly.
#:
#: The channel matters on Android: it is what lets someone silence club chatter
#: in the system settings while keeping direct messages loud, without the app
#: having to offer a second set of switches for it.
CATEGORIES = {
    'message':       ('notify_messages', 'messages'),
    'club_message':  ('notify_club_messages', 'clubs'),
    'club_event':    ('notify_club_events', 'events'),
    'social':        ('notify_social', 'social'),
    'story':         ('notify_story_activity', 'social'),
    'swift':         ('notify_swift_activity', 'social'),
    'zone_music':    ('music_zone_alerts', 'default'),
}


def allows(user, category: str) -> bool:
    """Whether `user` has said yes to this kind of push.

    An unknown category is allowed rather than dropped: a new caller that
    forgets to register itself above should be noisy, not silently dead.
    Missing fields read as allowed for the same reason — a preference column
    that has not been migrated yet must not mute the whole app.
    """
    pref = CATEGORIES.get(category)
    if pref is None:
        return True
    return bool(getattr(user, pref[0], True))


def channel_for(category: str) -> str:
    pref = CATEGORIES.get(category)
    return pref[1] if pref else 'default'


def _post(messages):
    """POST to Expo in chunks, swallowing everything.

    Runs on a daemon thread. Nothing upstream can act on a failed push — the
    message it announces is already saved — so a dead Expo must cost a log line
    and nothing else.
    """
    for start in range(0, len(messages), MAX_PER_REQUEST):
        chunk = messages[start:start + MAX_PER_REQUEST]
        try:
            requests.post(
                EXPO_PUSH_URL,
                json=chunk,
                headers={
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                },
                timeout=TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            print(f'[Push] send failed for {len(chunk)} message(s): {exc}')


def _envelope(token, title, body, data, category):
    message = {
        'to': token,
        'title': title,
        'body': body,
        'sound': 'default',
        'channelId': channel_for(category),
    }
    if data:
        # Expo requires JSON-serialisable values and caps the payload at 4KB.
        # Everything sent here is a handful of ids and a name, so the cap is
        # not close — but the ids are stringified because the iOS side hands
        # them back as strings anyway and the app should not have to guess.
        message['data'] = {k: v for k, v in data.items() if v is not None}
    return message


def push_to_users(users, title, body, *, data=None, category='default'):
    """Push to everyone in `users` who has a token and wants this category.

    Accepts any iterable of users, including one. Returns the number of
    messages queued, which is what the tests assert on — the send itself is
    off-thread and has no return value worth waiting for.
    """
    seen = set()
    messages = []
    for user in users:
        token = getattr(user, 'push_token', None)
        # Dedupe on the token, not the user: two accounts signed in on one
        # device share a token, and a club message that reaches both would
        # arrive twice on the same phone.
        if not token or token in seen:
            continue
        if not allows(user, category):
            continue
        seen.add(token)
        messages.append(_envelope(token, title, body, data, category))

    if not messages:
        return 0

    # Tests must not fire real HTTPS at Expo, and a daemon thread outliving the
    # test that spawned it is how flakes get in. Under `manage.py test` the
    # send is skipped entirely; `push_to_users` still returns the count, so the
    # decision logic above stays fully covered.
    if getattr(settings, 'PUSH_DISABLED', False):
        return len(messages)

    threading.Thread(target=_post, args=(messages,), daemon=True).start()
    return len(messages)


def push_to_user(user, title, body, *, data=None, category='default'):
    """One recipient's worth of `push_to_users`."""
    return push_to_users([user], title, body, data=data, category=category)
