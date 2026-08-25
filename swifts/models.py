"""Swifts — short-form vertical video.

`SwiftVideo`, not `Swift`. The user-facing object is a "Swift" and every string
the app shows says so, but `from swifts.models import Swift` sitting next to
Apple's language in a codebase that ships an iOS app is a collision worth six
characters to avoid.

Three things about this module are load-bearing and are argued where they live:

  * **Coordinates are stored here and are not serialized by anything the feed
    or the detail endpoint can reach.** See `swifts/visibility.py` for the two
    questions, and `swifts/serializers.py` for the field lists that enforce the
    answer. The only way a latitude leaves this server is
    `SwiftLocationSerializer`, reachable from one endpoint.

  * **Counters are denormalised columns**, so every path that changes one has to
    move it by exactly one without losing a concurrent write. That lives in
    `swifts/counters.py`, and it is the same argument as
    `stories.views._adjust_likes` — which exists because the read-modify-write
    form silently dropped simultaneous likes.

  * **Likes, reposts, views and reveals are each unique per (swift, user)** at
    the database level. Not "checked before insert" — constrained. A check is a
    race; a constraint is not.
"""
from django.conf import settings
from django.core.validators import MaxValueValidator
from django.db import models

from moderation.reasons import (
    HIDDEN_STATES,
    Action as SharedAction,
    Moderation as SharedModeration,
)


