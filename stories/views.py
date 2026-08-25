import math
from decimal import ROUND_HALF_EVEN, Decimal

from django.db.models import F, Q, Value
from django.db.models.functions import Greatest
from django.utils import timezone
from moderation import services as moderation_services
from moderation.models import ContentReport
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser

from notifications.utils import notify_story_reaction
from users import visibility
from . import visibility as story_visibility
from .models import Story, StoryReaction
from .serializers import (
    SocialStorySerializer,
    StorySerializer,
    StoryCreateSerializer,
    StoryReactionSerializer,
)
from proximity.utils import haversine

NEARBY_RADIUS_KM = 0.5


def _blocked_ids_for(user):
    """User ids involved in a block with `user`, either direction — their
    stories are invisible to each other. Thin alias so there is exactly one
    definition of "blocked" in the codebase."""
    return visibility.blocked_ids_for(user)


def _visible_story(user, pk):
    """A story `user` is allowed to open and act on, or None.

    Blocked authors and stories hidden by their author's Stories setting are
    indistinguishable from a story that does not exist. Location sharing is
    *not* consulted: a friend may read and react to a story whose author is
    paused. What that author's setting withholds is the pin, and only the
    pin — see `stories.visibility` for the two questions and why they are not
    the same one.
    """
    return (
        Story.objects
        .exclude(user_id__in=_blocked_ids_for(user))
        .filter(story_visibility.readable_q(user))
        .filter(pk=pk)
        .select_related('user')
        .first()
    )


#: Story coordinates are stored at the model's 6 decimal places, ~11cm.
_STORY_COORD_STEP = Decimal('0.000001')


def _fit_coord(value):
    """Round a coordinate to the precision the Story columns actually declare.

    `visibility.store_coordinates` works at the User model's 8 places; Story
    keeps 6, and handing a Decimal more places than the column allows is a
    validation error at save rather than a silent round. Rounding at ~11cm
    cannot meaningfully undo a 500m snap, so this is safe to apply to a fuzzed
    coordinate as well as a precise one.
    """
    return Decimal(value).quantize(_STORY_COORD_STEP, rounding=ROUND_HALF_EVEN)


def _position_for(user, serializer):
    """The position fields to persist for a story `user` is posting.

    The whole of the coupling between stories and location lives here, and it
    is one way round: what the author's location sharing says decides what gets
    stored, and posting is never refused over it.

    Paused        nothing. Not the coordinate the client sent, not the one
                  their private map queries travel with, not the last one we
                  held before they paused, not the centre of their city. A
                  story posted while paused is locationless, and the place name
                  goes with the coordinate because it is one - a story pinned
                  to "Blue Bottle, 8th Ave" is a story that says where you are.

    Approximate   the point snapped to its ~500m cell, by the same function the
                  location endpoint uses. Otherwise the setting is undone by
                  using the app: someone whose dot is fuzzed hands back the
                  exact point by posting, under their own name, at a place they
                  chose to stop at.

    Live          what they sent.

    Enforced here rather than trusted to the client, because the client is not
    the one making the promise. A build that keeps sending coordinates while
    paused - an old one, a modified one - gets them dropped.
    """
    # `is_sharing`, not `is_on_map`: this is the author's own act, and a story
    # carries the coordinate it was posted at rather than a claim about where
    # its author is now. Freshness governs the *user marker*, which does make
    # that claim — see users.visibility for the split.
    if not user.is_sharing():
        return {'latitude': None, 'longitude': None, 'place_name': ''}

    lat = serializer.validated_data.get('latitude')
    lon = serializer.validated_data.get('longitude')
    if lat is None or lon is None:
        # A sharing author who posted without one. Locationless is a normal
        # kind of story, so this is not an error; it simply is not a pin.
        return {'latitude': None, 'longitude': None}

    lat, lon = visibility.store_coordinates(user, lat, lon)
    return {'latitude': _fit_coord(lat), 'longitude': _fit_coord(lon)}


ARCHIVE_PAGE_MAX = 100


def _page_params(request, default_limit=30):
    """Parse ?limit=&offset= with sane bounds for the archive endpoints."""
    try:
        limit = min(max(int(request.query_params.get('limit', default_limit)), 1), ARCHIVE_PAGE_MAX)
    except (TypeError, ValueError):
        limit = default_limit
    try:
        offset = max(int(request.query_params.get('offset', 0)), 0)
    except (TypeError, ValueError):
        offset = 0
    return limit, offset


def _adjust_likes(story, delta):
    """Move a story's like counter by `delta`, in the database.

    `F()` rather than `likes_count=story.likes_count + delta`, which is what
    this used to be. That form reads the count into Python and writes back
    read-value-plus-one, so two people liking the same story at the same moment
    both read N and both write N+1 — and one of the likes is silently lost. A
    story on a busy map is exactly where simultaneous likes happen.

    The floor is applied in SQL for the same reason: a Python `max(0, ...)`
    would be computed from the same stale read it is meant to protect.
    """
    if delta >= 0:
        new_value = F('likes_count') + delta
    else:
        new_value = Greatest(F('likes_count') + delta, Value(0))
    Story.objects.filter(pk=story.pk).update(likes_count=new_value)


