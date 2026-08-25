"""The vocabulary every report in the project shares.

There were two ways to end up with a moderation queue nobody can triage. One is
a free-text box, which is unroutable. The other is two report models that each
invented their own category list, so "nudity" on a Swift and "nudity" on a story
sort into different buckets and neither count is the real one.

`swifts.SwiftReport` was here first and its choices are the ones already in the
database, so these are its values verbatim rather than a new list it now has to
migrate to. The two models import from here so they cannot drift apart again.
"""
from django.db import models


class Reason(models.TextChoices):
    SPAM = 'spam', 'Spam'
    NUDITY = 'nudity', 'Nudity or sexual content'
    VIOLENCE = 'violence', 'Violence'
    HATE = 'hate', 'Hate or harassment'
    SELF_HARM = 'self_harm', 'Self-harm'
    MINOR_SAFETY = 'minor_safety', 'Risk to a minor'
    #: Not a content category - "this shows where I live". The one reason that
    #: is about exposure rather than the material itself.
    PRIVACY = 'privacy', 'Reveals private information'
    OTHER = 'other', 'Something else'


#: The reasons that mean "this needs looking at today". Used to sort the queue
#: and, in `moderation.services`, to decide whether a report is severe enough to
#: quarantine content before a human reaches it.
URGENT_REASONS = frozenset({Reason.MINOR_SAFETY, Reason.SELF_HARM, Reason.NUDITY})


class State(models.TextChoices):
    OPEN = 'open', 'Open'
    #: Looked at, nothing wrong. This is the "dismiss" verdict - a name that
    #: says what the moderator concluded rather than what they clicked.
    REVIEWED = 'reviewed', 'Reviewed - no action'
    ACTIONED = 'actioned', 'Reviewed - action taken'


class Action(models.TextChoices):
    """What a moderator actually did, recorded separately from the verdict.

    `State.ACTIONED` says a report was upheld; this says what happened as a
    result, which is the part an appeal or an audit needs. Blank on an open or
    dismissed report.
    """

    NONE = '', 'Nothing yet'
    CONTENT_REMOVED = 'content_removed', 'Content removed'
    CONTENT_RESTORED = 'content_restored', 'Content restored (report rejected)'
    USER_WARNED = 'user_warned', 'User warned'
    USER_SUSPENDED = 'user_suspended', 'User suspended'
    USER_BANNED = 'user_banned', 'User banned'
    REFERRED = 'referred', 'Referred to law enforcement'


class Moderation(models.TextChoices):
    """The moderation state of one piece of content.

    Values are `swifts.SwiftVideo.Moderation`'s verbatim, so the column means
    the same thing on a story, a comment and a Swift and one admin filter can
    span all three. Kept here rather than imported from `swifts` because the
    dependency would run the wrong way: content apps depend on moderation, not
    on each other.
    """

    OK = 'ok', 'OK'
    #: Reported and awaiting review, and still visible. Flagging is not a
    #: verdict - hiding on the first report hands every user a mute button
    #: pointed at anybody they dislike, and coordinated flagging is a real
    #: tactic that works precisely on systems which treat a report as an
    #: outcome.
    FLAGGED = 'flagged', 'Flagged'
    #: Held back from publication by the automated filter, or by enough
    #: independent reports to stop waiting for a human. Invisible to everyone
    #: but the author, who is told, and to moderators.
    QUARANTINED = 'quarantined', 'Quarantined - awaiting review'
    #: Taken down. Invisible to everyone including the author, and
    #: indistinguishable from deleted to every reader.
    REMOVED = 'removed', 'Removed'


#: The states that keep content out of public view, and its complement. One
#: name for each rule, so no queryset has to re-derive it and get it subtly
#: different. Both are needed: a filter reads more clearly as `__in=VISIBLE`
#: when it is ORed with "or you wrote it", and as `~Q(__in=HIDDEN)` when it
#: stands alone.
HIDDEN_STATES = (Moderation.QUARANTINED, Moderation.REMOVED)
VISIBLE_STATES = (Moderation.OK, Moderation.FLAGGED)

#: How many *distinct* reporters it takes to quarantine content before a human
#: has looked at it. Three, because two is within reach of a pair of friends and
#: because the cost of being wrong is a delay rather than a deletion - the
#: author is told, and a moderator can restore it.
AUTO_QUARANTINE_REPORTS = 3
