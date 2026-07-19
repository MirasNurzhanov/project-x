import math
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser

from .models import Story, StoryReaction
from .serializers import StorySerializer, StoryCreateSerializer, StoryReactionSerializer
from proximity.utils import haversine

NEARBY_RADIUS_KM = 0.5


def _bounding_box(lat, lon, radius_km):
    lat_delta = radius_km / 111.0
    lon_delta = radius_km / (111.0 * math.cos(math.radians(lat)))
    return lat - lat_delta, lat + lat_delta, lon - lon_delta, lon + lon_delta


class CreateStoryView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request):
        print('[Story] received image:', request.FILES.get('image'))
        serializer = StoryCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        story = serializer.save(user=request.user)
        return Response(StorySerializer(story, context={'request': request}).data, status=status.HTTP_201_CREATED)


class NearbyStoriesView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user
        if user.latitude is None or user.longitude is None:
            return Response({'detail': 'Your location is not set.'}, status=status.HTTP_400_BAD_REQUEST)

        lat = float(user.latitude)
        lon = float(user.longitude)
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
        ).select_related('user').prefetch_related('reactions')

        stories = [
            s for s in candidates
            if s.user_id == user.pk or haversine(lat, lon, float(s.latitude), float(s.longitude)) <= NEARBY_RADIUS_KM
        ]

        serializer = StorySerializer(stories, many=True, context={'request': request})
        return Response(serializer.data)


class ReactToStoryView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        try:
            story = Story.objects.get(pk=pk)
        except Story.DoesNotExist:
            return Response({'detail': 'Story not found.'}, status=status.HTTP_404_NOT_FOUND)

        if story.expires_at <= timezone.now():
            return Response({'detail': 'Story has expired.'}, status=status.HTTP_410_GONE)

        user = request.user
        if user.latitude is None or user.longitude is None:
            return Response({'detail': 'Your location is not set.'}, status=status.HTTP_400_BAD_REQUEST)

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
                    Story.objects.filter(pk=story.pk).update(likes_count=max(0, story.likes_count - 1))
                return Response({'detail': 'Reaction removed.', 'user_reaction': None}, status=status.HTTP_200_OK)
            else:
                old_type = existing.reaction
                existing.reaction = reaction_type
                existing.save(update_fields=['reaction'])
                # Adjust likes_count
                if old_type == StoryReaction.ReactionType.LIKE:
                    Story.objects.filter(pk=story.pk).update(likes_count=max(0, story.likes_count - 1))
                elif reaction_type == StoryReaction.ReactionType.LIKE:
                    Story.objects.filter(pk=story.pk).update(likes_count=story.likes_count + 1)
                return Response({'detail': 'Reaction updated.', 'user_reaction': reaction_type}, status=status.HTTP_200_OK)
        else:
            StoryReaction.objects.create(user=user, story=story, reaction=reaction_type)
            if reaction_type == StoryReaction.ReactionType.LIKE:
                Story.objects.filter(pk=story.pk).update(likes_count=story.likes_count + 1)
            return Response({'detail': 'Reaction added.', 'user_reaction': reaction_type}, status=status.HTTP_201_CREATED)


class DeleteStoryView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, pk):
        try:
            story = Story.objects.get(pk=pk, user=request.user)
        except Story.DoesNotExist:
            return Response({'detail': 'Story not found or not yours.'}, status=status.HTTP_404_NOT_FOUND)
        story.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