class SwiftVideo(models.Model):
    """One vertical short video."""

    class Status(models.TextChoices):
        """Where this row is in the upload/transcode lifecycle.

        Deliberately separate from `visibility` and `moderation` below. They
        answer three different questions — is the file ready, who is it for,
        and has it been taken down — and collapsing any two of them means a
        transcode failure and a moderator removal become indistinguishable.
        """
        UPLOADING = 'uploading', 'Uploading'
        PROCESSING = 'processing', 'Processing'
        READY = 'ready', 'Ready'
        FAILED = 'failed', 'Failed'

    class Visibility(models.TextChoices):
        """Who the creator meant this for.

        Per-Swift rather than an account-wide setting, because "this one is for
        my friends" is a judgement about one video. Capped further by the
        author's account privacy — see `swifts.visibility.watchable_q`, where a
        PUBLIC Swift by a private account still only reaches approved viewers.
        """
        PUBLIC = 'public', 'Everyone'
        FRIENDS = 'friends', 'Friends'
        PRIVATE = 'private', 'Only me'

    #: Aliased to the project-wide vocabulary rather than redeclared, so
    #: `SwiftVideo.Moderation.REMOVED` and `Story.moderation` REMOVED are the
    #: same string and one admin filter spans both. The values here are the
    #: ones this column already held; `QUARANTINED` is the addition, and no
    #: existing row is in it.
    Moderation = SharedModeration

    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='swifts',
    )
    caption = models.CharField(max_length=300, blank=True)

    # ── Media ─────────────────────────────────────────────────────────────────
    # `video`/`thumbnail` are the live fields: the phone posts the raw file
    # (and, since export-time compositing has nowhere to run — see the R2
    # migration note on `swifts.media` — a client-captured poster frame)
    # straight to Django, which stores both on R2 via the project's default
    # STORAGES backend. Same shape as Story.image / ClubMessage.image.
    #
    # `video_url`/`thumbnail_url`/`media_id` are legacy: this app's earlier
    # design uploaded straight to Cloudinary and had Cloudinary composite trim,
    # volume, a music bed and a text overlay into the delivered file from these
    # URLs/id. No client writes them any more — see `swifts.media` — but they
    # stay so any row created under that design keeps resolving.
    video = models.FileField(upload_to='swifts/', null=True, blank=True)
    thumbnail = models.ImageField(upload_to='swifts/posters/', null=True, blank=True)
    video_url = models.URLField(max_length=500, blank=True)
    thumbnail_url = models.URLField(max_length=500, blank=True)
    media_id = models.CharField(max_length=200, blank=True, db_index=True)

    duration_ms = models.PositiveIntegerField(default=0)
    #: Pixel dimensions as uploaded. Stored rather than derived so the client
    #: can reserve the right box before the first frame arrives; `aspect_ratio`
    #: below is the thing screens actually want.
    width = models.PositiveSmallIntegerField(default=0)
    height = models.PositiveSmallIntegerField(default=0)

    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.READY, db_index=True,
    )

    # ── Audio ─────────────────────────────────────────────────────────────────
    # Flat fields rather than a Track table, deliberately, and only for now: the
    # foundation needs to *carry* what a Swift is playing, and a table earns its
    # place when there is a sound page to put behind it. Shaped to match the
    # zone's Apple Music columns on `users.User` so the same picker payload
    # fits both without translation.
    audio_title = models.CharField(max_length=200, blank=True)
    audio_artist = models.CharField(max_length=200, blank=True)
    #: Apple Music catalogue id, when the track came from there. Resolved
    #: server-side through `users.applemusic` — never a URL the phone made up.
    audio_apple_id = models.CharField(max_length=64, blank=True)
    audio_artwork_url = models.URLField(max_length=500, blank=True)
    audio_preview_url = models.URLField(max_length=500, blank=True)
    #: True when the Swift plays its own recorded sound. Mutually exclusive
    #: with an attached track in the UI, but not constrained here — a Swift
    #: with neither is silent, which is a real thing to post.
    uses_original_audio = models.BooleanField(default=True)
    #: Which catalogue entry this Swift is playing, when it is playing one.
    #:
    #: SET_NULL, and the flat fields above are *not* redundant with it: they are
    #: the attribution as it stood when the Swift was published. A track pulled
    #: from the catalogue — licence expired, rights withdrawn — must not silently
    #: strip the credit from every Swift that used it, and must not rewrite the
    #: credit either if the row is later edited. The FK says "which row"; the
    #: strings say "what this Swift claims", and only the first can go stale.
    #:
    #: Not attachable to a new Swift any more: mixing a track into the video
    #: was Cloudinary's job (see `swifts.media`), and there is nowhere for that
    #: mix to happen against R2. Left on the model, unused by
    #: `SwiftCreateSerializer`, rather than dropped — a Swift created before
    #: this change may still carry one, and `get_track`'s attribution-survives
    #: -withdrawal behaviour still depends on the column existing.
    track = models.ForeignKey(
        'swifts.SwiftTrack', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='swifts',
    )

    # ── The edit ──────────────────────────────────────────────────────────────
    # What the creator chose in the editor, stored as *parameters* rather than
    # applied to the file — a holdover from when the composite was a Cloudinary
    # delivery transformation built from these columns, rendered on first
    # request. `swifts.media.playback_url`/`poster_url` no longer build one:
    # trim and volume are applied by the player at playback time (the editor
    # already previewed them this way), and the text overlay is drawn as a
    # client-side layer over the video rather than baked into the file. Kept as
    # parameters rather than migrated to real edits because nothing on the
    # phone re-encodes video either — see that module.
    #: Milliseconds into the source. `trim_end_ms` null means "to the end".
    trim_start_ms = models.PositiveIntegerField(default=0)
    trim_end_ms = models.PositiveIntegerField(null=True, blank=True)
    #: Which frame becomes the poster, in ms from the *start of the source*.
    #: Null means the first frame of the trimmed range. Superseded by
    #: `thumbnail` for anything uploaded after the R2 migration — the phone
    #: captures the frame itself and posts it as a real image rather than
    #: asking the server to extract one from a timestamp.
    cover_ms = models.PositiveIntegerField(null=True, blank=True)
    #: 0–100. The camera's own sound; playback applies it as the player volume.
    original_volume = models.PositiveSmallIntegerField(
        default=100, validators=[MaxValueValidator(100)],
    )
    #: Unused since a music bed can no longer be mixed in (see `track` above).
    #: Left in place for the same reason.
    music_volume = models.PositiveSmallIntegerField(
        default=70, validators=[MaxValueValidator(100)],
    )
    #: A single line drawn over the video at playback. Kept deliberately small
    #: — one string and a vertical position, not a layer system. A general
    #: overlay editor is a different feature and would need its own model.
    overlay_text = models.CharField(max_length=80, blank=True)

    class OverlayPosition(models.TextChoices):
        TOP = 'top', 'Top'
        MIDDLE = 'middle', 'Middle'
        BOTTOM = 'bottom', 'Bottom'

    overlay_position = models.CharField(
        max_length=6, choices=OverlayPosition.choices, default=OverlayPosition.MIDDLE,
    )

    # ── Location ──────────────────────────────────────────────────────────────
    # Null together, and null is the ordinary case. Everything about how these
    # get *written* is in `swifts.views._position_for`, which runs the
    # creator's location-sharing mode over whatever the client proposed — a
    # paused creator stores nothing at all, and an approximate one is snapped
    # before storage rather than on the way out.
    #
    # Nothing that can reach the feed or the detail endpoint serializes these.
    # `location_shared` is the creator's answer to "may someone ask where this
    # was", and it is a separate question from whether a coordinate exists:
    # turning sharing off leaves the row's columns exactly where they are and
    # closes the door, which is what makes it reversible.
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    #: "Kok-Tobe Park". A location under another name, so it answers to exactly
    #: the same gate the coordinate does and is never in a feed payload.
    place_name = models.CharField(max_length=200, blank=True)
    location_shared = models.BooleanField(default=False, db_index=True)

    # ── Audience and moderation ───────────────────────────────────────────────
    visibility = models.CharField(
        max_length=8, choices=Visibility.choices, default=Visibility.PUBLIC,
        db_index=True,
    )
    moderation = models.CharField(
        # 11, not 8: `QUARANTINED` is the longest value in the shared vocabulary.
        max_length=11, choices=Moderation.choices, default=Moderation.OK, db_index=True,
    )
    allow_comments = models.BooleanField(default=True)
    allow_reposts = models.BooleanField(default=True)

    #: Soft delete. Set rather than `.delete()` so that likes, comments and the
    #: reveal audit survive the row going away — the audit in particular is
    #: worth more after a takedown than before one, and a cascade would erase
    #: exactly the evidence anybody would want.
    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)

    # ── Counters ──────────────────────────────────────────────────────────────
    # Denormalised, and moved only through `swifts.counters.adjust`. A COUNT(*)
    # per Swift per feed page is the N+1 this app has been bitten by repeatedly;
    # a column is one read. The cost is that every writer owes the column an
    # exact ±1, which is why there is one function that does it.
    like_count = models.PositiveIntegerField(default=0)
    comment_count = models.PositiveIntegerField(default=0)
    repost_count = models.PositiveIntegerField(default=0)
    #: Distinct viewers, not impressions. `SwiftView` is unique per viewer, so
    #: this only moves the first time somebody watches — see SwiftView.
    view_count = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            # The feed: live Swifts, newest first. `deleted_at` leads because
            # it is the cheapest elimination and every read applies it.
            models.Index(
                fields=['deleted_at', 'status', '-created_at'],
                name='swift_feed_idx',
            ),
            # A creator's own grid, and the author-diversity pass a ranked feed
            # will later need.
            models.Index(fields=['author', '-created_at'], name='swift_author_idx'),
            # Audience filtering, which every feed query adds to the above.
            models.Index(fields=['visibility', 'status'], name='swift_audience_idx'),
            # Sparse by design: only a minority of Swifts carry a location, and
            # this is what makes "which of these could be revealed" cheap.
            models.Index(fields=['location_shared'], name='swift_located_idx'),
        ]

    def __str__(self):
        return f'Swift {self.pk} by {self.author_id}'

    # ── Derived state ─────────────────────────────────────────────────────────
    @property
    def is_live(self):
        """Whether this row is a Swift anybody could watch right now.

        The lifecycle half only — says nothing about *who*. Audience, blocking
        and account privacy are `swifts.visibility`, and both halves have to
        hold. Kept apart because a Swift can stop being live for reasons that
        have nothing to do with the viewer, and folding the two together is how
        a takedown ends up depending on who is asking.
        """
        return (
            self.deleted_at is None
            and self.status == self.Status.READY
            and self.moderation not in HIDDEN_STATES
        )

    @property
    def has_location(self):
        """Whether a coordinate is stored at all.

        Necessary but not sufficient for anyone to see it — a Swift whose
        creator has since turned sharing off still has both columns populated,
        and that is the point of `location_shared` being a separate field.
        """
        return self.latitude is not None and self.longitude is not None

    @property
    def location_available(self):
        """Whether this Swift's location could be revealed to *someone*.

        The creator's half of the answer: a coordinate exists and they have
        said it may be asked for. The viewer's half — may this particular
        person ask — is `swifts.visibility.can_reveal_location`, and the
        boolean the feed publishes goes through that one, not this one. See the
        note there for why the difference matters.
        """
        return self.has_location and self.location_shared

    @property
    def aspect_ratio(self):
        """width / height, or None when the dimensions were not recorded.

        None rather than a guess: a client that gets 9/16 for a video that is
        actually square draws a letterboxed box and then jumps, which is worse
        than being told nothing and waiting for the first frame.
        """
        if not self.width or not self.height:
            return None
        return round(self.width / self.height, 4)


