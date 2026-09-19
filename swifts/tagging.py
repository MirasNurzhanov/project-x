"""Hashtags and mentions: parsing them, resolving them, attaching them.

## Why the caption is parsed on the server

The client sends its own lists — it has to, because it runs the autocomplete
and knows which `@aisha` the creator actually picked from the menu. But the
caption is parsed here as well, and the two are merged.

That is not redundancy. A creator can type `#astana` straight into the caption
without ever opening the tag field, and every reader will see a tag there; if
only the client's list were stored, that tag would be highlighted in the text
and belong to nothing. Parsing server-side means what a caption *says* and what
the Swift *is tagged with* cannot drift apart, whatever the client does or
forgets to do.

The client's list still matters for the opposite case: a tag or a mention the
creator added deliberately without putting it in the caption.

## What resolution means for a mention

A hashtag is whatever was typed. A mention is a **person**, so `@someone` is
only stored if that username exists, is reachable, and has not blocked the
author. An unresolvable handle stays plain text in the caption and no row is
written — which is the honest outcome: a mention that notifies nobody and links
nowhere is not a mention.
"""
import re

from django.db import IntegrityError, transaction
from django.db.models import F, Value
from django.db.models.functions import Greatest

#: The most tags one Swift may carry. Past a handful a tag list stops being a
#: description and starts being reach-farming, and every one of them is a row
#: plus a counter update on publish.
MAX_HASHTAGS = 15
#: The most people one Swift may name. Mentions each cost a notification, which
#: is what makes an unbounded list an abuse vector rather than a long caption.
MAX_MENTIONS = 10

#: Longest a single tag may be, matching `Hashtag.name`.
MAX_HASHTAG_CHARS = 64

#: A hashtag in running text.
#:
#: `\w` under `re.UNICODE` (Python 3's default) covers Cyrillic and Kazakh
#: alongside ASCII, which this app needs — a tag written in the language the
#: caption is in must work. The leading `(?<!\w)` stops `ab#cd` from being read
#: as a tag: a '#' in the middle of a word is punctuation, not a tag opener.
#: A tag of digits alone is excluded by the inner character class — `#2024`
#: reads as a year to everybody who writes it.
HASHTAG_RE = re.compile(r'(?<!\w)#(\w*[^\W\d_]\w*)', re.UNICODE)

#: A mention in running text. ASCII-only and deliberately so: it has to match
#: what `users.User.username` actually permits, and matching more widely would
#: produce handles that can never resolve.
MENTION_RE = re.compile(r'(?<!\w)@([A-Za-z0-9_.]{1,150})')


def normalise_tag(raw):
    """A tag reduced to its identity, or '' if it is not one.

    Casefolded, '#' stripped, length-capped. See `Hashtag` for why casefold
    rather than lower.
    """
    tag = (raw or '').strip().lstrip('#').strip()
    if not tag:
        return ''
    tag = tag.casefold()[:MAX_HASHTAG_CHARS]
    # Re-check the shape after trimming: a value that arrived as '#!!!' or
    # '#2024' is not a tag however it was delivered, and the client's explicit
    # list does not go through `HASHTAG_RE` on its own.
    match = HASHTAG_RE.fullmatch('#' + tag)
    return match.group(1) if match else ''


def parse_hashtags(text):
    """Every distinct tag in a caption, in the order they appear.

    Order matters and is preserved: `SwiftHashtag.position` exists so that a
    creator's tags come back the way they wrote them.
    """
    seen, out = set(), []
    for match in HASHTAG_RE.finditer(text or ''):
        tag = normalise_tag(match.group(1))
        if tag and tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out


def parse_mentions(text):
    """Every distinct handle in a caption, lowercased, in order.

    Lowercased because usernames are compared case-insensitively below; the
    row stores what was actually typed.
    """
    seen, out = set(), []
    for match in MENTION_RE.finditer(text or ''):
        handle = match.group(1).strip('.').lower()
        if handle and handle not in seen:
            seen.add(handle)
            out.append(handle)
    return out


def _merged(from_caption, from_client, limit):
    """The caption's items first, then anything the client added, capped.

    Caption order leads because that is the order a reader meets them in.
    """
    out = list(from_caption)
    seen = set(out)
    for item in from_client:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out[:limit]


# ── Attaching ────────────────────────────────────────────────────────────────

