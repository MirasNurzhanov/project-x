import math
from concurrent.futures import ThreadPoolExecutor

from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from django.db import close_old_connections
from django.db.models import Q
from django.utils import timezone
from regions import places
from users import visibility, zones
from users.models import User
from friends.models import FriendRequest, HiRequest
from .serializers import CheckInSerializer, LocationUpdateSerializer, NearbyUserSerializer
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


#: Ceiling on how many people one request will return.
#:
#: A 500m circle is self-limiting; a city zone is not. Without a cap, a viewer
#: who picks a dense city asks this endpoint — polled every ten seconds by every
#: client — to serialise every account in that city, and hands the map more
#: markers than it can draw. The nearest N is the useful part of that answer
#: anyway. Raise it when the payload and the map can carry more, not before.
MAX_NEARBY_MATCHES = 200

#: Rows fetched before containment is tested, per tier. Bounds the Python
#: ray-casting pass as well as the query: a city bounding box can hold far more
#: people than are actually inside the boundary, and every one of them would
#: otherwise be walked. This is the number that would stop mattering with a
#: PostGIS GIST index doing containment in the database.
MAX_ZONE_CANDIDATES = 1000


def _viewer_zone_box(user, lat, lon):
    """A database prefilter for people who might be inside `user`'s zone.

    Over-selects on purpose. `zone_contains` runs in Python, so the job here is
    to get the candidate set down to a bounding box before any ray casting
    happens — being exact is the next step's problem.

    Circles bound themselves. Polygons use the region's own stored box, on
    latitude always and on longitude only when the region does not cross the
    antimeridian: those bounds are stored *unwrapped* and can legitimately run
    past ±180, so comparing an ordinary ±180 longitude against them would
    exclude exactly the regions that are hardest to get right. Latitude never
    wraps. For a dateline-crossing region the latitude band alone is a looser
    filter and `Region.contains` — which aligns longitude itself — settles it.
    """
    if user.zone_type in User.POLYGON_ZONE_TYPES and user.zone_region_id:
        region = user.zone_region
        box = Q(latitude__range=(region.min_lat, region.max_lat))
        if not region.crosses_antimeridian:
            box &= Q(longitude__range=(region.min_lon, region.max_lon))
        return box

    # Circle, or a polygon type whose boundary has been evicted — see
    # `_viewer_zone_contains` for why that degrades to the circle here.
    km = user.zone_radius / 1000
    lat_delta = km / 111.0
    lon_delta = km / (111.0 * math.cos(math.radians(lat)))
    return Q(
        latitude__range=(lat - lat_delta, lat + lat_delta),
        longitude__range=(lon - lon_delta, lon + lon_delta),
    )


def _viewer_zone_contains(user, lat, lon, candidate_tolerance_m):
    """Whether `user`'s own zone covers a candidate at (`lat`, `lon`).

    The viewer's zone governs, and the direction is the whole design: this asks
    "is *they* inside *my* zone", never the reverse. Visibility is deliberately
    not mutual. Someone who picks their whole city sees everybody in it; the
    people they see keep whatever range they chose for themselves, and mostly
    cannot see back.

    Tolerance belongs to the *candidate*, not the viewer — it is their position
    being tested against the boundary, and an Approximate sharer's stored point
    is a ~500m grid centre. Without it a fuzzed position lands over a district
    line and somebody standing in the district is not shown as being in it.

    A zone whose boundary has been evicted falls back to the radius. That is a
    silent degradation and it is the right one here: the alternative is an empty
    map for the viewer with nothing to explain it, and `zone_region` is
    SET_NULL, so the state arrives without anyone doing anything wrong. It is
    the same fallback `effective_zone_mode` makes for drawing. Refusing to
    approximate applies to *storing* a zone, where the user is present to be
    told — see the serializer.
    """
    try:
        return user.zone_contains(lat, lon, tolerance_m=candidate_tolerance_m)
    except zones.ZoneBoundaryUnavailable:
        metres = haversine(
            float(user.latitude), float(user.longitude), lat, lon,
        ) * 1000
        return metres <= user.zone_radius + candidate_tolerance_m


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

    # Each direction is gated on the *sender's* own check-in inside
    # `can_surface_in_proximity`, so this stays a single call: a checked-out
    # user is neither announced to anyone nor told about anyone, and no
    # third state has to be maintained here.
    notify_nearby_matches(user, matches)
    # An encounter is a symmetric record of two people having been in the same
    # place, so it takes consent from both. The candidate's is already settled
    # — `visible_users(on_map=True)` filtered on it — which leaves the reporter.
    # Nothing is written while they are checked out, which is what keeps the
    # encounter log free of positions nobody agreed to publish.
    if user.is_on_map():
        record_encounters(user, matches)


