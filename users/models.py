from django.contrib.auth.models import AbstractUser
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from datetime import date, timedelta
from cloudinary_storage.storage import RawMediaCloudinaryStorage


class User(AbstractUser):
    class AgeTier(models.TextChoices):
        MINOR = 'minor', '13-17'
        ADULT = 'adult', '18+'

    email = models.EmailField(unique=True)
    date_of_birth = models.DateField(null=True, blank=True)
    age_tier = models.CharField(
        max_length=5,
        choices=AgeTier.choices,
        blank=True,
    )
    bio = models.TextField(blank=True)
    profile_picture = models.ImageField(upload_to='profile_pictures/', null=True, blank=True)
    latitude = models.DecimalField(max_digits=11, decimal_places=8, null=True, blank=True, db_index=True)
    longitude = models.DecimalField(max_digits=11, decimal_places=8, null=True, blank=True, db_index=True)

    # When the position above was last *reported*, and how good the phone said
    # the fix was. Both exist because anything deciding access from a location
    # has to be able to tell a position apart from a memory of one: `updated_at`
    # moves on any save at all, so it cannot answer "is this where they are".
    #
    # Null on rows that predate the columns and on anyone who has never shared
    # a position; readers fall back to `updated_at` for the age, which is an
    # upper bound rather than an answer — see clubs.visibility.
    location_updated_at = models.DateTimeField(null=True, blank=True, db_index=True)
    #: Metres, as reported by the platform. Null when the phone did not say.
    location_accuracy_m = models.FloatField(null=True, blank=True)

    interests = models.ManyToManyField('interests.Interest', blank=True, related_name='users')

    # ── Zone ──────────────────────────────────────────────────────────────────
    # A zone has three shapes and `zone_type` says which one is live. The
    # supporting fields are stored side by side rather than in one polymorphic
    # column so switching back and forth never loses the radius you had set, or
    # the city you picked.
    #
    # Which means the type is the *only* thing that decides how a zone is
    # tested. Never infer it from which fields happen to be populated — a city
    # zone keeps its old radius sitting right there, and reading that as "this
    # is a circle" is the bug this field exists to make impossible. See
    # users/zones.py, which is the one place containment is decided.

    class ZoneType(models.TextChoices):
        """The three shapes, stored explicitly.

        CITY and DISTRICT were one value (`region`) until the difference
        started to matter: both draw a polygon, but they are different offers —
        a district zone covers a neighbourhood and a city zone covers everyone
        in town. Collapsing them meant the only record of which one a user had
        chosen was the `kind` of the Region they happened to point at, which is
        upstream classification rather than the user's intent.
        """
        CIRCLE = 'circle', 'Circle around you'
        DISTRICT = 'district', 'District boundary'
        CITY = 'city', 'City boundary'

    #: The two types whose shape is a polygon rather than a distance.
    POLYGON_ZONE_TYPES = ('district', 'city')

    # Metres. Was a three-value choice field (100/500/1000); now a free dial.
    # The floor keeps a zone from being smaller than GPS error can resolve, and
    # the ceiling is where a circle stops being the right tool — past ~5km you
    # want a city, which is what the CITY type is for.
    ZONE_RADIUS_MIN_M = 50
    ZONE_RADIUS_MAX_M = 5000

    zone_audio = models.FileField(upload_to='zones/', null=True, blank=True, storage=RawMediaCloudinaryStorage())
    zone_type = models.CharField(
        max_length=8, choices=ZoneType.choices, default=ZoneType.CIRCLE, db_index=True,
    )
    #: Metres, and meaningful only while `zone_type` is CIRCLE. Kept rather than
    #: nulled on a switch to a polygon type so coming back restores the dial the
    #: user had set; containment ignores it outright for the other two types.
    zone_radius = models.IntegerField(
        default=500,
        validators=[
            MinValueValidator(ZONE_RADIUS_MIN_M),
            MaxValueValidator(ZONE_RADIUS_MAX_M),
        ],
    )
    # SET_NULL, not CASCADE: regions are a shared cache, and evicting one must
    # never delete the people whose zone pointed at it. Which leaves a real
    # state where a district or city zone has no boundary — `zone_contains`
    # raises there rather than falling back to the circle, because silently
    # shrinking somebody's city to a 500m ring is worse than an error.
    zone_region = models.ForeignKey(
        'regions.Region',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='zoned_users',
    )
    zone_active = models.BooleanField(default=False)

    # ── Music zone ────────────────────────────────────────────────────────────
    # The circle the *sound* carries to, which is not the same question as how
    # far you carry. The zone is a radar: it says who counts as near you, and
    # people widen it to be found. A music zone is the opposite instinct — a
    # track you want a friend at the next table to catch is not one you want
    # playing for everyone in a 5km circle. They were one number, so tuning
    # either ruined the other; they are two now, and this is the second.
    #
    #: On/off for the whole thing. Off means there is no music zone: nobody is
    #: sent the track, nobody is alerted, and no circle is drawn on anyone's
    #: map — the owner's included. The track and the radius stay where they
    #: are, so switching back on restores what was there.
    #:
    #: This used to be a mute that left a circle behind, sitting next to a
    #: second switch (`music_zone_separate`) that chose whether the music
    #: borrowed `zone_radius` instead of using its own. Two switches for one
    #: feature, and neither of them was "is there a music zone". Now there is
    #: one, `music_zone_radius` is always the radius in force, and turning it
    #: off removes the zone rather than quietly parking it.
    #:
    #: Default True so every zone that predates the field keeps broadcasting.
    #: See `broadcasts_zone_track`.
    music_zone_active = models.BooleanField(default=True)
    #: Always the music zone's own radius. It never inherits `zone_radius` —
    #: a music zone that followed the map zone was indistinguishable from not
    #: having one, which is what the removed `music_zone_separate` flag meant.
    music_zone_radius = models.IntegerField(
        default=200,
        validators=[
            MinValueValidator(ZONE_RADIUS_MIN_M),
            MaxValueValidator(ZONE_RADIUS_MAX_M),
        ],
    )
    #: Both directions of the music-zone alert: whether friends stepping into
    #: your earshot is news to you, and whether their music is news to you. One
    #: switch rather than two, because "quiet, please" is one intention.
    music_zone_alerts = models.BooleanField(default=True)

    # ── Apple Music ───────────────────────────────────────────────────────────
    # A catalogue track, resolved server-side from a song id (see
    # users/applemusic.py) so nothing here is a URL the phone made up.
    #
    # `zone_apple_preview_url` is Apple's 30-second preview, and that is a hard
    # ceiling rather than a shortcut: full playback requires MusicKit and the
    # *listener's* own subscription, and a stranger walking past your zone has
    # neither. A preview is what can legally and technically reach them.
    zone_apple_id = models.CharField(max_length=64, blank=True)
    zone_apple_title = models.CharField(max_length=200, blank=True)
    zone_apple_artist = models.CharField(max_length=200, blank=True)
    zone_apple_artwork = models.URLField(max_length=500, blank=True)
    zone_apple_preview_url = models.URLField(max_length=500, blank=True)

    # ── The loop ──────────────────────────────────────────────────────────────
    # Which slice of the track actually repeats. Source-agnostic on purpose: an
    # upload is a whole song and the interesting part is fifteen seconds of it,
    # and even Apple's 30-second preview is usually longer than anyone wants
    # going round and round.
    #
    # A null end means "play to the end of the file", which is what everyone
    # gets until they trim — the duration is not known here, and storing a
    # guess would be worse than storing nothing. Both are reset whenever the
    # track changes; a window chosen for one song means nothing in another.
    MIN_LOOP_MS = 3000

    zone_track_start_ms = models.PositiveIntegerField(default=0)
    zone_track_end_ms = models.PositiveIntegerField(null=True, blank=True)

    @property
    def has_trimmed_loop(self):
        return self.zone_track_start_ms > 0 or self.zone_track_end_ms is not None

    @property
    def zone_track_title(self):
        """What is playing, by name — empty when there is no name to give.

        An upload has only a filename, and "nocturne_04_ab12cd.mp3" in a push
        notification is worse than a sentence that does not name the track at
        all. Callers phrase around the empty string rather than filling it.
        """
        if self.zone_apple_preview_url:
            return self.zone_apple_title
        return ''

    @property
    def has_zone_track(self):
        return bool(self.zone_apple_preview_url or self.zone_audio)

    @property
    def broadcasts_zone_track(self):
        """Whether anyone can currently hear this zone.

        Both halves have to hold: something loaded, and a music zone to play it
        through. Every viewer-facing gate asks this rather than `has_zone_track`,
        so switching the zone off is one condition in one place instead of a
        flag each caller has to remember. The owner's own screens keep reading
        the track directly — removing the zone hides you from other people, not
        your song from you.
        """
        return self.music_zone_active and self.has_zone_track

    @property
    def zone_boundary_available(self):
        """Whether this zone currently has a shape that can be tested at all.

        False for a district or city whose Region has been evicted, and for a
        circle on a user with no stored position. Lets a caller sweeping many
        rows skip the unanswerable ones instead of catching per row.
        """
        from . import zones
        return zones.zone_boundary_available(self)

    def zone_contains(self, lat, lon, *, tolerance_m=0.0) -> bool:
        """Whether a location falls inside this user's zone.

        The single entry point — every consumer goes through here rather than
        reimplementing "distance or polygon" per feature. Raises
        `zones.ZoneBoundaryUnavailable` when the zone has no usable shape; see
        users/zones.py for why that is an exception and not a False.
        """
        from . import zones
        return zones.is_location_inside_zone(self, lat, lon, tolerance_m=tolerance_m)

    @property
    def effective_zone_mode(self):
        """What the client should *draw*: 'radius' or 'region'.

        A rendering hint, deliberately coarser than `zone_type` — the map only
        needs to know circle-or-polygon, and both polygon types draw
        identically. Kept as its own value rather than derived on the client so
        an evicted Region resolves to something drawable in one place: a city
        zone with no boundary left has nothing to trace, and a circle is the
        only honest thing to put on screen.

        This is a *drawing* fallback and nothing else. Containment does not have
        an equivalent — see `zone_contains`, which raises instead, because a
        zone that draws as a circle while covering a city would be a lie the map
        tells quietly.
        """
        if self.zone_type in self.POLYGON_ZONE_TYPES and self.zone_region_id:
            return 'region'
        return 'radius'

    # ── Map check-in ──────────────────────────────────────────────────────────
    # Whether this user is published to other people's maps *right now*, which
    # is a different question from whether the OS lets Aura read their position.
    # Granting "Allow While Using" says where the phone is; it says nothing
    # about wanting to be a dot on a stranger's screen. Treating the first as
    # consent for the second is what App Review rejected under 5.1.2(i), and
    # the three concepts live apart now:
    #
    #   OS permission    — may we read a position at all
    #   is_checked_in    — may anyone else be shown it
    #   location_sharing — how precisely it is stated to those who may see it
    #
    # Default False, and False for every account that predates the column: a
    # user is off the map until they say otherwise, by hand, this session.
    #
    # There is deliberately no "stay checked in" preference and no server-side
    # renewal. A check-in is one explicit act with an end to it, and coming
    # back means performing it again — anything that extended this without a
    # tap would be the automatic check-in the guideline forbids.

    #: The durations offered, in minutes. A closed set rather than a free
    #: number, so no client can ask to be published for a week.
    CHECK_IN_DURATION_CHOICES_M = (15, 60, 240)
    CHECK_IN_DEFAULT_MINUTES = 60

    is_checked_in = models.BooleanField(default=False, db_index=True)
    #: When the tap happened. Separate from the expiry so "how long have they
    #: been visible" stays answerable, and so a re-check-in is distinguishable
    #: from an extension in the record.
    checked_in_at = models.DateTimeField(null=True, blank=True)
    #: When it lapses. Enforced on *read* — see users.visibility.checked_in_q —
    #: rather than by a sweep, so an expired check-in stops publishing the
    #: instant it passes even if no worker ever runs.
    check_in_expires_at = models.DateTimeField(null=True, blank=True, db_index=True)

    def is_on_map(self, now=None):
        """Whether this user may currently be drawn on anybody else's map.

        The Python counterpart of `visibility.checked_in_q`, which is the same
        rule in SQL. Both exist because list endpoints have to filter in the
        database and per-row checks cannot; they must never drift apart, so
        neither grows a condition the other lacks.
        """
        if not self.is_checked_in or self.check_in_expires_at is None:
            return False
        return self.check_in_expires_at > (now or timezone.now())

    def check_in_state(self, now=None):
        """What the client renders: the flag, the clock, and the seconds left.

        `is_checked_in` is reported as the *effective* value rather than the
        stored one, so a lapsed check-in reads as checked out everywhere
        without the client having to compare timestamps and get the timezone
        wrong.
        """
        now = now or timezone.now()
        live = self.is_on_map(now)
        return {
            'is_checked_in': live,
            'checked_in_at': self.checked_in_at.isoformat() if live and self.checked_in_at else None,
            'check_in_expires_at': (
                self.check_in_expires_at.isoformat() if live and self.check_in_expires_at else None
            ),
            'seconds_remaining': (
                max(0, int((self.check_in_expires_at - now).total_seconds())) if live else 0
            ),
            'duration_choices_minutes': list(self.CHECK_IN_DURATION_CHOICES_M),
            'default_duration_minutes': self.CHECK_IN_DEFAULT_MINUTES,
        }

    def apply_check_in(self, minutes):
        """Set the check-in fields for `minutes` from now. Does not save."""
        now = timezone.now()
        self.is_checked_in = True
        self.checked_in_at = now
        self.check_in_expires_at = now + timedelta(minutes=minutes)
        return ['is_checked_in', 'checked_in_at', 'check_in_expires_at']

    def apply_check_out(self):
        """Take this user off every map. Does not save.

        Motion goes with it: a speed trail is drawn from a published position,
        so leaving one behind would outlive the position it belongs to.
        """
        self.is_checked_in = False
        self.checked_in_at = None
        self.check_in_expires_at = None
        self.motion_class = self.MotionClass.STATIONARY
        self.motion_updated_at = None
        return [
            'is_checked_in', 'checked_in_at', 'check_in_expires_at',
            'motion_class', 'motion_updated_at',
        ]

    # ── Privacy ───────────────────────────────────────────────────────────────
    # Two independent axes, deliberately kept apart:
    #   location_sharing — am I out there on the map right now, and how precisely
    #   is_private + visibility_* — who sees my stuff once they have found me
    # The old three-way `privacy_mode` (active/ghost/invisible) conflated both;
    # see users/migrations/0010 for the mapping.

    class LocationSharing(models.TextChoices):
        LIVE = 'live', 'Live'
        APPROXIMATE = 'approximate', 'Approximate'
        PAUSED = 'paused', 'Paused'

    location_sharing = models.CharField(
        max_length=11,
        choices=LocationSharing.choices,
        default=LocationSharing.LIVE,
    )

    class Audience(models.TextChoices):
        EVERYONE = 'everyone', 'Everyone'
        FRIENDS = 'friends', 'Friends'
        NOBODY = 'nobody', 'Nobody'

    # A private account shows non-approved viewers username + avatar and nothing
    # else. "Approved" means an accepted Hi (connected) or an accepted friend
    # request — the app's existing request queue is the approval queue.
    is_private = models.BooleanField(default=False)

    # Interests default to Everyone because they are the matching primitive the
    # whole app runs on; the rest default to Friends.
    visibility_interests = models.CharField(
        max_length=8, choices=Audience.choices, default=Audience.EVERYONE)
    visibility_stories = models.CharField(
        max_length=8, choices=Audience.choices, default=Audience.FRIENDS)
    visibility_stats = models.CharField(
        max_length=8, choices=Audience.choices, default=Audience.FRIENDS)
    visibility_zone_audio = models.CharField(
        max_length=8, choices=Audience.choices, default=Audience.FRIENDS)
    visibility_connections = models.CharField(
        max_length=8, choices=Audience.choices, default=Audience.FRIENDS)
    # Friends-only by default and deliberately so: on a proximity app a public
    # Instagram link ties "stranger standing near me" to a real identity.
    visibility_links = models.CharField(
        max_length=8, choices=Audience.choices, default=Audience.FRIENDS)
    # Presence is "am I around right now", not "where am I" — a separate axis
    # from location_sharing on purpose, so pausing the map does not force you
    # to look permanently offline to your friends, and vice versa.
    visibility_presence = models.CharField(
        max_length=8, choices=Audience.choices, default=Audience.FRIENDS)

    discoverable_by_username = models.BooleanField(default=True)
    discoverable_by_interest = models.BooleanField(default=True)

    nationality = models.CharField(max_length=100, blank=True, null=True)
    profession = models.CharField(max_length=100, blank=True, null=True)

    push_token = models.CharField(max_length=200, blank=True, null=True)

    # ── What is worth a buzz ──────────────────────────────────────────────
    # One switch per kind of news, all defaulting True: these were added after
    # the app already pushed for messages and Hi requests, and a default of
    # False would have silently turned that off for everyone who upgraded.
    #
    # These are *not* a substitute for the OS permission. Notifications can be
    # switched off at the system level, in which case none of this is
    # reachable — see Settings → Permissions. These decide what to send to
    # someone who has already agreed to be reachable at all.
    #
    # notifications.push.CATEGORIES maps each category to the field that gates
    # it; a field added here and not there gates nothing.
    notify_messages = models.BooleanField(default=True)
    notify_club_messages = models.BooleanField(default=True)
    notify_club_events = models.BooleanField(default=True)
    #: Hi requests, friend requests, and the acceptances of both.
    notify_social = models.BooleanField(default=True)
    #: Reactions and replies to your own stories.
    notify_story_activity = models.BooleanField(default=True)

    # ── Presence ──────────────────────────────────────────────────────────────
    # Deliberately *not* `updated_at`. That field is auto_now, so it moves on
    # any save at all — a profile edit, a privacy change — and it never moves
    # for someone with location sharing paused, who reports no position. It
    # means "row last modified", which is not the same claim as "user is here".
    #
    # Written only by users.middleware.PresenceMiddleware, and only for
    # requests the app marks as foreground. The background location task keeps
    # hitting the API every 30s with the app closed; if any authenticated
    # request counted, those users would read online forever.
    last_seen = models.DateTimeField(null=True, blank=True, db_index=True)

    class MotionClass(models.TextChoices):
        STATIONARY = 'stationary', 'Stationary'
        MOVING = 'moving', 'Moving'
        FAST = 'fast', 'Fast'

    #: Coarse bucket only. Raw speed is never stored and never leaves the
    #: server — see proximity.views.classify_speed for the thresholds. There is
    #: deliberately no heading column: the client orients a trail from the
    #: positions it has already observed, so no bearing exists here to leak.
    motion_class = models.CharField(
        max_length=10, choices=MotionClass.choices, default=MotionClass.STATIONARY)
    motion_updated_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username']

    def save(self, *args, **kwargs):
        update_fields = kwargs.get('update_fields')
        if update_fields is None or 'date_of_birth' in update_fields:
            if self.date_of_birth:
                today = date.today()
                age = today.year - self.date_of_birth.year - (
                    (today.month, today.day) < (self.date_of_birth.month, self.date_of_birth.day)
                )
                self.age_tier = self.AgeTier.MINOR if age < 18 else self.AgeTier.ADULT
                if update_fields is not None:
                    kwargs['update_fields'] = list(update_fields) + ['age_tier']
        super().save(*args, **kwargs)

    # Old app builds still speak active/ghost/invisible. Read-only bridge so
    # those clients keep working until they update; writes go through
    # PrivacySettingsView, which translates.
    LEGACY_PRIVACY_MODE = {
        LocationSharing.LIVE: 'active',
        LocationSharing.APPROXIMATE: 'ghost',
        LocationSharing.PAUSED: 'invisible',
    }
    LEGACY_PRIVACY_MODE_REVERSE = {v: k for k, v in LEGACY_PRIVACY_MODE.items()}

    @property
    def privacy_mode(self):
        return self.LEGACY_PRIVACY_MODE.get(self.location_sharing, 'active')

    def __str__(self):
        return self.email