class SwiftLike(models.Model):
    """One person's like of one Swift.

    The unique constraint is the whole design. Liking is a toggle on a screen
    where a double tap is also a like, so the same request arriving twice is
    ordinary rather than exceptional — and a `filter().exists()` check before
    an insert is a race that loses under exactly the conditions that make it
    matter. `SwiftVideo.like_count` is only moved when a row is genuinely
    created, so a duplicate cannot inflate it.
    """

    swift = models.ForeignKey(SwiftVideo, on_delete=models.CASCADE, related_name='likes')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='swift_likes',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['swift', 'user'], name='unique_swift_like'),
        ]
        indexes = [
            # "has this viewer liked these" for a page of the feed, in one query.
            models.Index(fields=['user', 'swift'], name='swift_like_viewer_idx'),
        ]

    def __str__(self):
        return f'{self.user_id} likes swift {self.swift_id}'


class SwiftRepost(models.Model):
    """`user` reposted `swift`.

    A join row, not a second SwiftVideo. A quote-repost that carries its own
    caption is a different product decision and would put two kinds of row in
    every feed query, notification and moderation path; this keeps reposting a
    signal and a boost. Same unique constraint and same reasoning as SwiftLike.
    """

    swift = models.ForeignKey(SwiftVideo, on_delete=models.CASCADE, related_name='reposts')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='swift_reposts',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(fields=['swift', 'user'], name='unique_swift_repost'),
        ]
        indexes = [
            models.Index(fields=['user', '-created_at'], name='swift_repost_user_idx'),
        ]

    def __str__(self):
        return f'{self.user_id} reposted swift {self.swift_id}'