def _bounding_box(lat, lon, radius_km):
    lat_delta = radius_km / 111.0
    lon_delta = radius_km / (111.0 * math.cos(math.radians(lat)))
    return lat - lat_delta, lat + lat_delta, lon - lon_delta, lon + lon_delta


class CreateStoryView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request):
        user = request.user
        serializer = StoryCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        # Blocked text never reaches here - `ScreenedTextMixin` refused it
        # during `is_valid()` above. What survives is either clean or flagged,
        # and a flagged story is stored quarantined rather than published.
        verdict = serializer.moderation_verdict
        story = serializer.save(
            user=user,
            moderation=moderation_services.moderation_for(verdict),
            **_position_for(user, serializer),
        )
        if verdict.flagged:
            moderation_services.record_automated_flag(
                story, ContentReport.Target.STORY, verdict, author=user,
            )
        return Response(
            StorySerializer(story, context={'request': request}).data,
            status=status.HTTP_201_CREATED,
        )


def nearby_stories(user, lat, lon, request):
    """Story *pins* near (`lat`, `lon`) that `user` may see, serialized.

    The map surface, and the narrow one: see `stories.visibility.placed_q`. A
    story that is missing from here has not necessarily gone anywhere - a
    friend's locationless story, or one whose author has paused since posting,
    is still theirs to read from /api/stories/friends/.

    The anchor is a parameter for the same reason it is in
    proximity.views.nearby_users: it comes either from the position stored for
    a user who shares their location, or from a coordinate a paused user's
    device supplied for this one request. Read-only either way — nothing here
    writes, so the private path cannot leave a trace by asking.
    """
    min_lat, max_lat, min_lon, max_lon = _bounding_box(lat, lon, NEARBY_RADIUS_KM)

    now = timezone.now()
    candidates = Story.objects.filter(
        expires_at__gt=now,
    ).filter(
        Q(user=user) |
        Q(
            latitude__gte=min_lat,
            latitude__lte=max_lat,
            longitude__gte=min_lon,
            longitude__lte=max_lon,
        )
    ).exclude(
        user_id__in=_blocked_ids_for(user)
    ).filter(
        # The map's rule, which is the strict one: friendship and the author's
        # Stories setting *and* a position to draw *and* an author who is on
        # the map right now. `readable_q` on its own - what the chat surface
        # uses - would put a paused author's old pin back on the map.
        story_visibility.placed_q(user)
    ).select_related('user').prefetch_related('reactions')

    stories = [
        s for s in candidates
        if s.user_id == user.pk or haversine(lat, lon, float(s.latitude), float(s.longitude)) <= NEARBY_RADIUS_KM
    ]

    serializer = StorySerializer(stories, many=True, context={'request': request})
    return serializer.data


class NearbyStoriesView(APIView):
    """Stories near the position stored for the caller.

    Kept as its own route because the story map is reachable outside the
    composite map endpoint. A paused caller has no stored anchor, so they get
    an empty list here and use /api/map/nearby/query/ instead.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user
        # Paused is an empty list: a working state with nothing to show. No
        # position yet is the shared `location_not_available` code, so a client
        # gets the same answer here as it does from the proximity endpoints and
        # needs one branch, not three.
        if not user.is_sharing():
            return Response([])
        if user.latitude is None or user.longitude is None:
            from proximity.views import location_not_available
            return location_not_available()
        return Response(nearby_stories(
            user, float(user.latitude), float(user.longitude), request,
        ))


#: Ceiling on the friend feed. Friends x live stories is small by construction
#: - a story lives 24 hours - but the response is a list this endpoint does not
#: get to bound, so it bounds it.
FRIEND_FEED_MAX = 200


class FriendStoriesView(APIView):
    """Live stories from the people this viewer is allowed to read, newest
    first. The surface behind the story rings in Chats.

    No position is involved on either side. Not the author's - a friend who has
    paused still posts stories, and their friends still see them. Not the
    viewer's - "has my friend posted today" is not a question about where
    anybody is standing, and answering it used to require a location fix
    because the only story feed that existed was the map's.

    That is the whole reason this endpoint exists rather than a flag on
    /nearby/: the two feeds share a table and nothing else. This one asks
    `readable_q`; the map asks `placed_q`. The viewer's own stories come
    through as well, so the client can render them the way every stories UI
    does, and so that a paused author can see that their story posted.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user
        stories = list(
            Story.objects
            .filter(expires_at__gt=timezone.now())
            .exclude(user_id__in=_blocked_ids_for(user))
            .filter(story_visibility.readable_q(user))
            .select_related('user')
            .prefetch_related('reactions')
            .order_by('-created_at')[:FRIEND_FEED_MAX]
        )
        serializer = SocialStorySerializer(
            stories, many=True, context={'request': request},
        )
        return Response({'count': len(stories), 'results': serializer.data})