def attach_hashtags(swift, extra=()):
    """Write this Swift's hashtag rows and bump each tag's counter.

    Returns the `Hashtag` rows attached, in order.

    `get_or_create` inside a savepoint per tag: two people publishing the same
    new tag in the same instant both find nothing and both insert, and the
    unique index turns the loser into an `IntegrityError` rather than a second
    row. Catching it and re-reading is the standard resolution, and the
    savepoint is what keeps that error from poisoning the surrounding
    transaction on PostgreSQL — where any failed statement aborts the whole
    transaction until it is rolled back to a savepoint.
    """
    from .models import Hashtag, SwiftHashtag

    tags = _merged(
        parse_hashtags(swift.caption),
        [normalise_tag(t) for t in (extra or ())],
        MAX_HASHTAGS,
    )
    if not tags:
        return []

    attached = []
    for position, name in enumerate(tags):
        try:
            with transaction.atomic():
                tag, _ = Hashtag.objects.get_or_create(
                    name=name, defaults={'display': _display_for(swift.caption, name)},
                )
        except IntegrityError:
            tag = Hashtag.objects.filter(name=name).first()
            if tag is None:
                continue
        if tag.is_blocked:
            # Silently skipped rather than refused. A blocked tag is a
            # moderation decision about the tag, not about this creator's post,
            # and failing their publish over it would both tell them the block
            # exists and lose them their video.
            continue
        try:
            with transaction.atomic():
                SwiftHashtag.objects.create(swift=swift, hashtag=tag, position=position)
        except IntegrityError:
            continue
        # F() rather than a read-modify-write, same as `swifts.counters` —
        # a trending tag is exactly where simultaneous publishes happen.
        Hashtag.objects.filter(pk=tag.pk).update(swift_count=F('swift_count') + 1)
        attached.append(tag)
    return attached


def _display_for(caption, name):
    """The tag as the caption wrote it, for a tag being created right now.

    Falls back to the normalised form when the tag came from the client's list
    rather than the text.
    """
    for match in HASHTAG_RE.finditer(caption or ''):
        written = match.group(1)
        if written.casefold()[:MAX_HASHTAG_CHARS] == name:
            return written[:MAX_HASHTAG_CHARS]
    return name


def attach_mentions(swift, extra=()):
    """Resolve and store the people this Swift names. Returns the rows written.

    Storing only — nobody is told here. Notifying is `notify_pending`, and the
    two are separate because a Swift is not necessarily watchable at the moment
    it is created: with AI moderation on it is PENDING, and an alert saying
    "you were mentioned" that opens a 404 is worse than one that arrives a
    minute later.

    Resolution rules, in order:

    * the username must exist (case-insensitively) and the account must not be
      deleted or retired;
    * never the author themselves;
    * never somebody who has blocked the author.

    A handle failing any of these is left as plain caption text and no row is
    written — which is the honest outcome: a mention that notifies nobody and
    links nowhere is not a mention.
    """
    from django.db.models import Q
    from users.models import User
    from users.visibility import blocked_ids_for
    from .models import SwiftMention

    handles = _merged(
        parse_mentions(swift.caption),
        [str(h or '').strip().lstrip('@').lower() for h in (extra or ())],
        MAX_MENTIONS,
    )
    if not handles:
        return []

    # `username__in` against a lowercased list would miss `Aisha`, so the match
    # is a bounded OR of `iexact` — bounded because `handles` is capped at
    # MAX_MENTIONS above.
    lookup = Q()
    for handle in handles:
        lookup |= Q(username__iexact=handle)
    found = {
        user.username.lower(): user
        for user in User.objects.filter(lookup)
        .exclude(pk=swift.author_id)
        .filter(deletion_requested_at__isnull=True, retired_account_id__isnull=True)
    }

    author_id = swift.author_id
    rows = []
    for position, handle in enumerate(handles):
        user = found.get(handle)
        if user is None:
            continue
        # The question is whether the person being named has shut the author
        # out — a mention is a way to reach somebody who said not to be.
        if author_id in blocked_ids_for(user):
            continue
        try:
            with transaction.atomic():
                mention = SwiftMention.objects.create(
                    swift=swift, user=user, username=user.username, position=position,
                )
        except IntegrityError:
            continue
        rows.append(mention)
    return rows


def notify_pending(swift):
    """Tell everybody named in this Swift who has not been told yet.

    Called once the Swift is actually watchable — straight after publishing
    when there is nothing to screen, and otherwise from
    `swifts.ai_moderation._approve` at the moment PENDING becomes OK. Never
    inside the publishing transaction: a push that times out must not roll back
    a Swift that is already stored.

    Idempotent by way of `SwiftMention.notified`, which is what makes the two
    call sites safe. A row is marked only after its alert is filed, so a
    failure leaves it pending for the next attempt and a success cannot be
    repeated.
    """
    from notifications.utils import notify_swift_mention
    from .models import SwiftMention

    pending = list(
        SwiftMention.objects.filter(swift=swift, notified=False).select_related('user')
    )
    notified = []
    for mention in pending:
        try:
            notify_swift_mention(swift, mention.user)
        except Exception:  # noqa: BLE001 — an alert is never worth a failed publish
            continue
        notified.append(mention.pk)
    if notified:
        SwiftMention.objects.filter(pk__in=notified).update(notified=True)
    return len(notified)


