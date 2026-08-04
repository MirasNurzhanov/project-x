import math
from concurrent.futures import ThreadPoolExecutor

from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from django.db import close_old_connections
from django.db.models import Q
from django.utils import timezone
from regions import places
from users import visibility
from users.models import User
from friends.models import FriendRequest, HiRequest
from .serializers import LocationUpdateSerializer, NearbyUserSerializer
from .utils import haversine
from notifications.utils import notify_nearby_matches, notify_zone_music_entry
from encounters.utils import delete_old_encounters_throttled, record_encounters


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

    # Collected first, then written once. The per-candidate version issued about
    # ten queries each on the endpoint every phone reports to every thirty
    # seconds; these two calls are a fixed cost however many people are in range.
    matches = []
    for candidate in candidates:
        dist = round(haversine(lat, lon, float(candidate.latitude), float(candidate.longitude)), 2)
        if dist <= DEFAULT_RADIUS_KM:
            matches.append((candidate, dist))

    notify_nearby_matches(user, matches)
    record_encounters(user, matches)


def _trigger_zone_music_alerts(user):
    """`user` has just reported a position. Whose music are they now inside?

    Friends only, in both roles. A stranger's zone music is not something they
    asked to be told about, and a stranger walking past is not news to the
    owner either — the map already shows both of them to each other.

    Only the person who *moved* is evaluated, which is enough to cover both
    directions: when you walk into a stationary friend's zone this runs for
    you, and when they walk into yours it runs for them.
    """
    if user.latitude is None or user.longitude is None:
        return
    if user.location_sharing == User.LocationSharing.PAUSED:
        return

    lat = float(user.latitude)
    lon = float(user.longitude)

    friend_ids, _ = _get_relationship_ids(user, visibility.blocked_ids_for(user) | {user.pk})
    if not friend_ids:
        return

    # Not filtered on `zone_active`. That flag is about the zone drawn on the
    # map, and using it here tied someone's music to whether they wanted a
    # circle around themselves. Whether there is a track to be in earshot of is
    # settled by `has_zone_track` in `notify_zone_music_entry`, which is a
    # property across three columns and so cannot be a filter here.
    owners = User.objects.filter(
        pk__in=friend_ids,
        latitude__isnull=False,
        longitude__isnull=False,
    ).exclude(location_sharing=User.LocationSharing.PAUSED)

    for owner in owners:
        # A region zone is a city border, not a distance, and its owner did not
        # choose it as a listening range — inheriting it here would alert on
        # every friend anywhere in town. Separating the music zone is exactly
        # how someone in region mode opts into these.
        if owner.effective_zone_mode == User.ZoneMode.REGION and not owner.music_zone_separate:
            continue
        metres = haversine(lat, lon, float(owner.latitude), float(owner.longitude)) * 1000
        if metres <= owner.music_radius_m:
            notify_zone_music_entry(user, owner, metres)


#: Speed bucket edges in m/s. 2.2 m/s ≈ 8 km/h, above a walk; 8.0 m/s ≈ 29 km/h,
#: which separates a cyclist from a vehicle well enough for a visual hint. The
#: client applies hysteresis around these before reporting, so a user hovering
#: at an edge does not flip class every tick.
WALKING_MS = 2.2
FAST_MS = 8.0


def classify_speed(speed):
    """Bucket a raw speed. Returns None when there is nothing to say.

    The bucket is the only motion fact that survives this function: the raw
    figure is not stored, so no endpoint can later leak a numeric speed.
    """
    from users.models import User

    # expo-location reports -1 (or null) when it has no speed estimate — a cold
    # fix, or a stationary device. Not the same as "measured as zero".
    if speed is None or speed < 0:
        return None
    if speed >= FAST_MS:
        return User.MotionClass.FAST
    if speed >= WALKING_MS:
        return User.MotionClass.MOVING
    return User.MotionClass.STATIONARY


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
        user.location_updated_at = timezone.now()
        # Absent and -1 both mean "the phone did not say", which is not the same
        # claim as "perfect", so neither is allowed to overwrite a real figure
        # with a better-looking one.
        accuracy = serializer.validated_data.get('accuracy')
        user.location_accuracy_m = float(accuracy) if accuracy is not None and accuracy >= 0 else None
        fields = [
            'latitude', 'longitude', 'location_updated_at', 'location_accuracy_m',
            'updated_at',
        ]

        motion = classify_speed(serializer.validated_data.get('speed'))
        if motion is not None:
            user.motion_class = motion
            user.motion_updated_at = timezone.now()
            fields += ['motion_class', 'motion_updated_at']

        user.save(update_fields=fields)
        # Which city/region/district this position falls in, recomputed only
        # when the user has actually left the area we had for them. Cache-only,
        # so it never reaches upstream on the hot path.
        places.refresh_quietly(user)
        _trigger_nearby_notifications(user)
        # Best-effort: a zone alert is a nice-to-have, and a failure inside it
        # must not fail the location write that every other feature depends on.
        try:
            _trigger_zone_music_alerts(user)
        except Exception as exc:  # noqa: BLE001
            print(f'[Zone] music alert sweep failed: {exc}')
        return Response({'latitude': str(user.latitude), 'longitude': str(user.longitude)})


class NearbyUsersView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        delete_old_encounters_throttled()
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


# Shared across requests on purpose: reused worker threads keep their DB
# connections alive under CONN_MAX_AGE instead of opening fresh ones per poll,
# and the worker count caps how many extra connections the map can ever hold.
_MAP_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix='map-nearby')


def _run_subview(view_get, request):
    # These threads never see Django's request_started/finished signals, so
    # stale-connection cleanup has to happen by hand on both sides of the call.
    close_old_connections()
    try:
        return view_get(request)
    finally:
        close_old_connections()


class NearbyMapView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user
        if user.latitude is None or user.longitude is None:
            return Response(
                {'detail': 'Your location is not set.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from stories.views import NearbyStoriesView
        from clubs.views import ClubEventNearbyView

        # No shared transaction (ATOMIC_REQUESTS is off), so the three reads
        # are independent and can overlap instead of paying their latencies
        # back to back.
        users_f = _MAP_POOL.submit(_run_subview, NearbyUsersView().get, request)
        stories_f = _MAP_POOL.submit(_run_subview, NearbyStoriesView().get, request)
        meetups_f = _MAP_POOL.submit(_run_subview, ClubEventNearbyView().get, request)

        return Response({
            'users': users_f.result().data,
            'stories': stories_f.result().data,
            'meetups': meetups_f.result().data,
        })
