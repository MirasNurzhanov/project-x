import math
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from users.models import User
from .serializers import LocationUpdateSerializer, NearbyUserSerializer
from .utils import haversine
from notifications.utils import notify_nearby_match
from encounters.utils import record_encounter

DEFAULT_RADIUS_KM = 5
MAX_RADIUS_KM = 50


def _trigger_nearby_notifications(user):
    if user.latitude is None or user.longitude is None:
        return

    lat = float(user.latitude)
    lon = float(user.longitude)
    lat_delta = DEFAULT_RADIUS_KM / 111.0
    lon_delta = DEFAULT_RADIUS_KM / (111.0 * math.cos(math.radians(lat)))

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
        user = request.user

        print(f"\n[NEARBY DEBUG] Request from user: {user.email} (pk={user.pk})")
        print(f"[NEARBY DEBUG] User location: lat={user.latitude}, lon={user.longitude}")
        print(f"[NEARBY DEBUG] User age_tier: '{user.age_tier}'")
        print(f"[NEARBY DEBUG] is_discoverable={user.is_discoverable}, ghost_mode={user.ghost_mode}")

        if user.latitude is None or user.longitude is None:
            print("[NEARBY DEBUG] EARLY EXIT: requesting user has no location set")
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
        print(f"[NEARBY DEBUG] Search radius: {radius_km} km")

        # Stage 1 — bounding box
        lat_delta = radius_km / 111.0
        lon_delta = radius_km / (111.0 * math.cos(math.radians(lat)))
        print(f"[NEARBY DEBUG] Bounding box: lat ±{lat_delta:.6f}, lon ±{lon_delta:.6f}")

        bbox_candidates = (
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
            .prefetch_related('interests')
        )
        bbox_list = list(bbox_candidates)
        print(f"[NEARBY DEBUG] Stage 1 (bounding box): {len(bbox_list)} user(s)")
        for u in bbox_list:
            print(f"  - {u.email} (pk={u.pk}) lat={u.latitude} lon={u.longitude} age_tier='{u.age_tier}' discoverable={u.is_discoverable} ghost={u.ghost_mode}")

        if not bbox_list:
            all_others = User.objects.exclude(pk=user.pk)
            print(f"[NEARBY DEBUG] Total other users in DB: {all_others.count()}")
            for u in all_others:
                print(f"  - {u.email}: lat={u.latitude} lon={u.longitude} age_tier='{u.age_tier}' discoverable={u.is_discoverable} ghost={u.ghost_mode}")

        # Stage 2 — shared interests
        user_interest_ids = list(user.interests.values_list('id', flat=True))
        print(f"[NEARBY DEBUG] Requesting user interest IDs: {user_interest_ids}")

        if user_interest_ids:
            after_interests = [
                u for u in bbox_list
                if u.interests.filter(id__in=user_interest_ids).exists()
            ]
            print(f"[NEARBY DEBUG] Stage 2 (shared interests): {len(after_interests)} user(s)")
            for u in after_interests:
                shared = list(u.interests.filter(id__in=user_interest_ids).values_list('name', flat=True))
                print(f"  - {u.email}: shared interests = {shared}")
        else:
            after_interests = bbox_list
            print("[NEARBY DEBUG] Stage 2 skipped: requesting user has no interests set")

        # Stage 3 — precise Haversine
        distances = {}
        nearby = []
        print(f"[NEARBY DEBUG] Stage 3 (Haversine ≤ {radius_km} km):")
        for candidate in after_interests:
            dist = round(
                haversine(lat, lon, float(candidate.latitude), float(candidate.longitude)), 2
            )
            print(f"  - {candidate.email}: distance = {dist} km  {'PASS' if dist <= radius_km else 'FAIL'}")
            if dist <= radius_km:
                distances[candidate.pk] = dist
                nearby.append(candidate)

        print(f"[NEARBY DEBUG] Final result: {len(nearby)} nearby user(s)\n")
        nearby.sort(key=lambda u: distances[u.pk])

        serializer = NearbyUserSerializer(
            nearby,
            many=True,
            context={'request': request, 'distances': distances},
        )
        return Response(serializer.data)
