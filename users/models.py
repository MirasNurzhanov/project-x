from django.contrib.auth.models import AbstractUser
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from datetime import date


class User(AbstractUser):
    class AgeTier(models.TextChoices):
        MINOR = 'minor', '13-17'
        ADULT = 'adult', '18+'

    email = models.EmailField(unique=True)
    deletion_requested_at = models.DateTimeField(null=True, blank=True, editable=False)
    delete_after = models.DateTimeField(null=True, blank=True, db_index=True, editable=False)
    # A credential-free identity for retained shared history after the original
    # account is purged. Never usable for authentication or discovery.
    retired_account_id = models.PositiveBigIntegerField(null=True, blank=True, unique=True, editable=False)
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

    zone_audio = models.FileField(upload_to='zones/', null=True, blank=True)
    #: Defaults to CITY, paired with `zone_follows_location=True` below, so a
    #: new account's reach is "the city I am in" rather than a 500m ring. A
    #: circle is still a first-class choice — it is simply no longer the one
    #: nobody picked.
    #:
    #: A brand new user has this set to CITY with no `zone_region` yet, because
    #: the boundary cannot be resolved until their first location report. That
    #: state is safe and already handled: `_viewer_zone_box` filters on the
    #: circle box and `_viewer_zone_contains` catches ZoneBoundaryUnavailable
    #: and falls back to `zone_radius` (see proximity/views.py). So discovery
    #: behaves exactly as it used to until the city is known, and widens once
    #: it is. Do not "fix" that by defaulting zone_region — there is no correct
    #: region to point at before we know where the person is.
    zone_type = models.CharField(
        max_length=8, choices=ZoneType.choices, default=ZoneType.CITY, db_index=True,
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

    #: Whether `zone_region` should re-point itself as its owner moves.
    #:
    #: A polygon zone used to be a *pin*: whichever district you were standing
    #: in when you pressed the button, forever. That is right for somebody who
    #: chose a place deliberately and wrong for almost everybody else — move
    #: across town and your zone keeps covering the neighbourhood you left,
    #: with nothing on screen saying so, until you happen to reopen the zone
    #: screen and set it again.
    #:
    #: With this on, every location report re-points the zone at the district
    #: (or city) the position now falls in. It costs no upstream lookup: the
    #: containing boundaries are already computed on that path for club
    #: visibility — see `regions.places.refresh_quietly` — so this reads a
    #: snapshot that exists either way.
    #:
    #: Defaults **True**, which is what makes the CITY default above mean "the
    #: city I am in" rather than "a city, once you pick one". It is also what
    #: resolves the empty `zone_region` a new account starts with: the first
    #: location report runs `follow_location` and the zone lands on their city.
    #:
    #: A field default only applies to rows created after the migration, so no
    #: existing zone changes under anybody — which was the original reason this
    #: defaulted False, and that guarantee is unchanged. The client still turns
    #: it off for a zone picked by name, which is the distinction it encodes:
    #: *where I am* versus *this place*.
    zone_follows_location = models.BooleanField(default=True)

    #: Set when a following zone had nowhere to move to — the user is somewhere
    #: OSM has no district mapped for, so `zone_region` is still the last one
    #: that resolved and no longer contains them.
    #:
    #: The zone deliberately keeps working in that state rather than emptying
    #: itself: a zone that silently stops covering anybody is a worse answer
    #: than one that is briefly behind, and widening it to the whole city
    #: instead would hand somebody a far larger zone than they asked for
    #: without being asked. The flag is what lets the zone screen say which of
    #: the two is happening.
    zone_follow_stale = models.BooleanField(default=False)

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
    #: Moderation state of the *current* zone track — Zone Music is
    #: user-generated audio other people hear (App Review 1.2). QUARANTINED is
    #: the automatic mute after enough independent reports
    #: (`moderation.reasons.AUTO_QUARANTINE_REPORTS`), reversible by a
    #: moderator; REMOVED is a moderator taking the track down. Either one
    #: silences the track for every listener (`zone_track_muted`) while the
    #: owner keeps seeing it. A new track is new content and starts at OK.
    zone_audio_moderation = models.CharField(max_length=12, default='ok', choices=(
        ('ok', 'OK'), ('quarantined', 'Quarantined'), ('removed', 'Removed'),
    ))

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
    def zone_track_report_snapshot(self):
        """What a moderator needs to find the reported track again: which kind,
        its name where there is one, and where the audio is. Snapshotted when a
        Zone Music report is filed, because the owner can replace the track."""
        if self.zone_apple_preview_url:
            name = ' — '.join(part for part in (self.zone_apple_title, self.zone_apple_artist) if part)
            return f'Apple Music preview: {name or "untitled"} ({self.zone_apple_preview_url})'
        if self.zone_audio:
            try:
                url = self.zone_audio.url
            except Exception:  # storage misconfigured: still name the file
                url = self.zone_audio.name
            return f'Uploaded audio: {url}'
        return ''

    def remove_zone_track(self):
        """Take the track out of this user's zone. Returns the fields to save.

        The moderation removal. The file itself is not deleted from storage —
        the report's snapshot points at it, and it is the evidence."""
        self.zone_audio = None
        self.zone_apple_id = ''
        self.zone_apple_title = ''
        self.zone_apple_artist = ''
        self.zone_apple_artwork = ''
        self.zone_apple_preview_url = ''
        self.zone_audio_moderation = 'removed'
        return ['zone_audio', 'zone_apple_id', 'zone_apple_title', 'zone_apple_artist',
                'zone_apple_artwork', 'zone_apple_preview_url', 'zone_audio_moderation']

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
        return self.music_zone_active and self.has_zone_track and not self.zone_track_muted

    @property
    def zone_track_muted(self):
        """Silenced by moderation, pending review or for good."""
        return self.zone_audio_moderation in ('quarantined', 'removed')

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

    # ── Privacy ───────────────────────────────────────────────────────────────
    # Two independent axes, deliberately kept apart:
    #   location_sharing — am I out there on the map right now, and how precisely
    #   is_private + visibility_* — who sees my stuff once they have found me
    # The old three-way `privacy_mode` (active/ghost/invisible) conflated both;
    # see users/migrations/0010 for the mapping.

    #: Anonymous profile mode and the mutual off switch for the map.
    #: Profile details, stories and Swifts are hidden from everyone else,
    #: including friends; owners keep access to their own content.
    #:
    #: Ghost Mode is mutual by design: while it is on, this user is on nobody
    #: else's map *and* nobody else is on theirs. Half of that is privacy and
    #: half is fairness — a one-way ghost watches a map it has opted out of
    #: appearing on.
    #:
    #: What it deliberately does *not* do is stop us storing a position. The
    #: position still answers this user's own questions — which clubs they can
    #: see and join, which region they are in — so ghosting hides you from the
    #: map without logging you out of the product. The `paused` mode this
    #: replaced wiped the stored coordinate, and an empty club list was the
    #: result.
    #:
    #: Precision is not a setting any more, and there is nothing about it here:
    #: friends see a precise position and everybody else sees a ~500m cell,
    #: decided per viewer at read time. See `visibility.coordinates_for`.
    ghost_mode = models.BooleanField(default=False, db_index=True)

    # ── Discovery and check-in (App Review 5.1.2(i)) ─────────────────────────
    # Visibility is decided by relationship tier, and only one tier is
    # automatic:
    #
    #   friends            see this user whenever Ghost Mode is off — across
    #                      launches and in the background. No check-in. This is
    #                      the feature the `location` background mode exists for.
    #   connected/strangers see this user only after an explicit check-in in
    #                      the *current* session, and only while that session
    #                      lasts.
    #
    # `discovery_mode` is the Privacy & safety setting. PRIVATE is friends-only,
    # permanently: there is no check-in to perform. DISCOVERABLE only makes the
    # check-in *possible* — it never makes anyone visible by itself, which is the
    # line App Review drew: nothing that is a stored preference may put a person
    # on a stranger's map.
    #
    # A check-in is `check_in_session` + `checked_in_at`, minted by a deliberate
    # tap (proximity.views.CheckInView) and ended by: the next launch of the app
    # (it cannot present the token, which is held in memory only — see
    # EndStaleCheckInView), signing out, switching to PRIVATE, turning Ghost Mode
    # on, checking out, or `visibility.CHECK_IN_MAX_AGE` passing. None of those
    # touches the stored position: clubs, meetups and region lookup keep reading
    # it whether or not anyone can see it.
    class DiscoveryMode(models.TextChoices):
        PRIVATE = 'private', 'Private'
        DISCOVERABLE = 'discoverable', 'Discoverable'

    discovery_mode = models.CharField(
        max_length=12, choices=DiscoveryMode.choices, default=DiscoveryMode.DISCOVERABLE,
    )
    check_in_session = models.UUIDField(null=True, blank=True, editable=False)
    checked_in_at = models.DateTimeField(null=True, blank=True, editable=False)

    def is_checked_in(self, now=None):
        """Whether this user may currently be seen by non-friends (and see them).

        All of it, every time, rather than trusting the columns alone: a row
        left checked in by a stale session, or one whose owner switched to
        Private or Ghost, must read as not checked in without waiting for a
        cleanup. The SQL half is `visibility.checked_in_q`.
        """
        from django.utils import timezone

        from . import visibility

        if self.ghost_mode or self.discovery_mode != self.DiscoveryMode.DISCOVERABLE:
            return False
        if self.check_in_session is None or self.checked_in_at is None:
            return False
        return (now or timezone.now()) - self.checked_in_at <= visibility.check_in_max_age()

    def check_in_expires_at(self):
        from . import visibility

        if not self.is_checked_in():
            return None
        return self.checked_in_at + visibility.check_in_max_age()

    def start_check_in(self):
        """Mint a new session. Returns the fields to save."""
        import uuid

        from django.utils import timezone

        self.check_in_session = uuid.uuid4()
        self.checked_in_at = timezone.now()
        return ['check_in_session', 'checked_in_at']

    def end_check_in(self):
        """Forget the session, and nothing else. Returns the fields to save."""
        self.check_in_session = None
        self.checked_in_at = None
        return ['check_in_session', 'checked_in_at']

    def is_sharing(self):
        """Whether this user may be drawn on somebody else's map at all.

        The choice alone. Says nothing about whether we currently hold a
        position for them, which is exactly what the write path needs: refusing
        a location update because the position it replaces has gone stale would
        be a deadlock. SQL counterpart is `visibility.sharing_q`.
        """
        return not self.ghost_mode

    def has_fresh_position(self, now=None, *, for_friend=False, max_age_s=None):
        """Whether the position on this row is recent enough to publish.

        There has to *be* one first. That check was previously redundant —
        sharing defaulted to off, so a row with no coordinate was never asked —
        but sharing defaults to on now, and `updated_at` ticks for reasons that
        have nothing to do with location. Without it a brand-new account with
        no position at all reads as freshly located, because it was freshly
        *saved*.

        `updated_at` stays as the fallback for rows predating the
        `location_updated_at` column — see `visibility.fresh_position_q`, which
        is the SQL half and carries the reasoning.
        """
        from django.utils import timezone

        from . import visibility

        if self.latitude is None or self.longitude is None:
            return False
        stamp = self.location_updated_at or self.updated_at
        if stamp is None:
            return False
        age = ((now or timezone.now()) - stamp).total_seconds()
        if max_age_s is None:
            max_age_s = visibility.position_max_age_s(for_friend=for_friend)
        return age <= max_age_s

    def is_on_map(self, now=None, *, for_friend=False, max_age_s=None):
        """Whether this user may be drawn on anybody else's map right now.

        The single named predicate for "is this person published", kept as a
        method rather than inlining at ~a dozen call sites: the rule has been
        re-expressed three times already, and each time it was one edit here
        instead of a hunt. The SQL counterpart is `visibility.on_map_q`, and the
        two must never disagree.

        Both halves, and the second is not decoration: `location_sharing` has no
        expiry, so a phone that stops reporting leaves a live setting and a
        stale coordinate behind it. Publishing that pair claims somebody is
        standing where they were hours ago.

        `for_friend` moves that cutoff out to a day — a friend's dot is allowed
        to sit at the last point we heard, because the viewer has another way to
        read it (the profile's last-seen stamp) and knows the person. It changes
        the window, never the rule.

        `for_friend` is also the check-in exemption. A friend sees this user
        whenever Ghost Mode is off; anybody else only while they are checked in
        (`is_checked_in`) — see the discovery note above. `max_age_s` overrides
        the freshness window alone, for a surface that wants fifteen minutes
        even between friends (a proximity alert is about *now*).

        `now` lets a caller sweeping many rows measure them all against one
        clock.
        """
        if not (self.is_sharing() and self.has_fresh_position(now, for_friend=for_friend, max_age_s=max_age_s)):
            return False
        return for_friend or self.is_checked_in(now)

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
    #: Likes, comments and reposts on your own Swifts. Default True, matching
    #: every switch above it: these are added after people already have
    #: accounts, and a default of False would ship a feature switched off for
    #: everyone who upgraded.
    notify_swift_activity = models.BooleanField(default=True)

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

    # ── The agreement ─────────────────────────────────────────────────────────
    # Which version of the Terms this account agreed to, and when. Two columns
    # rather than a boolean because "has agreed" is not a durable fact — the
    # text changes, and consent to the old text is not consent to the new one.
    # `users.terms.has_accepted` is the only thing that should read these; it
    # requires both to be set *and* the version to be current.
    #
    # Blank/null is the honest state for every account that predates this, and
    # it is what puts them in front of the gate on next launch rather than
    # locking them out — see `TermsAcceptView`.
    terms_version = models.CharField(max_length=20, blank=True, default='')
    terms_accepted_at = models.DateTimeField(null=True, blank=True)

    # ── Enforcement ───────────────────────────────────────────────────────────
    # A moderator's verdict on this account, and the reason a token stops
    # working. Deliberately not Django's `is_active`: that flag is checked only
    # at login, so flipping it leaves an already-issued access token working
    # until it expires. `users.middleware.SuspensionMiddleware` reads these on
    # every authenticated request instead, which is what makes a suspension
    # take effect at once.
    #
    # Null `suspended_until` on a suspended account means permanent — a ban.
    suspended_at = models.DateTimeField(null=True, blank=True)
    suspended_until = models.DateTimeField(null=True, blank=True)
    suspension_reason = models.CharField(max_length=200, blank=True, default='')

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

    # Old app builds still speak active/ghost/invisible, and older ones still
    # speak live/approximate/paused. Both collapse onto one boolean now, so the
    # bridge is lossy in the only direction that matters and says so:
    # `approximate` was a precision choice, and precision is no longer the
    # user's to make — it follows the relationship. An old client asking for it
    # gets visibility, which is the half of that value it can still have.
    LEGACY_PRIVACY_MODE_REVERSE = {
        'active': False,
        'ghost': False,
        'invisible': True,
        'live': False,
        'approximate': False,
        'paused': True,
    }

    @property
    def privacy_mode(self):
        return 'invisible' if self.ghost_mode else 'active'

    @property
    def location_sharing(self):
        """What an old client called this. Read-only; there is no third state
        left to report, so an Approximate build sees Live and is not wrong —
        strangers still see it approximately."""
        return 'paused' if self.ghost_mode else 'live'

    # ── Enforcement ───────────────────────────────────────────────────────────
    def is_suspended(self, now=None):
        """Whether a moderator's verdict is currently in force.

        A lapsed suspension answers False without needing anything to sweep the
        table — the row keeps its history, and the account simply works again
        the moment `suspended_until` passes. `suspended_until is None` on a
        suspended account is permanent, which is what a ban is here.
        """
        if self.suspended_at is None:
            return False
        if self.suspended_until is None:
            return True
        from django.utils import timezone

        return (now or timezone.now()) < self.suspended_until

    def suspension_detail(self, now=None):
        """What to tell a suspended account, or None when it is in good standing.

        The reason is included deliberately. An account told only "suspended"
        cannot tell a mistake from a verdict, and the appeal address is the
        thing App Review asks be reachable.
        """
        if not self.is_suspended(now):
            return None
        return {
            'detail': 'This account is suspended for breaking the Approx Terms of Use.',
            'code': 'account_suspended',
            'reason': self.suspension_reason or 'Violation of the Approx Terms of Use.',
            'until': self.suspended_until.isoformat() if self.suspended_until else None,
            'permanent': self.suspended_until is None,
        }

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
    """A Google or Apple identity attached to an Approx account.

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


class Device(models.Model):
    """One installation of the app, signed in to one account.

    Replaces the single `User.push_token`, which could only ever hold one
    device: signing in on a second phone overwrote the first's token, and the
    first silently stopped receiving notifications with nothing to show that
    anything had happened. Push now fans out across these rows (see
    `notifications.push.push_to_users`), and a new one appearing is what the
    "new sign-in" alert is raised from.

    Keyed on `install_id`, a value the app generates once per installation and
    keeps in device storage. Not the push token: that is reissued by Expo on
    its own schedule, so keying on it would make one phone look like a stream
    of new devices — and each reissue would fire a sign-in alert. The token is
    a column here, free to change without changing the identity of the row it
    sits on.
    """

    class Platform(models.TextChoices):
        IOS = 'ios', 'iOS'
        ANDROID = 'android', 'Android'
        UNKNOWN = 'unknown', 'Unknown'

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='devices')
    install_id = models.CharField(max_length=64)
    #: Blank whenever the device has no *current* token — push permission was
    #: never granted, or it signed out. The row stays either way, so the device
    #: keeps its identity and does not re-announce itself on the next sign-in.
    push_token = models.CharField(max_length=200, blank=True, default='')
    #: What the alert calls it — "Pixel 8". Supplied by the client and shown
    #: back to the account's owner, never to anyone else.
    name = models.CharField(max_length=120, blank=True, default='')
    platform = models.CharField(
        max_length=10, choices=Platform.choices, default=Platform.UNKNOWN,
    )
    last_seen = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-last_seen']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'install_id'], name='unique_install_per_user'),
        ]
        indexes = [
            # The push fan-out: every device belonging to these users.
            models.Index(fields=['user', 'push_token'], name='device_push_idx'),
        ]

    def __str__(self):
        return f'{self.name or self.install_id} → {self.user_id}'

    @property
    def label(self):
        """What to call this device in a sentence, when it has no name."""
        return self.name or self.get_platform_display()