def _trigger_zone_music_alerts(user):
    """`user` has just reported a position. Whose music are they now inside?

    Friends and connections only, in both roles. A stranger's zone music is
    not something they asked to be told about, and a stranger walking past is
    not news to the owner either — the map already shows both of them to
    each other.

    Only the person who *moved* is evaluated, which is enough to cover both
    directions: when you walk into a stationary friend's zone this runs for
    you, and when they walk into yours it runs for them.
    """
    if user.latitude is None or user.longitude is None:
        return
    if user.location_sharing == User.LocationSharing.PAUSED:
        return
    # Both halves of this alert say where somebody is standing — one tells the
    # owner a friend just walked into earshot, the other tells the visitor
    # whose earshot they are in. Neither is sendable without a check-in from
    # the person it discloses, and the mover is the one being disclosed here.
    if not user.is_on_map():
        return

    lat = float(user.latitude)
    lon = float(user.longitude)

    # Friends and connections both qualify — `visibility.can_view('zone_audio')`
    # allows the same two tiers, and `notify_zone_music_entry` re-checks it
    # per owner anyway, so this only has to avoid querying strangers at all.
    friend_ids, connected_ids = _get_relationship_ids(user, visibility.blocked_ids_for(user) | {user.pk})
    approved_ids = friend_ids | connected_ids
    if not approved_ids:
        return

    # Not filtered on `zone_active`. That flag is about the zone drawn on the
    # map, and using it here tied someone's music to whether they wanted a
    # circle around themselves. Whether there is a track to be in earshot of is
    # settled by `has_zone_track` in `notify_zone_music_entry`, which is a
    # property across three columns and so cannot be a filter here.
    #
    # `music_zone_active` can be, and is: a switched-off music zone is not there
    # to be walked into, so those rows are dropped before the distance maths
    # rather than walked and discarded. `notify_zone_music_entry` re-checks it
    # via `broadcasts_zone_track` for the callers that do not come through here.
    # `checked_in_q` for the owner side: a zone belongs to a person, and a
    # person who is off the map has no zone on it to be walked into.
    owners = User.objects.filter(
        visibility.checked_in_q(),
        pk__in=approved_ids,
        music_zone_active=True,
        latitude__isnull=False,
        longitude__isnull=False,
    ).exclude(location_sharing=User.LocationSharing.PAUSED)

    # `zone_mode` is not consulted. A music zone is always its own circle of
    # `music_zone_radius` now — it never inherits a region border, which is what
    # the removed `music_zone_separate` flag used to allow and what made this
    # loop skip region-mode owners to avoid alerting on everyone in town.
    for owner in owners:
        metres = haversine(lat, lon, float(owner.latitude), float(owner.longitude)) * 1000
        if metres <= owner.music_zone_radius:
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
    """Where the phone says it is.

    Reporting a position is not checking in and never becomes one: this view
    does not touch `is_checked_in` or `checked_in_at`, so no amount of a phone
    sitting there reporting can put its owner on the map. That separation is the whole of the fix for
    5.1.2(i) — the position is stored as the anchor for the owner's *own*
    queries ("who is near me", which district am I in), and `visible_users`
    decides whether anybody else ever sees it.
    """

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

        # Motion only ever exists to be drawn on somebody else's map, so it is
        # not recorded for a user who is not on one. Without this a checked-out
        # user would still be accumulating a movement classification, which is
        # state about them that has no reader and no consent behind it.
        motion = classify_speed(serializer.validated_data.get('speed'))
        if motion is not None and user.is_on_map():
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