class ReactToStoryView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        story = _visible_story(request.user, pk)
        if story is None:
            return Response({'detail': 'Story not found.'}, status=status.HTTP_404_NOT_FOUND)

        if story.expires_at <= timezone.now():
            return Response({'detail': 'Story has expired.'}, status=status.HTTP_410_GONE)

        user = request.user
        # The 500m rule applies to a story the viewer could have reached *on
        # the map* - that is the surface it was written for, where reacting
        # means "I am here too". A story that is not a pin for this viewer is
        # not answerable by standing anywhere, and gating it on position would
        # do two wrong things at once: lock a friend out of a story the chat
        # surface just showed them, and turn the 403 into a probe that reports
        # whether they are near a paused author's last known position.
        # `is_placed_for`, not `can_show_on_map`: `_visible_story` has already
        # established that this viewer may read this story, and the only thing
        # left to ask is whether it is also a pin. The fuller check would spend
        # two queries re-deriving the relationship to say the same thing.
        if story_visibility.is_placed_for(user, story):
            # Reacting to a pin takes standing near it, so it needs a position
            # like anything else - and the two ways of not having one are worth
            # telling apart. Paused is a setting the user can change; no fix
            # yet resolves on its own.
            if not user.is_sharing():
                from proximity.views import CODE_SHARING_PAUSED
                return Response(
                    {
                        'detail': 'Location sharing is paused.',
                        'code': CODE_SHARING_PAUSED,
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if user.latitude is None or user.longitude is None:
                from proximity.views import location_not_available
                return location_not_available()

            distance_km = haversine(
                float(user.latitude), float(user.longitude),
                float(story.latitude), float(story.longitude),
            )
            if distance_km > NEARBY_RADIUS_KM:
                return Response({'detail': 'You must be within 500m to react.'}, status=status.HTTP_403_FORBIDDEN)

        serializer = StoryReactionSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        existing = StoryReaction.objects.filter(user=user, story=story).first()
        if existing:
            existing.delete()
            _adjust_likes(story, -1)
            return Response({'detail': 'Reaction removed.', 'user_reaction': None}, status=status.HTTP_200_OK)

        StoryReaction.objects.create(user=user, story=story, reaction=StoryReaction.ReactionType.LIKE)
        _adjust_likes(story, 1)
        notify_story_reaction(story, user)
        return Response(
            {'detail': 'Reaction added.', 'user_reaction': StoryReaction.ReactionType.LIKE},
            status=status.HTTP_201_CREATED,
        )


class MyStoriesView(APIView):
    """The user's own story archive — includes expired stories (the map only
    shows live ones, but every posted story stays here until deleted)."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        limit, offset = _page_params(request)
        qs = Story.objects.filter(user=request.user).select_related('user').prefetch_related('reactions')
        count = qs.count()
        page = list(qs[offset:offset + limit])
        serializer = StorySerializer(page, many=True, context={'request': request})
        return Response({'count': count, 'results': serializer.data})

    def delete(self, request):
        """Clear the archive: permanently delete every story the user posted."""
        Story.objects.filter(user=request.user).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class StarredStoriesView(APIView):
    """The user's starred stories — a favorites shortlist over the full
    "Mine" archive. There is no cross-user bookmarking: `is_starred` lives
    directly on `Story`, so this can only ever be a subset of the caller's
    own posts (see `stories.models.Story.is_starred`)."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        limit, offset = _page_params(request)
        qs = Story.objects.filter(
            user=request.user, is_starred=True,
        ).select_related('user').prefetch_related('reactions')
        count = qs.count()
        page = list(qs[offset:offset + limit])
        serializer = StorySerializer(page, many=True, context={'request': request})
        return Response({'count': count, 'results': serializer.data})

    def delete(self, request):
        """Unstar everything. The stories themselves are untouched."""
        Story.objects.filter(user=request.user, is_starred=True).update(is_starred=False)
        return Response(status=status.HTTP_204_NO_CONTENT)


class StarStoryView(APIView):
    """POST = star one of your own stories, DELETE = unstar it. Only the
    author may star a story — unlike the old bookmark feature this replaces,
    there is nothing here for anyone else's posts to hook into."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        try:
            story = Story.objects.get(pk=pk, user=request.user)
        except Story.DoesNotExist:
            return Response({'detail': 'Story not found or not yours.'}, status=status.HTTP_404_NOT_FOUND)
        if not story.is_starred:
            story.is_starred = True
            story.save(update_fields=['is_starred'])
        return Response({'detail': 'Starred.'}, status=status.HTTP_200_OK)

    def delete(self, request, pk):
        Story.objects.filter(pk=pk, user=request.user).update(is_starred=False)
        return Response(status=status.HTTP_204_NO_CONTENT)


class DeleteStoryView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, pk):
        try:
            story = Story.objects.get(pk=pk, user=request.user)
        except Story.DoesNotExist:
            return Response({'detail': 'Story not found or not yours.'}, status=status.HTTP_404_NOT_FOUND)
        story.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