class SwiftComment(models.Model):
    """A comment, or a reply to one.

    One level of replies. Two is where threading stops paying for itself on a
    phone, and `parent` pointing at another reply is refused in the serializer
    rather than modelled away, so the shape stays flat enough to render in a
    single query.
    """

    swift = models.ForeignKey(SwiftVideo, on_delete=models.CASCADE, related_name='comments')
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='swift_comments',
    )
    parent = models.ForeignKey(
        'self', on_delete=models.CASCADE, null=True, blank=True, related_name='replies',
    )
    text = models.TextField(max_length=500)
    #: Screening state, same vocabulary as `SwiftVideo.moderation` but drawn
    #: from `moderation.reasons` so a story, a comment and a Swift all mean the
    #: same thing by "quarantined". Written only by `moderation.services`.
    moderation = models.CharField(
        max_length=11, choices=SharedModeration.choices, default=SharedModeration.OK,
        db_index=True,
    )
    #: Soft delete, matching SwiftVideo: a deleted comment with live replies
    #: hanging off it still has to hold their place in the thread.
    deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            # A whole thread in one query, in render order.
            models.Index(
                fields=['swift', 'parent', '-created_at'], name='swift_comment_thread_idx',
            ),
        ]

    def __str__(self):
        return f'comment {self.pk} on swift {self.swift_id}'


