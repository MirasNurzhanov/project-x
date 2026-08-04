from django.contrib.auth.models import AbstractUser
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from datetime import date
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
    # A zone has two shapes, and `zone_mode` says which one is live. They are
    # stored side by side rather than in one polymorphic field so switching
    # back and forth never loses the radius you had set, or the city you picked.

    class ZoneMode(models.TextChoices):
        RADIUS = 'radius', 'Radius'
        REGION = 'region', 'Region'

    # Metres. Was a three-value choice field (100/500/1000); now a free dial.
    # The floor keeps a zone from being smaller than GPS error can resolve, and
    # the ceiling is where a circle stops being the right tool — past ~5km you
    # want a city, which is what REGION mode is for.
    ZONE_RADIUS_MIN_M = 50
    ZONE_RADIUS_MAX_M = 5000

    zone_audio = models.FileField(upload_to='zones/', null=True, blank=True, storage=RawMediaCloudinaryStorage())
    zone_mode = models.CharField(max_length=8, choices=ZoneMode.choices, default=ZoneMode.RADIUS)
    zone_radius = models.IntegerField(
        default=500,
        validators=[
            MinValueValidator(ZONE_RADIUS_MIN_M),
            MaxValueValidator(ZONE_RADIUS_MAX_M),
        ],
    )
    # SET_NULL, not CASCADE: regions are a shared cache, and evicting one must
    # never delete the people whose zone pointed at it. A null region with
    # mode=region falls back to the radius — see `effective_zone_mode`.
    zone_region = models.ForeignKey(
        'regions.Region',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='zoned_users',
    )
    zone_active = models.BooleanField(default=False)

    # ── Music zone ────────────────────────────────────────────────────────────
    # How far the *sound* carries, which is not the same question as how far you
    # carry. The zone is a radar: it says who counts as near you, and people
    # widen it to be found. Earshot is the opposite instinct — a track you want
    # a friend at the next table to catch is not one you want playing for
    # everyone in a 5km circle. They were one number, so tuning either ruined
    # the other.
    #
    # Off by default, and inheriting `zone_radius` while off, so nothing changes
    # for anyone who never opens this. See `music_radius_m`.
    music_zone_separate = models.BooleanField(default=False)
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
    def music_radius_m(self):
        """Metres someone must be within to hear this zone's track.

        Falls back to the zone's own radius, so a user who never separates the
        two keeps the single circle they already had.
        """
        if self.music_zone_separate:
            return self.music_zone_radius
        return self.zone_radius

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
    def effective_zone_mode(self):
        """The mode actually in force.

        `zone_mode` is what the user asked for; this is what can be drawn. They
        differ only when a region was picked and has since been evicted from
        the cache, and falling back to the circle beats showing no zone at all.
        """
        if self.zone_mode == self.ZoneMode.REGION and self.zone_region_id:
            return self.ZoneMode.REGION
        return self.ZoneMode.RADIUS

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
