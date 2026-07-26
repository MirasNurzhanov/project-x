import math
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from django.db.models import Q
from users import visibility
from users.models import User
from friends.models import FriendRequest, HiRequest
from .serializers import LocationUpdateSerializer, NearbyUserSerializer
from .utils import haversine
from notifications.utils import notify_nearby_match
from encounters.utils import delete_old_encounters, record_encounter
from quests.utils import check_and_complete_quest


def _build_relationships(user, nearby_ids):
    relationships = {}

    for s, r in FriendRequest.objects.filter(
        status=FriendRequest.Status.ACCEPTED
    ).filter(
        Q(sender=user, receiver_id__in=nearby_ids) | Q(receiver=user, sender_id__in=nearby_ids)
    ).values_list('sender_id', 'receiver_id'):
        relationships[r if s == user.pk else s] = 'friend'

    for s, r in HiRequest.objects.filter(
        status=HiRequest.Status.ACCEPTED
    ).filter(
        Q(sender=user, receiver_id__in=nearby_ids) | Q(receiver=user, sender_id__in=nearby_ids)
    ).values_list('sender_id', 'receiver_id'):
        other = r if s == user.pk else s
        relationships.setdefault(other, 'connected')

    for rid in HiRequest.objects.filter(
        sender=user, status=HiRequest.Status.PENDING, receiver_id__in=nearby_ids
    ).values_list('receiver_id', flat=True):
        relationships.setdefault(rid, 'hi_sent')

    for sid in HiRequest.objects.filter(
        receiver=user, status=HiRequest.Status.PENDING, sender_id__in=nearby_ids
    ).values_list('sender_id', flat=True):
        relationships.setdefault(sid, 'hi_received')

    return relationships


DEFAULT_RADIUS_KM = 0.5
CONNECTED_RADIUS_KM = 1.0


def _get_relationship_ids(user, excluded_ids):
    friend_ids = set()
    for s, r in FriendRequest.objects.filter(
        status=FriendRequest.Status.ACCEPTED
    ).filter(
        Q(sender=user) | Q(receiver=user)
    ).values_list('sender_id', 'receiver_id'):
        other = r if s == user.pk else s
        if other not in excluded_ids:
            friend_ids.add(other)

    connected_ids = set()
    for s, r in HiRequest.objects.filter(
        status=HiRequest.Status.ACCEPTED
    ).filter(
        Q(sender=user) | Q(receiver=user)
    ).values_list('sender_id', 'receiver_id'):
        other = r if s == user.pk else s
        if other not in excluded_ids and other not in friend_ids:
            connected_ids.add(other)

    return friend_ids, connected_ids


def _trigger_nearby_notifications(user):
    if user.latitude is None or user.longitude is None:
        return
    if user.location_sharing == User.LocationSharing.PAUSED:
        return

    lat = float(user.latitude)
    lon = float(user.longitude)
    lat_delta = DEFAULT_RADIUS_KM / 111.0
    lon_delta = DEFAULT_RADIUS_KM / (111.0 * math.cos(math.radians(lat)))

    candidates = visibility.visible_users(user, on_map=True).filter(
        latitude__isnull=False,
        longitude__isnull=False,
        latitude__range=(lat - lat_delta, lat + lat_delta),
        longitude__range=(lon - lon_delta, lon + lon_delta),
    )

    if user.age_tier:
        candidates = candidates.filter(age_tier=user.age_tier)

    user_interest_ids = list(user.interests.values_list('id', flat=True))
    if user_interest_ids:
        # Interest matching only reaches people who are findable that way.
        candidates = candidates.filter(
            interests__in=user_interest_ids, discoverable_by_interest=True,
        ).distinct()

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
        # The client fuzzes too, but it is not trusted to: on Approximate the
        # precise value is discarded here, before anything is written.
        user.latitude, user.longitude = visibility.store_coordinates(
            user,
            serializer.validated_data['latitude'],
            serializer.validated_data['longitude'],
        )
        user.save(update_fields=['latitude', 'longitude', 'updated_at'])
        _trigger_nearby_notifications(user)
        check_and_complete_quest(user, 'update_location')
        return Response({'latitude': str(user.latitude), 'longitude': str(user.longitude)})