class ProfileLink(models.Model):
    """An external link on a profile.

    `url` is always the canonical form produced by users.links.normalize_link —
    https, no credentials, no port, no tracking parameters. `platform` and
    `handle` are derived offline from the host and path; the server never
    fetches the link.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='links')
    url = models.URLField(max_length=300)
    #: 'instagram', 'spotify', ... or 'website' for anything unrecognised.
    platform = models.CharField(max_length=20, default='website')
    #: Username parsed from the path, without the leading '@'. May be blank.
    handle = models.CharField(max_length=64, blank=True)
    #: What the profile renders — "@name", or the bare host for unknown sites.
    label = models.CharField(max_length=64)
    #: Manual ordering; lower first.
    position = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['position', 'pk']
        constraints = [
            models.UniqueConstraint(fields=['user', 'url'], name='unique_link_per_user'),
        ]

    def __str__(self):
        return f'{self.user_id}: {self.label}'


class SocialAccount(models.Model):
    """A Google or Apple identity attached to an Aura account.

    Keyed on the provider's `sub`, not on the email. `sub` is the only value
    either provider promises never changes: Apple's is per-app and permanent,
    Google's survives the user renaming their Gmail. Email does not — people
    change it, and Apple hands out a per-app relay address that has no relation
    to the real one. An account matched by email would come apart the first
    time either happened.

    `email` here is a record of what the provider said at link time, kept for
    support ("which Google account is this?"). Nothing authenticates off it,
    and it is never written back onto the User.
    """

    class Provider(models.TextChoices):
        GOOGLE = 'google', 'Google'
        APPLE = 'apple', 'Apple'

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='social_accounts')
    provider = models.CharField(max_length=10, choices=Provider.choices)
    #: The provider's subject identifier. Apple's are ~44 chars, Google's ~21;
    #: 255 is the width every other implementation uses and costs nothing.
    subject = models.CharField(max_length=255)
    email = models.EmailField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            # The load-bearing one. Without it, two rows could claim the same
            # Google identity for two different accounts and which one you
            # signed into would come down to row order.
            models.UniqueConstraint(
                fields=['provider', 'subject'], name='unique_social_identity'),
            # One Google *and* one Apple per account is fine; two Googles is a
            # bug — the second link would silently orphan the first.
            models.UniqueConstraint(
                fields=['user', 'provider'], name='unique_provider_per_user'),
        ]

    def __str__(self):
        return f'{self.get_provider_display()} → {self.user_id}'
