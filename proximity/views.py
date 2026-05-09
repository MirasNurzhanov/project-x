import math
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from django.db.models import Q
from users.models import User
from friends.models import BlockedUser
from .serializers import LocationUpdateSerializer, NearbyUserSerializer
from .utils import haversine
from notifications.utils import notify_nearby_match
from encounters.utils import delete_old_encounters, record_encounter

DEFAULT_RADIUS_KM = 5
MAX_RADIUS_KM = 50


def _trigger_nearby_notifications(user):
    if user.latitude is None or user.longitude is None:
        return

    lat = float(user.latitude)
    lon = float(user.longitude)
    lat_delta = DEFAULT_RADIUS_KM / 111.0
    lon_delta = DEFAULT_RADIUS_KM / (111.0 * math.cos(math.radians(lat)))

    blocked_others = BlockedUser.objects.filter(blocker=user).values('blocked_id')
    blocked_me = BlockedUser.objects.filter(blocked=user).values('blocker_id')

    candidates = (
        User.objects.filter(
            latitude__isnull=False,
            longitude__isnull=False,
            is_discoverable=True,
            ghost_mode=False,
            age_tier=user.age_tier,
            latitude__range=(lat - lat_delta, lat + lat_delta),
            longitude__range=(lon - lon_delta, lon + lon_delta),
        )
        .exclude(pk=user.pk)
        .exclude(pk__in=blocked_others)
        .exclude(pk__in=blocked_me)
    )

    user_interest_ids = list(user.interests.values_list('id', flat=True))
    if user_interest_ids:
        candidates = candidates.filter(interests__in=user_interest_ids).distinct()

    for candidate in candidates:
        dist = round(haversine(lat, lon, float(candidate.latitude), float(candidate.longitude)), 2)
        if dist <= DEFAULT_RADIUS_KM:
            notify_nearby_match(user, candidate, dist)
            record_encounter(user, candidate, dist)


class UpdateLocationView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request):
        serializer = LocationUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = request.user
        user.latitude = serializer.validated_data['latitude']
        user.longitude = serializer.validated_data['longitude']
        user.save(update_fields=['latitude', 'longitude', 'updated_at'])
        _trigger_nearby_notifications(user)
        return Response({'latitude': str(user.latitude), 'longitude': str(user.longitude)})


class NearbyUsersView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        delete_old_encounters()
        user = request.user

        if user.latitude is None or user.longitude is None:
            return Response(
                {'detail': 'Your location is not set.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            radius_km = min(float(request.query_params.get('radius', DEFAULT_RADIUS_KM)), MAX_RADIUS_KM)
        except ValueError:
            radius_km = DEFAULT_RADIUS_KM

        lat = float(user.latitude)
        lon = float(user.longitude)

        # Cheap bounding-box pre-filter before computing exact distances
        lat_delta = radius_km / 111.0
        lon_delta = radius_km / (111.0 * math.cos(math.radians(lat)))

        blocked_others = BlockedUser.objects.filter(blocker=user).values('blocked_id')
        blocked_me = BlockedUser.objects.filter(blocked=user).values('blocker_id')

        candidates = (
            User.objects.filter(
                latitude__isnull=False,
                longitude__isnull=False,
                is_discoverable=True,
                ghost_mode=False,
                age_tier=user.age_tier,
                latitude__range=(lat - lat_delta, lat + lat_delta),
                longitude__range=(lon - lon_delta, lon + lon_delta),
            )
            .exclude(pk=user.pk)
            .exclude(pk__in=blocked_others)
            .exclude(pk__in=blocked_me)
            .prefetch_related('interests')
        )

        user_interest_ids = list(user.interests.values_list('id', flat=True))
        if not user_interest_ids:
            return Response([])
        candidates = candidates.filter(interests__in=user_interest_ids).distinct()

        # Precise Haversine pass
        distances = {}
        nearby = []
        for candidate in candidates:
            dist = round(
                haversine(lat, lon, float(candidate.latitude), float(candidate.longitude)), 2
            )
            if dist <= radius_km:
                distances[candidate.pk] = dist
                nearby.append(candidate)

        nearby.sort(key=lambda u: distances[u.pk])

        serializer = NearbyUserSerializer(
            nearby,
            many=True,
            context={'request': request, 'distances': distances},
        )
        return Response(serializer.data)