class NearbyUsersView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        delete_old_encounters()
        user = request.user

        if user.location_sharing == User.LocationSharing.PAUSED:
            return Response([])

        if user.latitude is None or user.longitude is None:
            return Response(
                {'detail': 'Your location is not set.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        lat = float(user.latitude)
        lon = float(user.longitude)

        blocked_ids = visibility.blocked_ids_for(user)

        friend_ids, connected_ids = _get_relationship_ids(user, blocked_ids | {user.pk})

        # Single chokepoint: blocks both ways and paused accounts are gone
        # before any of the distance tiers below get to see the rows.
        base_qs = (
            visibility.visible_users(user, on_map=True)
            .filter(latitude__isnull=False, longitude__isnull=False)
            .prefetch_related('interests')
        )

        # Friends: always visible, no distance limit
        friend_candidates = base_qs.filter(pk__in=friend_ids) if friend_ids else User.objects.none()

        # Connected: visible within CONNECTED_RADIUS_KM
        if connected_ids:
            conn_lat_delta = CONNECTED_RADIUS_KM / 111.0
            conn_lon_delta = CONNECTED_RADIUS_KM / (111.0 * math.cos(math.radians(lat)))
            connected_candidates = base_qs.filter(
                pk__in=connected_ids,
                latitude__range=(lat - conn_lat_delta, lat + conn_lat_delta),
                longitude__range=(lon - conn_lon_delta, lon + conn_lon_delta),
            )
        else:
            connected_candidates = User.objects.none()

        # Strangers: visible within DEFAULT_RADIUS_KM, filtered by age_tier and interests
        str_lat_delta = DEFAULT_RADIUS_KM / 111.0
        str_lon_delta = DEFAULT_RADIUS_KM / (111.0 * math.cos(math.radians(lat)))
        stranger_candidates = (
            base_qs
            .exclude(pk__in=friend_ids)
            .exclude(pk__in=connected_ids)
            .filter(
                latitude__range=(lat - str_lat_delta, lat + str_lat_delta),
                longitude__range=(lon - str_lon_delta, lon + str_lon_delta),
            )
        )
        if user.age_tier:
            stranger_candidates = stranger_candidates.filter(age_tier=user.age_tier)
        user_interest_ids = list(user.interests.values_list('id', flat=True))
        if user_interest_ids:
            # Strangers are surfaced through shared interests, so opting out of
            # interest discovery keeps you off strangers' maps.
            stranger_candidates = stranger_candidates.filter(
                interests__in=user_interest_ids, discoverable_by_interest=True,
            ).distinct()

        distances = {}
        nearby = []
        seen = set()

        # Distances are measured against whatever position is stored. For an
        # Approximate user that is already the snapped point, so the number is
        # coarse by construction rather than by being withheld.
        for candidate in friend_candidates:
            distances[candidate.pk] = round(
                haversine(lat, lon, float(candidate.latitude), float(candidate.longitude)), 2)
            nearby.append(candidate)
            seen.add(candidate.pk)

        for candidate in connected_candidates:
            if candidate.pk in seen:
                continue
            dist = round(haversine(lat, lon, float(candidate.latitude), float(candidate.longitude)), 2)
            if dist <= CONNECTED_RADIUS_KM:
                distances[candidate.pk] = dist
                nearby.append(candidate)
                seen.add(candidate.pk)

        for candidate in stranger_candidates:
            if candidate.pk in seen:
                continue
            dist = round(haversine(lat, lon, float(candidate.latitude), float(candidate.longitude)), 2)
            if dist <= DEFAULT_RADIUS_KM:
                distances[candidate.pk] = dist
                nearby.append(candidate)
                seen.add(candidate.pk)

        nearby.sort(key=lambda u: distances.get(u.pk, float('inf')))

        nearby_ids = [u.pk for u in nearby]
        relationships = _build_relationships(user, nearby_ids)
        serializer = NearbyUserSerializer(
            nearby,
            many=True,
            context={
                'request': request,
                'distances': distances,
                'relationships': relationships,
                'my_interest_ids': set(user_interest_ids),
            },
        )
        return Response(serializer.data)