def apply(swift, hashtags=(), mentions=()):
    """Write everything tagging owns for one freshly published Swift.

    Rows only. `notify_pending` is the other half and is deliberately the
    caller's to schedule — see its note.
    """
    attach_hashtags(swift, hashtags)
    return attach_mentions(swift, mentions)


def retag(swift, hashtags=(), mentions=()):
    """Bring a published Swift's tags back in line with its edited caption.

    `SwiftUpdateSerializer` lets a creator change their caption after posting,
    and a caption is half of what a Swift is tagged with — so without this, an
    edit that adds `#astana` would show the tag in the text and attach it to
    nothing, and one that removes a tag would leave the Swift listed under it
    forever.

    Synced rather than appended, in both directions:

    * tags no longer wanted lose their row and give back their count;
    * tags newly wanted gain a row and take one;
    * people no longer named lose their mention row — but **not** their
      notification, which already happened and cannot be unsent. That is the
      one asymmetry here and it is deliberate: the row is a claim about what
      the Swift says now, and the alert was a fact about what it said then.

    New mentions are notified only if the Swift is watchable, by the same rule
    publishing follows — see `notify_pending`.
    """
    from .models import Hashtag, SwiftHashtag, SwiftMention

    # ── Hashtags ─────────────────────────────────────────────────────────────
    wanted = _merged(
        parse_hashtags(swift.caption),
        [normalise_tag(t) for t in (hashtags or ())],
        MAX_HASHTAGS,
    )
    existing = {
        link.hashtag.name: link
        for link in SwiftHashtag.objects.filter(swift=swift).select_related('hashtag')
    }

    stale = [link for name, link in existing.items() if name not in wanted]
    if stale:
        Hashtag.objects.filter(pk__in=[link.hashtag_id for link in stale]).update(
            swift_count=Greatest(F('swift_count') - 1, Value(0)),
        )
        SwiftHashtag.objects.filter(pk__in=[link.pk for link in stale]).delete()

    for position, name in enumerate(wanted):
        link = existing.get(name)
        if link is not None:
            # Already attached: only its place in the creator's order can have
            # moved, and that is one cheap UPDATE rather than a delete-and-add
            # that would churn the tag's counter for no reason.
            if link.position != position:
                SwiftHashtag.objects.filter(pk=link.pk).update(position=position)
            continue
        try:
            with transaction.atomic():
                tag, _ = Hashtag.objects.get_or_create(
                    name=name, defaults={'display': _display_for(swift.caption, name)},
                )
        except IntegrityError:
            tag = Hashtag.objects.filter(name=name).first()
            if tag is None:
                continue
        if tag.is_blocked:
            continue
        try:
            with transaction.atomic():
                SwiftHashtag.objects.create(swift=swift, hashtag=tag, position=position)
        except IntegrityError:
            continue
        Hashtag.objects.filter(pk=tag.pk).update(swift_count=F('swift_count') + 1)

    # ── Mentions ─────────────────────────────────────────────────────────────
    # Re-resolved from scratch: a handle that could not be resolved when the
    # Swift was posted — the account did not exist yet, or had blocked the
    # author — may resolve now, and the reverse is equally true.
    wanted_handles = set(_merged(
        parse_mentions(swift.caption),
        [str(h or '').strip().lstrip('@').lower() for h in (mentions or ())],
        MAX_MENTIONS,
    ))

    # Compared on the stored username rather than on a re-resolved user id: the
    # row records what the caption said, which is exactly what is being matched
    # against the caption's current handles.
    gone = [
        row.pk for row in SwiftMention.objects.filter(swift=swift)
        if row.username.lower() not in wanted_handles
    ]
    if gone:
        SwiftMention.objects.filter(pk__in=gone).delete()

    # Adds whatever is newly named; the unique constraint makes the ones that
    # are already there no-ops, so nobody is notified twice.
    attach_mentions(swift, mentions)


def release(swift):
    """Give back this Swift's tag counts when it stops being live.

    Called on delete and on a takedown. The `SwiftHashtag` rows stay — the tag
    was part of what was posted and the history is worth keeping — but a tag's
    `swift_count` is meant to describe what a reader would find under it, and a
    deleted Swift is not that.

    Not idempotent by itself, which is why callers must only invoke it on a
    transition: `swifts.views` checks the Swift was not already soft-deleted
    before calling, and calling it twice would undercount the tag.
    """
    from .models import Hashtag, SwiftHashtag

    tag_ids = list(
        SwiftHashtag.objects.filter(swift=swift).values_list('hashtag_id', flat=True)
    )
    if tag_ids:
        Hashtag.objects.filter(pk__in=tag_ids).update(
            swift_count=Greatest(F('swift_count') - 1, Value(0)),
        )
