"""What a Swift looks like on the wire.

The load-bearing property of this module is a negative one: **no serializer
reachable from the feed or the detail endpoint declares a coordinate field.**

That is enforced structurally rather than by subtraction. `stories` solves the
same problem the other way round — `StorySerializer` has the coordinates and
`SocialStorySerializer` removes them — which works, but leaves a base class in
the codebase that emits a position and a one-line mistake (using the parent by
accident, adding a field to the wrong `Meta`) between that and a leak.

Here the read serializer never has them in the first place. `SwiftVideo` keeps
latitude and longitude in columns that no `ModelSerializer` in this file lists,
and the one class that can emit a coordinate — `SwiftLocationSerializer` — is a
plain `Serializer` with hand-written fields, so it cannot inherit model fields,
cannot be widened by `fields = '__all__'`, and is imported by exactly one view.

`swifts/test_location_privacy.py` asserts this rather than trusting it.
"""
from moderation.drf import ScreenedTextMixin
from rest_framework import serializers

from . import visibility as swift_visibility
from .models import SwiftComment, SwiftVideo


def _author_payload(user):
    """The author block, shaped like the one `stories` emits.

    Kept identical on purpose: the app already has an author-avatar component
    fed by story payloads, and a second shape for the same idea would mean a
    second branch in it.
    """
    return {
        'id': user.id,
        'username': user.username,
        'profile_picture': user.profile_picture.url if user.profile_picture else None,
    }


class SwiftSerializer(serializers.ModelSerializer):
    """A Swift as the feed and the detail endpoint send it.

    One serializer for both surfaces, deliberately. They differ in *which* rows
    they return, not in what a row contains, and two classes here would be two
    places for a coordinate to appear.

    Note what is absent and stays absent: `latitude`, `longitude`,
    `place_name`, and `media_id`. The first three are the location rule. The
    fourth is not a privacy matter but there is no reason to publish the CDN's
    internal handle to every reader.
    """

    author = serializers.SerializerMethodField()
    aspect_ratio = serializers.FloatField(read_only=True)
    # Computed from the render parameters every time rather than stored, so an
    # edit takes effect the moment it is saved and there is no second copy of
    # the truth to fall out of step. See `swifts.media`.
    video_url = serializers.SerializerMethodField()
    thumbnail_url = serializers.SerializerMethodField()
    track = serializers.SerializerMethodField()
    location_available = serializers.SerializerMethodField()
    viewer_liked = serializers.SerializerMethodField()
    viewer_reposted = serializers.SerializerMethodField()
    viewer_viewed = serializers.SerializerMethodField()
    is_mine = serializers.SerializerMethodField()

    class Meta:
        model = SwiftVideo
        fields = [
            'id', 'author', 'caption',
            'video_url', 'thumbnail_url',
            'duration_ms', 'width', 'height', 'aspect_ratio',
            'status',
            'audio_title', 'audio_artist', 'audio_artwork_url',
            'audio_preview_url', 'uses_original_audio',
            'track',
            'trim_start_ms', 'trim_end_ms', 'overlay_text', 'overlay_position',
            'visibility', 'allow_comments', 'allow_reposts',
            'like_count', 'comment_count', 'repost_count', 'view_count',
            'viewer_liked', 'viewer_reposted', 'viewer_viewed',
            'location_available',
            'is_mine', 'created_at', 'updated_at',
        ]
        read_only_fields = fields

    # ── Viewer-relative flags ────────────────────────────────────────────────
    # Each reads a set the view put in the context, and falls back to a query
    # only for a single-object response. A list endpoint that let these fall
    # through would issue three queries per Swift — the N+1 shape this codebase
    # has been bitten by more than once, and the reason
    # `core/test_query_budgets.py` exists.
    def _viewer(self):
        request = self.context.get('request')
        return getattr(request, 'user', None) if request else None

    def _flag(self, obj, context_key, model):
        ids = self.context.get(context_key)
        if ids is not None:
            return obj.pk in ids
        viewer = self._viewer()
        if viewer is None or not viewer.is_authenticated:
            return False
        field = 'viewer' if model.__name__ == 'SwiftView' else 'user'
        return model.objects.filter(swift=obj, **{field: viewer}).exists()

    def get_viewer_liked(self, obj):
        from .models import SwiftLike
        return self._flag(obj, 'liked_ids', SwiftLike)

    def get_viewer_reposted(self, obj):
        from .models import SwiftRepost
        return self._flag(obj, 'reposted_ids', SwiftRepost)

    def get_viewer_viewed(self, obj):
        from .models import SwiftView
        return self._flag(obj, 'viewed_ids', SwiftView)

    def get_video_url(self, obj):
        """The uploaded file, or the legacy `video_url` for a row that predates
        it — see `swifts.media.playback_url`."""
        from . import media

        return media.playback_url(obj)

    def get_thumbnail_url(self, obj):
        """The captured poster frame, or the legacy `thumbnail_url` — see
        `swifts.media.poster_url`."""
        from . import media

        return media.poster_url(obj)

    def get_track(self, obj):
        """The music credit.

        Read from the flat columns, not from the `track` relation, and that is
        deliberate: the strings are the attribution as it stood when the Swift
        was published, and a track withdrawn from the catalogue must not strip
        the credit from every Swift that used it. `id` is best-effort — it is
        gone once the row is — and everything a viewer reads survives.
        """
        if not obj.audio_title:
            return None
        return {
            'id': obj.track_id,
            'title': obj.audio_title,
            'artist': obj.audio_artist,
            'artwork_url': obj.audio_artwork_url,
        }

    def get_author(self, obj):
        return _author_payload(obj.author)

    def get_is_mine(self, obj):
        viewer = self._viewer()
        return bool(viewer and obj.author_id == viewer.pk)

    def get_location_available(self, obj):
        """Whether this viewer would be shown a location-reveal button.

        Goes through `swifts.visibility`, not through
        `SwiftVideo.location_available` — the model property answers "does this
        Swift have a shareable location", which is a fact about the Swift, and
        publishing that is a leak in its own right. See the note on
        `location_available_for`.

        `watchable_verified` is the performance half, and it is the same
        arrangement `SocialStorySerializer.get_place_name` uses: every row that
        reaches this serializer from a list or detail view came out of a
        queryset already filtered by `watchable_q`, so re-deriving the viewer's
        relationship to the author here would be three queries per row to
        re-answer a question SQL has already answered. The context key is set
        only by views that did that filtering; without it the full gate runs,
        so a new caller that forgets is slow rather than wrong.
        """
        viewer = self._viewer()
        if viewer is None or not viewer.is_authenticated:
            return False
        if self.context.get('watchable_verified'):
            return obj.location_shared and obj.has_location
        return swift_visibility.location_available_for(viewer, obj)


