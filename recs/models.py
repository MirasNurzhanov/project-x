"""What the recommender remembers.

Three tables, and the split between them is the whole design:

`SwiftSignal`      append-only, one row per thing a viewer did. The raw record.
                   Never read in the request path.
`CreatorAffinity`  a rolling summary of those signals per (viewer, creator),
                   maintained on write. This *is* the request path.
`SwiftImpression`  what has already been served to whom, so the feed does not
                   repeat itself.

The reason for the middle table is X's, arrived at the same way: user-signal-
service exists because scanning a person's raw interaction history at request
time does not survive contact with a real user. A viewer with 5,000 signals
would cost a 5,000-row scan per feed request per source. `CreatorAffinity` is
one row per creator they have met, updated in the same transaction as the
signal, and the feed reads a few dozen of them.

`SwiftSignal` is kept anyway, and kept complete, because it is the training
set. The day this is replaced by a model, the model is trained on exactly
these rows — which is why they carry `served_by` and `position`, the two things
you cannot reconstruct afterwards and cannot train an unbiased ranker without.

## What is deliberately not here

**No viewer coordinates.** Not a latitude, not a longitude, not a geohash, not
a region id. `LOCATION_REVEAL` records that a reveal happened and nothing about
where either party was. This is a hard rule, not an oversight, and
`tests/test_privacy.py` asserts the tables have no such column: ranking must
never be a reason to hold location data, and a recommender that reads
coordinates is a recommender that leaks them the first time someone logs a
feature vector.
"""
from django.conf import settings
from django.db import models
from django.utils import timezone


class SwiftSignal(models.Model):
    """One thing a viewer did to one Swift.

    Append-only. Nothing updates a signal row; a viewer who likes, unlikes and
    likes again produces three rows, because "changed their mind twice" is a
    different fact from "likes it" and only the row sequence carries it.
    """

    class Kind(models.TextChoices):
        # ── Explicit positive ────────────────────────────────────────────────
        LIKE = 'like', 'Liked'
        COMMENT = 'comment', 'Commented'
        REPOST = 'repost', 'Reposted'
        PROFILE_OPEN = 'profile_open', 'Opened creator profile'
        LOCATION_REVEAL = 'location_reveal', 'Revealed location'
        #: Reserved. Approx has no share-to-elsewhere action today — repost is
        #: the in-app equivalent — so nothing emits this yet. Declared so that
        #: adding sharing later is a client change and not a migration.
        SHARE = 'share', 'Shared'

        # ── Implicit positive ────────────────────────────────────────────────
        WATCH = 'watch', 'Watched'
        """Carries `watch_ms` and `completion_pct`. Emitted once per viewing,
        on the way out of the card, not on a timer."""
        COMPLETE = 'complete', 'Watched to the end'
        REPLAY = 'replay', 'Replayed'
        PAUSE = 'pause', 'Paused'
        """Weak but real: pausing a short video is usually reading something in
        it. Weighted low because it is also how you answer a phone call."""

        # ── Negative ─────────────────────────────────────────────────────────
        SKIP = 'skip', 'Skipped immediately'
        """Scrolled past inside the skip threshold. The most common signal in
        the table by an order of magnitude, and the most useful."""
        LOW_WATCH = 'low_watch', 'Watched very little'
        NOT_INTERESTED = 'not_interested', 'Not interested'
        REPORT = 'report', 'Reported'
        BLOCK_CREATOR = 'block_creator', 'Blocked the creator'

    #: Signals that move affinity up, and by how much per occurrence. Kept as
    #: data rather than as `if` branches so that `CreatorAffinity` and the
    #: scorer cannot disagree about what a signal is worth, and so a new kind
    #: is one line.
    POLARITY = {
        Kind.LIKE: 3.0,
        Kind.COMMENT: 4.0,
        Kind.REPOST: 5.0,
        Kind.PROFILE_OPEN: 2.5,
        Kind.LOCATION_REVEAL: 2.0,
        Kind.SHARE: 5.0,
        Kind.WATCH: 0.0,      # scored from completion_pct instead, below
        Kind.COMPLETE: 2.0,
        Kind.REPLAY: 3.0,
        Kind.PAUSE: 0.5,
        Kind.SKIP: -2.0,
        Kind.LOW_WATCH: -1.0,
        Kind.NOT_INTERESTED: -8.0,
        Kind.REPORT: -15.0,
        Kind.BLOCK_CREATOR: -50.0,
    }

    #: Below this fraction watched, a WATCH is really a skip. The client sends
    #: what happened; the server decides what it meant.
    SKIP_COMPLETION_PCT = 15
    LOW_WATCH_COMPLETION_PCT = 40

    viewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='swift_signals',
    )
    #: Null for creator-level signals — blocking someone is about the person,
    #: not about whichever Swift happened to be on screen.
    swift = models.ForeignKey(
        'swifts.SwiftVideo', on_delete=models.CASCADE,
        related_name='signals', null=True, blank=True,
    )
    #: Denormalised from `swift.author`. Every affinity read groups by creator,
    #: and this is what turns that from a join into an index scan. Also what
    #: keeps a signal meaningful after its Swift is hard-deleted.
    creator = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='swift_signals_received', null=True, blank=True,
    )

    kind = models.CharField(max_length=20, choices=Kind.choices, db_index=True)

    #: Longest single watch for this event, milliseconds. Zero for taps.
    watch_ms = models.PositiveIntegerField(default=0)
    #: 0–100. Capped at 100 even when a replay pushes total watch past the
    #: video length — `replay` is its own signal and must not double-count here.
    completion_pct = models.PositiveSmallIntegerField(default=0)

    #: Which candidate source put this Swift in front of them, and where in the
    #: page it sat. Position especially: a Swift at slot 1 gets watched because
    #: it is at slot 1, and a model trained without knowing that learns the
    #: feed's own ordering rather than the viewer's taste.
    served_by = models.CharField(max_length=24, blank=True, db_index=True)
    position = models.PositiveSmallIntegerField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [
            # The training scan, and the only large read of this table.
            models.Index(fields=['viewer', '-created_at'], name='swiftsig_viewer_time_idx'),
            # Creator-level aggregation for backfills and repair.
            models.Index(fields=['creator', 'kind'], name='swiftsig_creator_kind_idx'),
        ]

    def __str__(self):
        return f'{self.viewer_id} {self.kind} {self.swift_id}'

    @classmethod
    def polarity_for(cls, kind, completion_pct=0):
        """What one occurrence is worth on the affinity scale.

        A `WATCH` is worth what was watched: nothing at 0%, and up to the value
        of a like at 100%. Everything else is a flat value from `POLARITY`.
        """
        if kind == cls.Kind.WATCH:
            return round((completion_pct / 100.0) * 3.0, 4)
        return cls.POLARITY.get(kind, 0.0)


