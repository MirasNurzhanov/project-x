import math
from django.db.models import F, Value
from django.db.models.functions import Greatest
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser

from notifications.utils import notify_story_reaction
from users import visibility
from .models import SavedStory, Story, StoryReaction
from .serializers import StorySerializer, StoryCreateSerializer, StoryReactionSerializer
from proximity.utils import haversine

NEARBY_RADIUS_KM = 0.5


def _blocked_ids_for(user):
    """User ids involved in a block with `user`, either direction — their
    stories are invisible to each other. Thin alias so there is exactly one
    definition of "blocked" in the codebase."""
    return visibility.blocked_ids_for(user)

def _visible_story(user, pk):
    """A story `user` is allowed to act on, or None. Blocked authors and
    stories hidden by their author's Stories setting are indistinguishable
    from a story that does not exist."""
    return (
        Story.objects
        .exclude(user_id__in=_blocked_ids_for(user))
        .filter(visibility.visible_field_q(user, 'stories'))
        .filter(pk=pk)
        .first()
    )


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
        serializer = StoryCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        story = serializer.save(user=request.user)
        return Response(StorySerializer(story, context={'request': request}).data, status=status.HTTP_201_CREATED)


def nearby_stories(user, lat, lon, request):
    """Stories placed near (`lat`, `lon`) that `user` may see, serialized.

    The anchor is a parameter for the same reason it is in
    proximity.views.nearby_users: it comes either from the position stored for
    a user who shares their location, or from a coordinate a paused user's
    device supplied for this one request. Read-only either way — nothing here
    writes, so the private path cannot leave a trace by asking.
    """
    min_lat, max_lat, min_lon, max_lon = _bounding_box(lat, lon, NEARBY_RADIUS_KM)

    from django.db.models import Q
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
        # Whose stories may reach this viewer at all — the author's
        # Stories setting and private-account gate, applied in SQL.
        visibility.visible_field_q(user, 'stories')
    ).select_related('user').prefetch_related('reactions')

    stories = [
        s for s in candidates
        if s.user_id == user.pk or haversine(lat, lon, float(s.latitude), float(s.longitude)) <= NEARBY_RADIUS_KM
    ]

    saved_ids = set(
        SavedStory.objects.filter(
            user=user, story_id__in=[s.pk for s in stories],
        ).values_list('story_id', flat=True)
    )
    serializer = StorySerializer(
        stories, many=True, context={'request': request, 'saved_ids': saved_ids},
    )
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
        if not user.is_on_map():
            return Response([])
        if user.latitude is None or user.longitude is None:
            from proximity.views import location_not_available
            return location_not_available()
        return Response(nearby_stories(
            user, float(user.latitude), float(user.longitude), request,
        ))


class ReactToStoryView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        story = _visible_story(request.user, pk)
        if story is None:
            return Response({'detail': 'Story not found.'}, status=status.HTTP_404_NOT_FOUND)

        if story.expires_at <= timezone.now():
            return Response({'detail': 'Story has expired.'}, status=status.HTTP_410_GONE)

        user = request.user
        # Reacting takes standing near the story, so it needs a position like
        # anything else — and the two ways of not having one are worth telling
        # apart here too. Paused is a setting the user can change; no fix yet
        # resolves on its own.
        if not user.is_on_map():
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

        reaction_type = serializer.validated_data['reaction']
        existing = StoryReaction.objects.filter(user=user, story=story).first()

        if existing:
            was_like = existing.reaction == StoryReaction.ReactionType.LIKE
            if existing.reaction == reaction_type:
                # Toggle off
                existing.delete()
                if was_like:
                    _adjust_likes(story, -1)
                return Response({'detail': 'Reaction removed.', 'user_reaction': None}, status=status.HTTP_200_OK)
            else:
                old_type = existing.reaction
                existing.reaction = reaction_type
                existing.save(update_fields=['reaction'])
                # Adjust likes_count
                if old_type == StoryReaction.ReactionType.LIKE:
                    _adjust_likes(story, -1)
                elif reaction_type == StoryReaction.ReactionType.LIKE:
                    _adjust_likes(story, 1)
                notify_story_reaction(story, user, reaction_type)
                return Response({'detail': 'Reaction updated.', 'user_reaction': reaction_type}, status=status.HTTP_200_OK)
        else:
            StoryReaction.objects.create(user=user, story=story, reaction=reaction_type)
            if reaction_type == StoryReaction.ReactionType.LIKE:
                _adjust_likes(story, 1)
            notify_story_reaction(story, user, reaction_type)
            return Response({'detail': 'Reaction added.', 'user_reaction': reaction_type}, status=status.HTTP_201_CREATED)


class MyStoriesView(APIView):
    """The user's own story archive — includes expired stories (the map only
    shows live ones, but every posted story stays here until deleted)."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        limit, offset = _page_params(request)
        qs = Story.objects.filter(user=request.user).select_related('user').prefetch_related('reactions')
        count = qs.count()
        page = list(qs[offset:offset + limit])
        saved_ids = set(
            SavedStory.objects.filter(
                user=request.user, story_id__in=[s.pk for s in page],
            ).values_list('story_id', flat=True)
        )
        serializer = StorySerializer(
            page, many=True, context={'request': request, 'saved_ids': saved_ids},
        )
        return Response({'count': count, 'results': serializer.data})

    def delete(self, request):
        """Clear the archive: permanently delete every story the user posted."""
        Story.objects.filter(user=request.user).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class SavedStoriesView(APIView):
    """Stories the user bookmarked, most recently saved first. Stories deleted
    by their author disappear automatically (FK cascade)."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        limit, offset = _page_params(request)
        # A bookmark is not a permanent grant: if the author has since
        # restricted who can see their stories, the saved copy goes too.
        qs = SavedStory.objects.filter(user=request.user).exclude(
            story__user_id__in=_blocked_ids_for(request.user)
        ).filter(
            visibility.visible_field_q(request.user, 'stories', prefix='story__user')
        ).select_related('story__user').prefetch_related('story__reactions')
        count = qs.count()
        page = list(qs[offset:offset + limit])
        # Everything on this page is by definition saved — skip the per-story
        # existence checks the serializer would otherwise run.
        saved_ids = {saved.story_id for saved in page}
        results = []
        for saved in page:
            data = StorySerializer(
                saved.story, context={'request': request, 'saved_ids': saved_ids},
            ).data
            data['saved_at'] = saved.created_at.isoformat()
            results.append(data)
        return Response({'count': count, 'results': results})

    def delete(self, request):
        """Clear all bookmarks. Original stories are untouched."""
        SavedStory.objects.filter(user=request.user).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class SaveStoryView(APIView):
    """POST = bookmark a story, DELETE = remove the bookmark. Removing a
    bookmark never touches the original story."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        story = _visible_story(request.user, pk)
        if story is None:
            return Response({'detail': 'Story not found.'}, status=status.HTTP_404_NOT_FOUND)
        if story.user_id == request.user.pk:
            return Response({'detail': 'Your own stories are archived automatically.'},
                            status=status.HTTP_400_BAD_REQUEST)
        _, created = SavedStory.objects.get_or_create(user=request.user, story=story)
        return Response({'detail': 'Saved.'},
                        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    def delete(self, request, pk):
        SavedStory.objects.filter(user=request.user, story_id=pk).delete()
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