class SwiftCreateSerializer(ScreenedTextMixin, serializers.ModelSerializer):
    """Posting a Swift.

    Note what is *not* here: `author`, every counter, `moderation`,
    `deleted_at`, and the final say over the position.

    Latitude and longitude are accepted but are a **proposal**, not the stored
    value — `swifts.views._position_for` runs them through the creator's own
    location-sharing mode, which may snap them to a ~500m grid or drop them
    entirely. Same arrangement as `StoryCreateSerializer`, and for the same
    reason: the client is not the party making the promise.
    """

    #: `blank=True` on the model is for the legacy Cloudinary-era rows that
    #: never populate this column at all — a new post always carries the file.
    video = serializers.FileField(required=True)

    #: The two pieces of a Swift a person writes. The video itself is not
    #: screened by anything here and this module does not pretend otherwise —
    #: see `SwiftVideo.moderation` and the note in `swifts/media.py`.
    screened_fields = ('caption', 'place_name')

    class Meta:
        model = SwiftVideo
        fields = [
            'video', 'thumbnail',
            'duration_ms', 'width', 'height', 'status',
            'caption',
            'uses_original_audio',
            'latitude', 'longitude', 'place_name', 'location_shared',
            'visibility', 'allow_comments', 'allow_reposts',
            'trim_start_ms', 'trim_end_ms',
            'original_volume',
            'overlay_text', 'overlay_position',
        ]

    def validate_video(self, value):
        from .media import MAX_UPLOAD_BYTES

        if value.size > MAX_UPLOAD_BYTES:
            raise serializers.ValidationError(
                f'That file is too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)}MB).')
        return value

    def validate_duration_ms(self, value):
        from .media import MAX_DURATION_MS, MIN_DURATION_MS

        if value and value > MAX_DURATION_MS:
            raise serializers.ValidationError(
                f'A Swift can be at most {MAX_DURATION_MS // 1000} seconds.')
        if value and value < MIN_DURATION_MS:
            raise serializers.ValidationError('That video is too short.')
        return value

    def validate(self, attrs):
        """`location_shared` cannot be switched on over nothing.

        A Swift flagged as having a shareable location but carrying no
        coordinate would publish `location_available: true`, draw the reveal
        button, and then answer the press with a 404 — which reads to the
        viewer as a bug and to a curious one as a signal worth probing.

        Only enforced when the client actually asked for it. A creator whose
        location sharing is paused sends coordinates that `_position_for` will
        drop, and refusing the whole post over that would be telling them their
        privacy setting is an error.
        """
        shared = attrs.get('location_shared', False)
        has_pair = attrs.get('latitude') is not None and attrs.get('longitude') is not None
        if shared and not has_pair:
            raise serializers.ValidationError({
                'location_shared': 'A location must be provided to share one.',
            })

        # A trim that ends before it starts describes an empty clip — nothing
        # plays it, but nothing renders it either any more (playback just
        # seeks within the range), so this is the one place it gets caught.
        start = attrs.get('trim_start_ms', 0) or 0
        end = attrs.get('trim_end_ms')
        if end is not None and end <= start:
            raise serializers.ValidationError({
                'trim_end_ms': 'The end of the trim must come after the start.',
            })
        if end is not None:
            from .media import MAX_DURATION_MS, MIN_DURATION_MS

            kept = end - start
            if kept > MAX_DURATION_MS:
                raise serializers.ValidationError({
                    'trim_end_ms':
                        f'A Swift can be at most {MAX_DURATION_MS // 1000} seconds.',
                })
            if kept < MIN_DURATION_MS:
                raise serializers.ValidationError({
                    'trim_end_ms': 'That trim is too short.',
                })
        return attrs