class SwiftView(models.Model):
    """That `viewer` has watched `swift` — once, however many times they watch.

    This is the row the location reveal is gated on, so what it means has to be
    exact: **it records that a person encountered this Swift, not how many
    times a component mounted.**

    Hence one row per (swift, viewer), forever, enforced by the constraint
    rather than by the client behaving. A feed re-render, a scroll back up, a
    remount after a backgrounding — all of them are the same person having seen
    the same Swift, and all of them find the existing row. What repeat calls
    move is `view_count` and `watch_ms` on this row; what they never do is
    insert another one or touch `SwiftVideo.view_count`, which counts distinct
    viewers and moves exactly once per person.

    `first_viewed_at` is the value the reveal gate reads and it never changes.
    """

    swift = models.ForeignKey(SwiftVideo, on_delete=models.CASCADE, related_name='views')
    viewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='swift_views',
    )
    #: auto_now_add — set once, on the row that is created the first time.
    first_viewed_at = models.DateTimeField(auto_now_add=True)
    last_viewed_at = models.DateTimeField(auto_now=True)
    #: How many times this person has come back to this Swift. A signal worth
    #: keeping (a rewatch is the strongest implicit interest a video feed has)
    #: and the reason repeat POSTs are not simply discarded.
    view_count = models.PositiveIntegerField(default=1)
    #: The longest single watch, in milliseconds. Highest-water-mark rather
    #: than a sum, so a report cannot be inflated by replaying it.
    watch_ms = models.PositiveIntegerField(default=0)
    completed = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['swift', 'viewer'], name='unique_swift_view'),
        ]
        indexes = [
            # The reveal gate's lookup, and the "already seen" set a ranked
            # feed will want later.
            models.Index(fields=['viewer', 'swift'], name='swift_view_pair_idx'),
            models.Index(fields=['swift', '-first_viewed_at'], name='swift_view_recent_idx'),
        ]

    def __str__(self):
        return f'{self.viewer_id} viewed swift {self.swift_id}'


class SwiftLocationReveal(models.Model):
    """That `viewer` asked where `swift` was made, and was told.

    Three jobs, and the third is why it is a table rather than a counter:

      * it makes the reveal endpoint idempotent — the row is the record that
        this person already has this location, so asking again is a lookup
        rather than a new grant,
      * it is the creator's "who looked at where this was" list, which is the
        thing that makes attaching a location a decision somebody can review
        rather than a switch they flip and forget,
      * and it is the only way coordinate scraping is visible after the fact.
        A reveal that left no trace would be indistinguishable from one that
        never happened.

    `revealed_at` records the *first* reveal and never moves, so the audit
    answers "since when does this person know" rather than "when did they last
    ask". Repeats move `reveal_count` only, and are deliberately absent from
    the response — see `swifts.views.RevealSwiftLocationView`, where the
    identical-response property is the point.
    """

    swift = models.ForeignKey(
        SwiftVideo, on_delete=models.CASCADE, related_name='location_reveals',
    )
    viewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='swift_location_reveals',
    )
    revealed_at = models.DateTimeField(auto_now_add=True)
    reveal_count = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ['-revealed_at']
        constraints = [
            models.UniqueConstraint(
                fields=['swift', 'viewer'], name='unique_swift_location_reveal',
            ),
        ]
        indexes = [
            # The creator's audit list.
            models.Index(fields=['swift', '-revealed_at'], name='swift_reveal_swift_idx'),
            # One viewer's reveal history — the shape a rate limit reads.
            models.Index(fields=['viewer', '-revealed_at'], name='swift_reveal_viewer_idx'),
        ]

    def __str__(self):
        return f'{self.viewer_id} revealed location of swift {self.swift_id}'