class CreatorAffinity(models.Model):
    """How much one viewer has liked one creator's work, so far.

    The request-path summary of `SwiftSignal`, and Approx's stand-in for X's
    real-graph: a single number per (viewer, creator) pair predicting whether
    this viewer wants more from this creator.

    `score` is a decayed running total, not an average. Decay is applied
    lazily on write — `decayed_score()` recomputes at read time from
    `last_signal_at`, so a pair nobody has touched for a month does not need a
    nightly job to fall out of the ranking. That is the whole reason there is
    no batch pipeline in this design.
    """

    viewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='creator_affinities',
    )
    creator = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='viewer_affinities',
    )

    score = models.FloatField(default=0.0, db_index=True)
    positive_count = models.PositiveIntegerField(default=0)
    negative_count = models.PositiveIntegerField(default=0)
    #: Consecutive skips with no positive signal in between. Reset by any
    #: positive. This is the "repeated skips of similar content" signal the
    #: brief asks for, held per creator where it is actionable.
    skip_streak = models.PositiveSmallIntegerField(default=0)
    #: Set by an explicit "not interested" or a block. Sticky: nothing but the
    #: viewer undoing it clears this, because a person who said no should not
    #: have to keep saying it.
    suppressed = models.BooleanField(default=False)

    last_signal_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['viewer', 'creator'], name='unique_viewer_creator_affinity',
            ),
        ]
        indexes = [
            # "Who does this viewer like most" — the interaction-graph source.
            models.Index(fields=['viewer', '-score'], name='affinity_viewer_score_idx'),
            # "Who else likes this creator" — the collaborative source.
            models.Index(fields=['creator', '-score'], name='affinity_creator_score_idx'),
        ]

    def __str__(self):
        return f'{self.viewer_id}→{self.creator_id}: {self.score:.2f}'


class SwiftImpression(models.Model):
    """That a Swift was *served* to a viewer — which is not that they saw it.

    The distinction is the entire point of the table. A page of ten is served;
    a viewer who swipes three times has seen three. Recording "served" as
    "seen" burns seven candidates the person never laid eyes on, and after a
    few sessions the feed has quietly consumed everything it had.

    So an impression is provisional. `swifts.SwiftView` — written by the
    client when a card is actually on screen — is the authoritative record of
    having seen something, and `exposure.py` re-admits impressions with no
    matching view once the reserve window has passed.
    """

    viewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='swift_impressions',
    )
    swift = models.ForeignKey(
        'swifts.SwiftVideo', on_delete=models.CASCADE, related_name='impressions',
    )

    first_served_at = models.DateTimeField(auto_now_add=True)
    last_served_at = models.DateTimeField(auto_now=True)
    serve_count = models.PositiveSmallIntegerField(default=1)
    #: The source that won the slot. Feeds the per-source distribution metric,
    #: and is what makes "which source actually produces watched Swifts"
    #: answerable rather than guessed.
    served_by = models.CharField(max_length=24, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['viewer', 'swift'], name='unique_swift_impression',
            ),
        ]
        indexes = [
            models.Index(fields=['viewer', '-last_served_at'], name='impression_viewer_idx'),
        ]

    def __str__(self):
        return f'{self.viewer_id} served swift {self.swift_id}'
