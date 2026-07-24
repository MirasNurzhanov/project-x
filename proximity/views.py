import math
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from django.db.models import Q
from users.models import User
from friends.models import BlockedUser, FriendRequest, HiRequest
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
    if user.privacy_mode == 'invisible':
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
            privacy_mode__in=['active', 'ghost'],
            latitude__range=(lat - lat_delta, lat + lat_delta),
            longitude__range=(lon - lon_delta, lon + lon_delta),
        )
        .exclude(pk=user.pk)
        .exclude(pk__in=blocked_others)
        .exclude(pk__in=blocked_me)
    )

    if user.age_tier:
        candidates = candidates.filter(age_tier=user.age_tier)

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
        check_and_complete_quest(user, 'update_location')
        return Response({'latitude': str(user.latitude), 'longitude': str(user.longitude)})


class NearbyUsersView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        delete_old_encounters()
        user = request.user

        if user.privacy_mode == 'invisible':
            return Response([])

        if user.latitude is None or user.longitude is None:
            return Response(
                {'detail': 'Your location is not set.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        lat = float(user.latitude)
        lon = float(user.longitude)

        blocked_others_ids = set(BlockedUser.objects.filter(blocker=user).values_list('blocked_id', flat=True))
        blocked_me_ids = set(BlockedUser.objects.filter(blocked=user).values_list('blocker_id', flat=True))
        blocked_ids = blocked_others_ids | blocked_me_ids

        friend_ids, connected_ids = _get_relationship_ids(user, blocked_ids | {user.pk})

        base_qs = (
            User.objects.filter(
                latitude__isnull=False,
                longitude__isnull=False,
                privacy_mode__in=['active', 'ghost'],
            )
            .exclude(pk=user.pk)
            .exclude(pk__in=blocked_ids)
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
            stranger_candidates = stranger_candidates.filter(interests__in=user_interest_ids).distinct()

        distances = {}
        nearby = []
        seen = set()

        for candidate in friend_candidates:
            dist = round(haversine(lat, lon, float(candidate.latitude), float(candidate.longitude)), 2)
            if candidate.privacy_mode == 'active':
                distances[candidate.pk] = dist
            nearby.append(candidate)
            seen.add(candidate.pk)

        for candidate in connected_candidates:
            if candidate.pk in seen:
                continue
            dist = round(haversine(lat, lon, float(candidate.latitude), float(candidate.longitude)), 2)
            if dist <= CONNECTED_RADIUS_KM:
                if candidate.privacy_mode == 'active':
                    distances[candidate.pk] = dist
                nearby.append(candidate)
                seen.add(candidate.pk)

        for candidate in stranger_candidates:
            if candidate.pk in seen:
                continue
            dist = round(haversine(lat, lon, float(candidate.latitude), float(candidate.longitude)), 2)
            if dist <= DEFAULT_RADIUS_KM:
                if candidate.privacy_mode == 'active':
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