class SwiftReport(models.Model):
    """Somebody reported a Swift.

    Reporting **flags**, it does not remove. `SwiftVideo.Moderation.FLAGGED` is
    deliberately still visible — a report is an accusation, not a verdict, and
    hiding content on the first one hands every user a mute button pointed at
    anybody they dislike. Coordinated flagging is a real tactic and it works
    precisely on systems that treat a report as an outcome.

    What the flag does is put the row in front of a moderator, which is what
    `swifts/admin.py` filters on. Taking it down is a human setting
    `Moderation.REMOVED`, and only that.

    Unique per (swift, reporter): one person is one report, however many times
    they press it. Reporting twice is not more true than reporting once, and
    letting it count twice is the whole mechanism a brigade needs.
    """

    class Reason(models.TextChoices):
        SPAM = 'spam', 'Spam'
        NUDITY = 'nudity', 'Nudity or sexual content'
        VIOLENCE = 'violence', 'Violence'
        HATE = 'hate', 'Hate or harassment'
        SELF_HARM = 'self_harm', 'Self-harm'
        MINOR_SAFETY = 'minor_safety', 'Risk to a minor'
        #: Not a content category — "this Swift shows where I live". The one
        #: reason that is about the location rather than the video, and the
        #: reason this list is not simply borrowed from a generic template.
        PRIVACY = 'privacy', 'Reveals private information'
        OTHER = 'other', 'Something else'

    class State(models.TextChoices):
        OPEN = 'open', 'Open'
        REVIEWED = 'reviewed', 'Reviewed — no action'
        ACTIONED = 'actioned', 'Reviewed — content removed'

    swift = models.ForeignKey(SwiftVideo, on_delete=models.CASCADE, related_name='reports')
    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='swift_reports',
    )
    reason = models.CharField(max_length=14, choices=Reason.choices)
    #: Optional free text. Capped short: a moderator reads these in a list, and
    #: the reason code is what actually routes the decision.
    note = models.CharField(max_length=500, blank=True)
    state = models.CharField(
        max_length=8, choices=State.choices, default=State.OPEN, db_index=True,
    )
    #: Who closed it, when, and what they did. Same three columns as
    #: `moderation.ContentReport`, so the unified queue in
    #: `moderation/admin.py` can show one table for both.
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='swift_reports_reviewed',
    )
    action_taken = models.CharField(
        max_length=17, choices=SharedAction.choices, blank=True, default='',
    )
    moderator_note = models.CharField(max_length=500, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['swift', 'reporter'], name='unique_swift_report',
            ),
        ]
        indexes = [
            # The moderation queue: open reports, oldest first, so the thing
            # that has been waiting longest is the thing that gets looked at.
            models.Index(fields=['state', 'created_at'], name='swift_report_queue_idx'),
            models.Index(fields=['swift', '-created_at'], name='swift_report_swift_idx'),
        ]

    def __str__(self):
        return f'report on swift {self.swift_id} by {self.reporter_id}'


