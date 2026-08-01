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
    latitude = models.DecimalField(max_digits=11, decimal_places=8, null=True, blank=True)
    longitude = models.DecimalField(max_digits=11, decimal_places=8, null=True, blank=True)

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
    zone_preset = models.CharField(max_length=50, blank=True)
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