class MapCheckInView(APIView):
    """The one way onto other people's maps, and the one way off.

    GET    — whether you are on the map, and since when.
    POST   — go on the map, from a position supplied in the same request.
    DELETE — come off it now.

    Nothing else in the codebase sets `is_checked_in`. That is the point, and
    it carries more weight now that a check-in does not expire: the flag can
    only move because somebody pressed something, so the consent screen in
    front of the POST is the whole of what the user agreed to and the DELETE is
    the whole of how they take it back. The location reporter below
    deliberately does not touch these fields — a phone left running cannot put
    its owner on the map.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response(request.user.check_in_state())

    def post(self, request):
        serializer = CheckInSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = request.user

        # Paused is a standing "do not put me on the map"; checking in over the
        # top of it would be the app overruling a setting the user last touched
        # on purpose. Refused with something the client can act on rather than
        # silently flipping it back to Live.
        if user.location_sharing == User.LocationSharing.PAUSED:
            return Response(
                {
                    'detail': 'Location sharing is paused. Resume it in Privacy '
                              'to check in.',
                    'code': 'location_sharing_paused',
                },
                status=status.HTTP_409_CONFLICT,
            )

        # The position rides the check-in and is stored through the same
        # fuzzing path as any other report, so an Approximate sharer's precise
        # point is discarded here too rather than being written just this once.
        user.latitude, user.longitude = visibility.store_coordinates(
            user,
            serializer.validated_data['latitude'],
            serializer.validated_data['longitude'],
        )
        user.location_updated_at = timezone.now()
        accuracy = serializer.validated_data.get('accuracy')
        user.location_accuracy_m = (
            float(accuracy) if accuracy is not None and accuracy >= 0 else None
        )

        fields = user.apply_check_in() + [
            'latitude', 'longitude', 'location_updated_at', 'location_accuracy_m',
            'updated_at',
        ]
        user.save(update_fields=fields)
        places.refresh_quietly(user)
        return Response(user.check_in_state(), status=status.HTTP_200_OK)

    def delete(self, request):
        """Leave the map immediately.

        Idempotent, and never an error: someone tapping "check out" twice, or
        from two screens at once, wants the same outcome both times and should
        not be shown a failure for asking. This is the only way off the map, so
        it has to succeed whatever state it finds.
        """
        user = request.user
        user.save(update_fields=user.apply_check_out() + ['updated_at'])
        return Response(user.check_in_state(), status=status.HTTP_200_OK)


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

        # How far this viewer sees, which is now something they chose rather
        # than a constant. `zone_box` is the bounding box of their zone —
        # their circle, or their district's or city's outline — and every
        # non-friend tier below is gated on it. Nothing about the people being
        # looked at enters into it: visibility is not mutual, so somebody found
        # this way generally cannot see back.
        #
        # Deliberately independent of `zone_active`, which has one documented
        # job — whether the circle is *drawn* on the map — and has never gated
        # anything else. See ZoneSwitchScopeTests.
        zone_box = _viewer_zone_box(user, lat, lon)

        # Connected: inside the zone, or within CONNECTED_RADIUS_KM.
        #
        # The 1km stays as a floor rather than being replaced. Somebody you have
        # exchanged a Hi with is a weaker tie than a friend but not a stranger,
        # and shrinking your own zone to 50m is a statement about *discovery* —
        # it should not quietly drop the people you have already met. The zone
        # only ever widens this tier.
        if connected_ids:
            conn_lat_delta = CONNECTED_RADIUS_KM / 111.0
            conn_lon_delta = CONNECTED_RADIUS_KM / (111.0 * math.cos(math.radians(lat)))
            connected_candidates = base_qs.filter(
                Q(
                    latitude__range=(lat - conn_lat_delta, lat + conn_lat_delta),
                    longitude__range=(lon - conn_lon_delta, lon + conn_lon_delta),
                ) | zone_box,
                pk__in=connected_ids,
            )[:MAX_ZONE_CANDIDATES]
        else:
            connected_candidates = User.objects.none()

        # Strangers: inside the viewer's zone, filtered by age_tier and
        # interests. This is where the fixed 500m used to be, and it is gone —
        # a viewer's reach is their zone and nothing else. For anyone who has
        # never touched the setting that is a 500m circle, which is the same
        # number by a different route.
        stranger_candidates = (
            base_qs
            .exclude(pk__in=friend_ids)
            .exclude(pk__in=connected_ids)
            .filter(zone_box)
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

        # The exact test, for both gated tiers. The box above was a bounding
        # box; this is the boundary itself — a circle measured properly, or the
        # ray cast against the real outline. Tolerance is the *candidate's*,
        # because it is their stored position being tested and theirs that may
        # have been snapped to a grid cell.
        def in_zone(candidate):
            return _viewer_zone_contains(
                user,
                float(candidate.latitude), float(candidate.longitude),
                visibility.position_tolerance_m(candidate),
            )

        for candidate in connected_candidates:
            if candidate.pk in seen:
                continue
            dist = round(haversine(lat, lon, float(candidate.latitude), float(candidate.longitude)), 2)
            # The 1km floor, or the zone if it reaches further. Either qualifies.
            if dist <= CONNECTED_RADIUS_KM or in_zone(candidate):
                distances[candidate.pk] = dist
                nearby.append(candidate)
                seen.add(candidate.pk)

        for candidate in stranger_candidates[:MAX_ZONE_CANDIDATES]:
            if candidate.pk in seen:
                continue
            if not in_zone(candidate):
                continue
            distances[candidate.pk] = round(
                haversine(lat, lon, float(candidate.latitude), float(candidate.longitude)), 2)
            nearby.append(candidate)
            seen.add(candidate.pk)

        # Nearest first, then capped. The order matters more than it used to:
        # inside a city zone this is the difference between the 200 people
        # closest to you and 200 arbitrary rows from the same city.
        #
        # Friends are exempt. They are visible at any distance by design, and a
        # cap that quietly dropped the friend across town the moment a city zone
        # filled the list would be the zone setting silently changing something
        # that has nothing to do with discovery.
        nearby.sort(key=lambda u: distances.get(u.pk, float('inf')))
        if len(nearby) > MAX_NEARBY_MATCHES:
            friends = [u for u in nearby if u.pk in friend_ids]
            others = [u for u in nearby if u.pk not in friend_ids]
            room = max(0, MAX_NEARBY_MATCHES - len(friends))
            nearby = friends + others[:room]
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


# Shared across requests on purpose: eight threads is the ceiling on how much
# of the map fan-out can be in flight at once, so the endpoint cannot open
# work faster than the database can absorb it.
#
# It used to also be the thing keeping connections alive between polls, via
# CONN_MAX_AGE on the reused threads. That job now belongs to the psycopg pool
# in core.settings, which holds connections against the *process* rather than
# against whichever thread happened to survive — and DB_POOL_MAX_SIZE, not this
# number, is what bounds connections to the server.
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