class SwiftTrack(models.Model):
    """A song a Swift may play, from Aura's own catalogue.

    **Aura-owned or licensed only.** Nothing in this table is scraped, and the
    schema is deliberately shaped so that it cannot quietly become a scraper's
    landing zone: there is no source URL a client can write, `audio_url` is
    only ever set by an operator through the admin or a seeding command, and
    `licence` is a required statement of where the right to play it comes from.

    That last field is not paperwork. A short-video product plays other
    people's music to strangers, which is the single most reliably litigated
    thing an app of this kind does. A row with no licence is a row nobody can
    say is safe, so `is_active` defaults to False and publishing a track is a
    deliberate act.

    Why this exists at all rather than reusing `users.applemusic`: an Apple
    catalogue preview may be *previewed* under Apple's terms, and rendering one
    into a user's video file and redistributing it is a different act that
    those terms do not cover. The zone can point at Apple because it streams
    the preview to a listener; a Swift bakes audio into a file. So Swifts get
    their own catalogue and the Apple integration stays where it is.
    """

    class Source(models.TextChoices):
        #: Commissioned or bought outright. Aura owns it.
        OWNED = 'owned', 'Aura owned'
        #: Licensed from a rights holder or a production-music library.
        LICENSED = 'licensed', 'Licensed'
        #: Creative Commons or public domain, with the attribution the licence
        #: requires recorded in `attribution`.
        OPEN = 'open', 'Open licence'

    title = models.CharField(max_length=200)
    artist = models.CharField(max_length=200, blank=True)
    #: Free-text grouping — "Lo-fi", "Cinematic", "Upbeat". A CharField rather
    #: than a table because a genre list is editorial and changes with the
    #: catalogue, and a foreign key to six rows buys nothing.
    genre = models.CharField(max_length=60, blank=True, db_index=True)

    #: The audio itself, on the CDN. Set by an operator; there is no client
    #: path that writes this, which is the point.
    audio_url = models.URLField(max_length=500)
    #: The same asset's Cloudinary public id. Separate from `audio_url` because
    #: they are used for different things and only one of them can be derived:
    #: the URL is what the picker streams for a preview, and the id is what
    #: `swifts.media` names in the `l_video:` layer that mixes the track into
    #: the finished video. A track with no id can be auditioned and not used.
    cloudinary_id = models.CharField(max_length=200, blank=True)
    artwork_url = models.URLField(max_length=500, blank=True)
    duration_ms = models.PositiveIntegerField(default=0)

    source = models.CharField(max_length=8, choices=Source.choices)
    #: Where the right to play this comes from, in words a person can check —
    #: a contract reference, a library name and licence id, or the CC licence.
    licence = models.CharField(max_length=200)
    #: The credit line the licence requires, if it requires one. Shown wherever
    #: the track is named.
    attribution = models.CharField(max_length=200, blank=True)

    # ── Where it came from ────────────────────────────────────────────────────
    # Blank for a row typed into the admin. Set to a provider key — see
    # `swifts/music/registry.py` — for anything imported, together with that
    # provider's own id for the track.
    #
    # The pair is what makes a re-sync idempotent: `swifts.music.sync` matches
    # on it rather than on the title, which would collide the moment two
    # libraries both have a track called "Sunrise".
    provider = models.CharField(max_length=20, blank=True, db_index=True)
    provider_track_id = models.CharField(max_length=120, blank=True)
    synced_at = models.DateTimeField(null=True, blank=True)

    #: Off by default. A track becomes available to creators only when somebody
    #: has looked at the licence and said so.
    is_active = models.BooleanField(default=False, db_index=True)
    #: Editorial ordering for the picker's default list; higher first.
    priority = models.SmallIntegerField(default=0)
    #: How many Swifts have used it. Denormalised through `swifts.counters`.
    use_count = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-priority', 'title']
        constraints = [
            # One row per provider track. Without it a re-run of a sync that
            # was interrupted mid-page would double the catalogue.
            models.UniqueConstraint(
                fields=['provider', 'provider_track_id'],
                condition=models.Q(provider__gt=''),
                name='unique_provider_track',
            ),
        ]
        indexes = [
            # The picker's default list: active tracks, editorial order.
            models.Index(fields=['is_active', '-priority'], name='swift_track_active_idx'),
            models.Index(fields=['is_active', 'genre'], name='swift_track_genre_idx'),
            # The sync's lookup, and the admin's "everything from this provider".
            models.Index(fields=['provider', 'provider_track_id'],
                         name='swift_track_provider_idx'),
        ]

    def __str__(self):
        return f'{self.title} — {self.artist}' if self.artist else self.title
