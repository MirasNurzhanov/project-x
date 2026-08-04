from django.db import models
from django.conf import settings


class Club(models.Model):
    class Visibility(models.TextChoices):
        """How far a club spreads. Never wider than the city it was made in.

        The three levels are nested, so each is a promise about the *widest*
        place someone can stand in and still see the club:

          district — the block, the microdistrict, the neighbourhood
          region   — the part of town: a raion, a borough, a ward
          city     — everywhere inside the city limits, and no further

        `global` is not one of them. It is the mark left on the clubs that
        existed before this feature, which named no place at all and so cannot
        be restricted to one without deleting them from everybody's app. It is
        deliberately unselectable — see SELECTABLE below — so the set of
        unscoped clubs can only ever shrink.
        """
        DISTRICT = 'district', 'District'
        REGION = 'region', 'Region'
        CITY = 'city', 'City'
        GLOBAL = 'global', 'Everywhere (legacy)'

    #: What a creator may choose, narrowest first — the order the picker shows.
    SELECTABLE = (Visibility.DISTRICT, Visibility.REGION, Visibility.CITY)

    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    interest = models.ForeignKey(
        'interests.Interest',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='clubs',
    )
    emblem = models.ImageField(upload_to='club_emblems/', null=True, blank=True)
    creator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='created_clubs',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

    # ── Where this club exists ────────────────────────────────────────────────
    # Set once, from the creator's own resolved position, and never from
    # anything the client sent. See clubs.visibility for the rule these feed.
    #
    # The default is CITY rather than GLOBAL on purpose: a club created by some
    # future code path that forgets to scope it ends up with a level it cannot
    # satisfy — city set to nothing matches nobody — and so is invisible rather
    # than visible to the world. Failing closed is the whole point of the field.
    visibility_level = models.CharField(
        max_length=8, choices=Visibility.choices, default=Visibility.CITY, db_index=True,
    )

    # SET_NULL, matching every other reference to a region: the region table is
    # a shared cache and evicting a row must never delete the clubs scoped to
    # it. The names alongside are what a nulled tier falls back to.
    visibility_city = models.ForeignKey(
        'regions.Region', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='clubs_in_city',
    )
    visibility_region = models.ForeignKey(
        'regions.Region', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='clubs_in_region',
    )
    visibility_district = models.ForeignKey(
        'regions.Region', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='clubs_in_district',
    )

    visibility_city_name = models.CharField(max_length=200, blank=True)
    visibility_region_name = models.CharField(max_length=200, blank=True)
    visibility_district_name = models.CharField(max_length=200, blank=True)

    # The same three places as OpenStreetMap knows them — 'R1234'. A club
    # outlives the cache: evict Bostandyk and the foreign key above goes null,
    # and the next person to stand there re-fetches the identical boundary
    # under a *new* primary key. Matching on that key alone would have quietly
    # unscoped the club; matching on this recognises it as the same place.
    visibility_city_osm = models.CharField(max_length=24, blank=True, db_index=True)
    visibility_region_osm = models.CharField(max_length=24, blank=True, db_index=True)
    visibility_district_osm = models.CharField(max_length=24, blank=True, db_index=True)

    visibility_country_code = models.CharField(max_length=2, blank=True)

    class Meta:
        indexes = [
            # The shape of every discovery query: active clubs in my city, then
            # narrowed by level. City leads because it is the most selective
            # column by far — it eliminates the rest of the world first.
            models.Index(
                fields=['visibility_city', 'visibility_level', 'is_active'],
                name='club_visibility_city_idx',
            ),
            # The two narrower tiers are matched on their own once the city has
            # already cut the field down; Django indexes the foreign keys, and
            # these carry the level alongside so the tier check is index-only.
            models.Index(
                fields=['visibility_region', 'visibility_level'],
                name='club_visibility_region_idx',
            ),
            models.Index(
                fields=['visibility_district', 'visibility_level'],
                name='club_visibility_district_idx',
            ),
            # Legacy clubs, which are matched on the level alone.
            models.Index(fields=['visibility_level'], name='club_visibility_level_idx'),
        ]

    def __str__(self):
        return self.name

    @property
    def is_location_restricted(self) -> bool:
        return self.visibility_level in self.SELECTABLE

    @property
    def visibility_place_name(self) -> str:
        """The place this club is scoped to, as a person would say it."""
        if self.visibility_level == self.Visibility.GLOBAL:
            return ''
        return getattr(self, f'visibility_{self.visibility_level}_name', '') or ''


class ClubMember(models.Model):
    class Role(models.TextChoices):
        OWNER = 'owner', 'Owner'
        MEMBER = 'member', 'Member'

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='club_memberships',
    )
    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name='members')
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.MEMBER)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'club')
        ordering = ['joined_at']

    def __str__(self):
        return f'{self.user} — {self.club} ({self.role})'


class ClubEvent(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        APPROVED = 'approved', 'Approved'
        REJECTED = 'rejected', 'Rejected'

    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name='events')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='club_events',
    )
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    location_name = models.CharField(max_length=200, blank=True)
    latitude = models.DecimalField(max_digits=11, decimal_places=8, null=True, blank=True)
    longitude = models.DecimalField(max_digits=11, decimal_places=8, null=True, blank=True)
    event_datetime = models.DateTimeField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='approved_club_events',
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['event_datetime']
        indexes = [
            # The map's meetup pins: approved events that have not expired.
            models.Index(
                fields=['status', 'expires_at'], name='clubevent_live_idx',
            ),
        ]

    def __str__(self):
        return f'{self.title} @ {self.club}'


class ClubMessage(models.Model):
    class MessageType(models.TextChoices):
        TEXT = 'text', 'Text'
        MEETUP_PROPOSAL = 'meetup_proposal', 'Meetup Proposal'

    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name='messages')
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='club_messages',
    )
    content = models.TextField(blank=True)
    image = models.ImageField(upload_to='club_messages/', null=True, blank=True)
    # Set when the client uploaded straight to Cloudinary and only the URL
    # passed through here. Takes precedence over `image` when both exist.
    image_url = models.URLField(max_length=500, blank=True, default='')
    message_type = models.CharField(max_length=20, choices=MessageType.choices, default=MessageType.TEXT)
    event = models.ForeignKey(
        ClubEvent,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='proposal_messages',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f'{self.sender} → {self.club}: {self.content[:40]}'