class SwiftUpdateSerializer(serializers.ModelSerializer):
    """What a creator may change after posting.

    Small on purpose. The media is immutable — a Swift whose video can be
    swapped after it has been watched, liked and reposted is a different Swift
    wearing the same id, and every engagement on it becomes a lie.

    `location_shared` is here and is the important one: it is what makes
    attaching a location reversible. Setting it False closes the door for
    everyone who has not already been through it. It cannot re-open a door over
    a Swift that never had coordinates, for the reason argued in
    `SwiftCreateSerializer.validate`.
    """

    class Meta:
        model = SwiftVideo
        fields = [
            'caption', 'visibility', 'allow_comments', 'allow_reposts',
            'location_shared',
        ]

    def validate_location_shared(self, value):
        if value and not self.instance.has_location:
            raise serializers.ValidationError(
                'This Swift has no location to share.'
            )
        return value


class SwiftLocationSerializer(serializers.Serializer):
    """**The only serializer in this codebase that emits a Swift's coordinate.**

    A plain `Serializer` rather than a `ModelSerializer`, and that is the
    point: it cannot acquire a field from the model by accident, cannot be
    widened by `fields = '__all__'`, and every value it sends is written out
    below by hand. It is imported by exactly one view, which is guarded by
    `swifts.visibility.reveal_denial`.

    If this class ever appears in a second import site, that is the thing to
    look at first.
    """

    latitude = serializers.SerializerMethodField()
    longitude = serializers.SerializerMethodField()
    place_name = serializers.CharField(read_only=True)
    #: When the position was captured — the Swift's own creation time. Sent
    #: because "where" without "when" invites the reader to assume "now", and
    #: a Swift can be watched months after it was made.
    captured_at = serializers.DateTimeField(source='created_at', read_only=True)
    #: Whether what is being returned is the exact point the creator's phone
    #: reported, or the ~500m cell it was snapped to before storage. The client
    #: draws a pin for one and a circle for the other; drawing a pin over a
    #: fuzzed coordinate would state a precision that was never granted.
    precision = serializers.SerializerMethodField()

    # DRF's DecimalField stringifies by default (COERCE_DECIMAL_TO_STRING),
    # which Fabric's typed view-manager props on Android refuse outright —
    # AIRMapMarker throws "cannot be cast from String to double" rather than
    # coercing the way the old bridge did. Floats here, for the same reason
    # `StorySerializer` does it, so a revealed location can go straight onto a
    # map without the client normalising it first.
    def get_latitude(self, obj):
        return None if obj.latitude is None else float(obj.latitude)

    def get_longitude(self, obj):
        return None if obj.longitude is None else float(obj.longitude)

    def get_precision(self, obj):
        from users.models import User

        return (
            'approximate'
            if obj.author.location_sharing == User.LocationSharing.APPROXIMATE
            else 'exact'
        )


