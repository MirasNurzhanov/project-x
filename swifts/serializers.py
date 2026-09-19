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

from . import capture, tagging, visibility as swift_visibility
from .models import SwiftComment, SwiftTrack, SwiftVideo


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


def _clamped(value, low, high, fallback):
    """A float inside [low, high], or `fallback` if it was not a number at all.

    Used by the sticker validator. Clamping rather than refusing is deliberate
    there — see `SwiftCreateSerializer.validate_stickers` — but a value that is
    not a number is not a near-miss to be rescued, so it falls back rather than
    clamping something meaningless.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if number != number:  # NaN: fails every comparison, so clamping misses it
        return fallback
    return round(min(high, max(low, number)), 4)


#: What `SwiftSerializer.review_state` can say. Strings rather than the
#: `moderation` column's own values on purpose: the column answers a question
#: about enforcement ("has this been taken down"), and the app is asking a
#: question about progress ("can I show this to my friends yet"). Mapping
#: between them here means the client never learns the enforcement vocabulary,
#: and a new internal state does not become an app release.
REVIEW_PROCESSING = 'processing'
REVIEW_REVIEWING = 'reviewing'
REVIEW_PUBLISHED = 'published'
REVIEW_NEEDS_REVIEW = 'needs_review'


def review_state_for(swift):
    """The creator-facing state of one Swift.

    Ordered by which fact is most specific. A Swift that is still uploading is
    `processing` whatever its moderation column says, because the column has
    not been decided yet and telling somebody their video is "under review"
    while it is in fact still transcoding is simply false.
    """
    from moderation.reasons import Moderation

    if swift.status != SwiftVideo.Status.READY:
        return REVIEW_PROCESSING
    if swift.moderation == Moderation.PENDING:
        return REVIEW_REVIEWING
    if swift.moderation == Moderation.OK:
        return REVIEW_PUBLISHED
    return REVIEW_NEEDS_REVIEW


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
    #: Whether this viewer has it on their shortlist. Private to them —
    #: there is no matching count, because a saved-by number would leak a
    #: list nobody is meant to be able to read.
    viewer_saved = serializers.SerializerMethodField()
    viewer_viewed = serializers.SerializerMethodField()
    is_mine = serializers.SerializerMethodField()
    review_state = serializers.SerializerMethodField()
    hashtags = serializers.SerializerMethodField()
    mentions = serializers.SerializerMethodField()

    class Meta:
        model = SwiftVideo
        fields = [
            'id', 'author', 'caption',
            'video_url', 'thumbnail_url',
            'duration_ms', 'width', 'height', 'aspect_ratio',
            'status', 'review_state',
            'audio_title', 'audio_artist', 'audio_artwork_url',
            'audio_preview_url', 'uses_original_audio',
            'track', 'music_volume', 'music_start_ms',
            'trim_start_ms', 'trim_end_ms', 'overlay_text', 'overlay_position',
            'original_volume',
            'speed_percent', 'rotation_deg', 'effect', 'effect_intensity',
            'stickers',
            'hashtags', 'mentions',
            'visibility', 'allow_comments', 'allow_reposts',
            'like_count', 'comment_count', 'repost_count', 'view_count',
            'viewer_liked', 'viewer_reposted', 'viewer_saved', 'viewer_viewed',
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

    def get_viewer_saved(self, obj):
        from .models import SavedSwift
        return self._flag(obj, 'saved_ids', SavedSwift)

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
            'audio_url': obj.track.audio_url if obj.track_id and obj.track else '',
            'duration_ms': obj.track.duration_ms if obj.track_id and obj.track else 0,
        }

    def get_hashtags(self, obj):
        """This Swift's tags, in the order they were written.

        Reads the prefetched relation rather than querying: every list view
        that reaches this serializer prefetches `hashtags__hashtag`, and one
        that forgets pays a query per row — the N+1 shape
        `core/test_query_budgets.py` exists to catch.
        """
        return [
            {'name': link.hashtag.name, 'display': link.hashtag.display}
            for link in obj.hashtags.all()
        ]

    def get_mentions(self, obj):
        """Who this Swift names.

        `username` comes off the mention row, not off the related user: it is
        what the caption actually says, and a client highlighting the span in
        the text needs the written form rather than the current one. `user_id`
        is what a tap opens, and that one does follow a rename.
        """
        return [
            {'user_id': mention.user_id, 'username': mention.username}
            for mention in obj.mentions.all()
        ]

    def get_author(self, obj):
        return _author_payload(obj.author)

    def get_is_mine(self, obj):
        viewer = self._viewer()
        return bool(viewer and obj.author_id == viewer.pk)

    def get_review_state(self, obj):
        """What the composer shows the creator while a Swift is being screened.

        Four words, and deliberately only four. Everything the pipeline
        actually knows — which categories fired, how confident the model was,
        how many frames it looked at — stays on `SwiftModerationJob` where a
        moderator can read it. Publishing a confidence score to the author
        would be handing anybody who wants to get material past this filter a
        free tuning signal, one upload at a time, and it would do it while
        telling somebody who did nothing wrong a number they cannot act on.

        Only the author is told anything. Every other viewer sees
        `published`, because every other viewer can only ever be looking at a
        Swift that *is* published: `live_q` has already excluded the rest, and
        a state field that could say something else to a stranger would be a
        second place for that rule to be got wrong.

        `needs_review` is worded on the client as a neutral sentence — see
        `aura-mobile/components/swifts/SwiftReviewNotice.tsx`. A quarantine is
        not a finding, and accusing somebody before a human has looked is both
        wrong and, for the false positives this policy accepts by design,
        wrong about a person who did nothing.
        """
        viewer = self._viewer()
        if viewer is None or obj.author_id != getattr(viewer, 'pk', None):
            return REVIEW_PUBLISHED
        return review_state_for(obj)

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
        if viewer.ghost_mode and obj.author_id != viewer.pk:
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
    thumbnail = serializers.FileField(required=False, allow_null=True)
    idempotency_key = serializers.UUIDField(required=False, allow_null=True, write_only=True)

    #: The two pieces of a Swift a person writes. The video itself is not
    #: screened by anything here and this module does not pretend otherwise —
    #: see `SwiftVideo.moderation` and the note in `swifts/media.py`.
    screened_fields = ('caption', 'place_name')

    #: The music bed, by catalogue id.
    #:
    #: Only ever an *active* track. The queryset is the enforcement: a
    #: withdrawn or unvetted row cannot be attached however the id was
    #: obtained, and DRF answers "invalid pk" rather than this module having to
    #: remember to check.
    track = serializers.PrimaryKeyRelatedField(
        queryset=SwiftTrack.objects.filter(is_active=True),
        required=False, allow_null=True,
    )

    #: Tags and mentions the creator added *outside* the caption.
    #:
    #: Write-only and merged with whatever the caption itself says — see
    #: `swifts.tagging`, which does the parsing and owns the merge order. They
    #: are popped in `create()` rather than passed to the model: neither is a
    #: column, both are rows that can only be written once the Swift has a pk.
    hashtags = serializers.ListField(
        child=serializers.CharField(max_length=tagging.MAX_HASHTAG_CHARS + 1),
        required=False, write_only=True, max_length=tagging.MAX_HASHTAGS,
    )
    mentions = serializers.ListField(
        child=serializers.CharField(max_length=151),
        required=False, write_only=True, max_length=tagging.MAX_MENTIONS,
    )

    class Meta:
        model = SwiftVideo
        fields = [
            'video', 'thumbnail', 'idempotency_key',
            'duration_ms', 'width', 'height', 'status',
            'caption',
            'uses_original_audio',
            'latitude', 'longitude', 'place_name', 'location_shared',
            'visibility', 'allow_comments', 'allow_reposts',
            'trim_start_ms', 'trim_end_ms',
            'original_volume',
            'overlay_text', 'overlay_position',
            'stickers',
            'speed_percent', 'rotation_deg', 'effect', 'effect_intensity',
            'track', 'music_volume', 'music_start_ms',
            'hashtags', 'mentions',
        ]

    def validate_video(self, value):
        from core.uploads import validate_video
        return validate_video(value)

    def validate_effect(self, value):
        """Only a treatment some player actually knows how to draw.

        A Swift stored with a typo'd key would play unfiltered for everybody,
        silently, and the creator would never be told — which is why an unknown
        key is a 400 rather than something to ignore. Blank is the ordinary
        case and means untouched.
        """
        value = (value or '').strip()
        if value and value not in capture.EFFECT_KEYS:
            raise serializers.ValidationError('Unknown effect.')
        return value

    def validate_stickers(self, value):
        """Rebuild the list from scratch, keeping only what a player reads.

        Rebuilt rather than checked-and-passed-through, and that is the point:
        `stickers` is a JSON column with no schema of its own, so anything this
        method lets past is stored verbatim and handed to every viewer. Copying
        five known keys out of each entry means a payload carrying a sixth —
        whatever it is — cannot reach the database at all.

        Positions are fractions of the frame rather than pixels, so the same
        arrangement lands in the same place on a phone that is not the one it
        was made on. Out-of-range numbers are clamped rather than refused: a
        sticker dragged a hair past the edge is a normal thing to do and losing
        the publish over it would be absurd.
        """
        if not isinstance(value, list):
            raise serializers.ValidationError('Stickers must be a list.')
        if len(value) > capture.MAX_STICKERS:
            raise serializers.ValidationError(
                f'At most {capture.MAX_STICKERS} stickers.')

        cleaned = []
        for entry in value:
            if not isinstance(entry, dict):
                raise serializers.ValidationError('Each sticker must be an object.')
            emoji = str(entry.get('emoji') or '')
            if not emoji:
                raise serializers.ValidationError('A sticker needs an emoji.')
            if len(emoji) > capture.MAX_STICKER_CHARS:
                # Long enough for any ZWJ sequence; past that it is text being
                # smuggled into a field the client draws at 40pt.
                raise serializers.ValidationError('That is not an emoji.')
            cleaned.append({
                'emoji': emoji,
                'x': _clamped(entry.get('x'), 0.0, 1.0, 0.5),
                'y': _clamped(entry.get('y'), 0.0, 1.0, 0.5),
                'scale': _clamped(entry.get('scale'), 0.2, 5.0, 1.0),
                'rotation': _clamped(entry.get('rotation'), -180.0, 180.0, 0.0),
            })
        return cleaned

    def create(self, validated_data):
        """Save the row, holding back the two lists that are not columns.

        `hashtags` and `mentions` describe rows that cannot exist until the
        Swift has a primary key, so they are lifted off here and left on the
        serializer for the view to apply once the transaction has a Swift to
        attach them to — see `SwiftListCreateView._create`.

        The music credit is flattened at the same moment and for the reason
        `SwiftVideo.track` documents: the FK says which row, the strings say
        what this Swift claims, and a track later withdrawn from the catalogue
        must not strip the credit from everything that used it.
        """
        self.hashtag_names = validated_data.pop('hashtags', [])
        self.mention_handles = validated_data.pop('mentions', [])

        track = validated_data.get('track')
        if track is not None:
            validated_data.setdefault('audio_title', track.title)
            validated_data.setdefault('audio_artist', track.artist)
            validated_data.setdefault('audio_artwork_url', track.artwork_url)
        return super().create(validated_data)

    def validate_thumbnail(self, value):
        from core.uploads import validate_image
        return validate_image(value) if value is not None else None

    def validate_duration_ms(self, value):
        from .media import MAX_DURATION_MS, MIN_DURATION_MS

        if value and value > MAX_DURATION_MS:
            raise serializers.ValidationError(
                f'A Swift can be at most {MAX_DURATION_MS // 1000} seconds.')
        if value and value < MIN_DURATION_MS:
            raise serializers.ValidationError('That video is too short.')
        return value

    def validate(self, attrs):
        """Screen the text, then check the fields that have to agree.

        `super().validate(attrs)` is the important line and it was missing.
        `ScreenedTextMixin` does its work *in* `validate`, so a subclass that
        defines one and does not chain silently switches the filter off —
        `screened_fields` above still reads as a declaration that the caption is
        screened, `is_valid()` still passes, and the only symptom is that
        prohibited captions publish. Every other screened serializer in the
        project (`StoryCreateSerializer`, `SwiftCommentCreateSerializer`,
        `users`) declares `screened_fields` and no `validate`, which is why this
        was the only one affected.

        What follows is the pair of consistency rules this method was written
        for:

        `location_shared` cannot be switched on over nothing.

        A Swift flagged as having a shareable location but carrying no
        coordinate would publish `location_available: true`, draw the reveal
        button, and then answer the press with a 404 — which reads to the
        viewer as a bug and to a curious one as a signal worth probing.

        Only enforced when the client actually asked for it. A creator whose
        location sharing is paused sends coordinates that `_position_for` will
        drop, and refusing the whole post over that would be telling them their
        privacy setting is an error.
        """
        attrs = super().validate(attrs)

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

            # The ceiling applies to what a viewer *watches*, not to how much
            # source was kept, and those stop being the same number the moment
            # a speed is set. A 90-second trim at 150% is a 60-second Swift and
            # is fine; a 45-second trim at 50% is a 90-second Swift and is not.
            # `duration_ms` is already the played length — the client sends it
            # that way — so this is the one place the source length has to be
            # converted before it can be compared.
            kept = capture.played_ms(end - start, attrs.get('speed_percent', 100))
            if kept > MAX_DURATION_MS:
                raise serializers.ValidationError({
                    'trim_end_ms':
                        f'A Swift can be at most {MAX_DURATION_MS // 1000} seconds.',
                })
            if kept < MIN_DURATION_MS:
                raise serializers.ValidationError({
                    'trim_end_ms': 'That trim is too short.',
                })

        # A track and no volume for it is a silent bed nobody asked for, and a
        # bed starting past the end of its own track is silence too. Neither is
        # worth refusing a publish over, so both are corrected rather than
        # rejected — but a `music_start_ms` with no track at all is a client
        # bug worth surfacing while it is still cheap to find.
        if attrs.get('track') is None and attrs.get('music_start_ms'):
            raise serializers.ValidationError({
                'music_start_ms': 'There is no track for this to be an offset into.',
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

    #: The same two write-only lists `SwiftCreateSerializer` takes, for the
    #: same reason: a caption edit changes half of what the Swift is tagged
    #: with, and the creator may also want to change the other half. Without
    #: them an edit could only ever *add* what the new caption says and never
    #: remove a tag added through the field.
    hashtags = serializers.ListField(
        child=serializers.CharField(max_length=tagging.MAX_HASHTAG_CHARS + 1),
        required=False, write_only=True, max_length=tagging.MAX_HASHTAGS,
    )
    mentions = serializers.ListField(
        child=serializers.CharField(max_length=151),
        required=False, write_only=True, max_length=tagging.MAX_MENTIONS,
    )

    class Meta:
        model = SwiftVideo
        fields = [
            'caption', 'visibility', 'allow_comments', 'allow_reposts',
            'location_shared',
            'hashtags', 'mentions',
        ]

    def update(self, instance, validated_data):
        """Save the row, holding back the two lists that are not columns.

        Same split as `SwiftCreateSerializer.create`, and the view applies them
        the same way — see `SwiftDetailView.patch`.
        """
        self.hashtag_names = validated_data.pop('hashtags', [])
        self.mention_handles = validated_data.pop('mentions', [])
        return super().update(instance, validated_data)

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
    def _precise_for_viewer(self, obj):
        """Whether this viewer is entitled to the point as recorded.

        Same rule as a live position: a friend of the author sees the precise
        coordinate, everybody else sees the ~500m cell. It used to be decided
        before the row was written, by the author's own Approximate setting;
        that setting is gone and precision follows the relationship now.

        Unauthenticated falls to the coarse answer, which is the safe
        direction — this serializer is reached through one guarded view, so
        that branch should be unreachable rather than merely unlikely.
        """
        from users import visibility as user_visibility

        viewer = getattr(self.context.get('request'), 'user', None)
        if viewer is None or not viewer.is_authenticated:
            return False
        return user_visibility.is_friend(viewer, obj.author)

    def _shown(self, obj):
        from users import visibility as user_visibility

        if obj.latitude is None or obj.longitude is None:
            return None, None
        if self._precise_for_viewer(obj):
            return obj.latitude, obj.longitude
        # Snapped against the *author*, so the grid is the same one their live
        # position is fuzzed onto and the two agree.
        return user_visibility.snap_coordinates(obj.author, obj.latitude, obj.longitude)

    def get_latitude(self, obj):
        lat, _ = self._shown(obj)
        return None if lat is None else float(lat)

    def get_longitude(self, obj):
        _, lon = self._shown(obj)
        return None if lon is None else float(lon)

    def get_precision(self, obj):
        return 'exact' if self._precise_for_viewer(obj) else 'approximate'


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