class SwiftCommentSerializer(serializers.ModelSerializer):
    author = serializers.SerializerMethodField()
    is_mine = serializers.SerializerMethodField()
    reply_count = serializers.SerializerMethodField()

    class Meta:
        model = SwiftComment
        fields = ['id', 'author', 'parent', 'text', 'reply_count', 'is_mine', 'created_at']
        read_only_fields = fields

    def get_author(self, obj):
        return _author_payload(obj.author)

    def get_is_mine(self, obj):
        request = self.context.get('request')
        return bool(request and obj.author_id == request.user.pk)

    def get_reply_count(self, obj):
        """Reads a map the view built, or 0.

        Never a `.count()` — that is one query per comment on a thread page,
        which is the same N+1 the viewer flags above avoid.
        """
        counts = self.context.get('reply_counts') or {}
        return counts.get(obj.pk, 0)


class SwiftCommentCreateSerializer(ScreenedTextMixin, serializers.ModelSerializer):
    screened_fields = ('text',)

    class Meta:
        model = SwiftComment
        fields = ['text', 'parent']

    def validate_text(self, value):
        text = (value or '').strip()
        if not text:
            raise serializers.ValidationError('A comment cannot be empty.')
        return text


class SwiftViewReportSerializer(serializers.Serializer):
    """What the client may say about a watch it is reporting.

    Deliberately tiny, and deliberately *not* trusted for anything that grants
    access. `watch_ms` and `completed` are engagement signal; the fact that
    counts for the location reveal is the existence of the `SwiftView` row this
    server writes, which no field here can conjure. See `swifts.visibility`.
    """

    watch_ms = serializers.IntegerField(required=False, min_value=0, default=0)
    completed = serializers.BooleanField(required=False, default=False)


class SwiftLocationRevealAuditSerializer(serializers.ModelSerializer):
    """One row of the creator's "who looked at where this was" list."""

    viewer = serializers.SerializerMethodField()

    class Meta:
        from .models import SwiftLocationReveal

        model = SwiftLocationReveal
        fields = ['viewer', 'revealed_at', 'reveal_count']
        read_only_fields = fields

    def get_viewer(self, obj):
        return _author_payload(obj.viewer)


class SwiftReportSerializer(serializers.ModelSerializer):
    """Reporting a Swift.

    `reason` is required and comes from a fixed list. Free text alone would be
    unroutable — a moderation queue is triaged by category long before anybody
    reads a sentence — and it is the reason code that decides urgency.
    """

    class Meta:
        from .models import SwiftReport

        model = SwiftReport
        fields = ['reason', 'note']

    def validate_note(self, value):
        return (value or '').strip()


class SwiftTrackSerializer(serializers.ModelSerializer):
    """A catalogue track as the picker shows it.

    `licence` and `source` are absent: they are the operator's record of why
    this track is safe to offer, not something a creator needs or should be
    handed. `attribution` *is* present, because when a licence requires a
    credit line the app has to be able to show it.
    """

    class Meta:
        from .models import SwiftTrack

        model = SwiftTrack
        fields = [
            'id', 'title', 'artist', 'genre',
            'audio_url', 'artwork_url', 'duration_ms',
            'attribution', 'use_count',
        ]
        read_only_fields = fields
