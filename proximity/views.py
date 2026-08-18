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
from .serializers import (
    EndSharingSessionSerializer,
    LocationUpdateSerializer,
    NearbyQuerySerializer,
    NearbyUserSerializer,
)
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


# ─── Machine-readable error codes ─────────────────────────────────────────────
# Paired with `detail` in the project's usual error shape (see users/views.py).
# The client branches on these and never on the prose, because the prose is
# copy: it gets rewritten, translated and shortened, and a client keyed to it
# breaks silently and at a distance.
#
# The distinction they draw is the one that matters to the map. Both are "we
# cannot answer you", and they mean opposite things about what to do next:

#: Sharing is off. Nothing to answer with, and nothing to wait for — the user
#: has to choose Live or Approximate first. Not a transient condition.
CODE_SHARING_PAUSED = 'location_sharing_paused'

#: Sharing is on, but no position has been reported yet. Transient by
#: definition: a fix is coming, or the client can supply one itself. The client
#: must show "getting your location" rather than "nobody nearby", because an
#: empty map here is a claim about the world that we have not checked.
CODE_LOCATION_NOT_AVAILABLE = 'location_not_available'


def location_not_available():
    """The response for sharing-on-but-no-position-yet. One shape, one place."""
    return Response(
        {
            'detail': 'Location has not been reported yet.',
            'code': CODE_LOCATION_NOT_AVAILABLE,
        },
        status=status.HTTP_400_BAD_REQUEST,
    )


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


def _viewer_zone_contains(user, lat, lon, candidate_tolerance_m, anchor):
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
        # `anchor`, not `user.latitude`. A circle zone is centred on where the
        # viewer is, and on the private-query path that is a coordinate this
        # request carried rather than one on the row — a paused viewer has no
        # stored position at all, and reading one here would raise or, worse,
        # silently measure from a position that is no longer theirs.
        anchor_lat, anchor_lon = anchor
        metres = haversine(anchor_lat, anchor_lon, lat, lon) * 1000
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
    if not user.is_on_map():
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

    # Each direction is gated on the *sender's* own sharing setting inside
    # `can_surface_in_proximity`, so this stays one call: someone paused is
    # neither announced to anyone nor told about anyone.
    notify_nearby_matches(user, matches)
    # An encounter is a symmetric record of two people having been in the same
    # place, so it takes consent from both. The candidate's is settled by
    # `visible_users(on_map=True)` above and the reporter's by the early return
    # at the top of this function — which is what keeps the encounter log free
    # of positions nobody agreed to publish.
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
    # Both halves of this alert say where somebody is standing — one tells the
    # owner a friend just walked into earshot, the other tells the visitor whose
    # earshot they are in. Neither is sendable without the person it discloses
    # being on the map, and the mover is the one disclosed here.
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
    # `on_map_q` for the owner side: a zone belongs to a person, and a person
    # who is off the map has no zone on it to be walked into.
    owners = User.objects.filter(
        visibility.on_map_q(),
        pk__in=approved_ids,
        music_zone_active=True,
        latitude__isnull=False,
        longitude__isnull=False,
    )

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

    Reporting a position never changes who may see it: this view does not touch
    `location_sharing`, so no amount of a phone sitting there reporting can put
    its owner on the map. That separation is the whole of the fix for 5.1.2(i) —
    a stored position is the anchor for the owner's *own* queries ("who is near
    me", "which district am I in"), and `visible_users` decides independently
    whether anybody else ever sees it.
    """

    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request):
        serializer = LocationUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = request.user

        # Paused means we hold no position at all, so there is nothing for this
        # to write. The client already refuses to send one, which makes this
        # unreachable from our own app — and that is exactly why it is here:
        # the only caller that can reach it is one we did not write, and "the
        # app would not do that" is not an access control.
        #
        # `is_sharing`, not `is_on_map`: this is the endpoint that *makes* a
        # position fresh, so gating it on the freshness of the one it replaces
        # would lock out precisely the users trying to fix it.
        if not user.is_sharing():
            return Response(
                {
                    'detail': 'Location sharing is paused.',
                    'code': CODE_SHARING_PAUSED,
                },
                status=status.HTTP_403_FORBIDDEN,
            )
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


def nearby_users(user, lat, lon, request):
    """Everyone `user` may see from (`lat`, `lon`), serialized.

    The anchor is a parameter rather than a read of `user.latitude` because it
    has two sources and they are not interchangeable:

      * a user who shares their location is anchored on the position stored for
        them, which is also the position other people see;
      * a paused user is anchored on a coordinate their device sent for this
        request only, which is never stored and never shown to anybody.

    Nothing in here writes. Everything that used to be a side effect of asking
    "who is near me" — the encounter rows, the nearby pushes, the place
    snapshot — lives on the write path in `UpdateLocationView`, which is the
    half a paused user cannot reach. Keeping this function free of writes is
    what makes the private path safe to expose rather than a second way to
    become visible.
    """
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
            anchor=(lat, lon),
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
    return serializer.data


class NearbyUsersView(APIView):
    """Who is near the position stored for the caller.

    Only answers for somebody who shares their location, because only they have
    a stored position — see NearbyQueryView for the other half.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        delete_old_encounters_throttled()
        user = request.user

        # Paused: empty, not an error. Their client uses the query endpoint and
        # only reaches this while switching over; a failure there would be
        # noise about a state that is working as intended.
        #
        # `is_sharing` throughout this view: the caller's own freshness governs
        # what *others* see of them, not whether they may ask this question. A
        # stale sharer still gets an answer, anchored on the position below,
        # and the report going out alongside this refreshes it.
        if not user.is_sharing():
            return Response([])

        # Sharing, but nothing on file yet — the gap between choosing Live and
        # the first report landing, which pausing having deleted the position
        # makes certain rather than rare. A 400 rather than an empty list, and
        # the difference matters: an empty list is indistinguishable from
        # "nobody is around", so the client would draw a blank map and believe
        # it. The 400 is what services/discovery.ts falls back to the private
        # query on, and it is the same answer /api/map/nearby/ gives.
        if user.latitude is None or user.longitude is None:
            return location_not_available()

        return Response(nearby_users(
            user, float(user.latitude), float(user.longitude), request,
        ))


class EndSharingSessionView(APIView):
    """"This app just started, and I did not bring a sharing session with me."

    The half of the manual check-in rule that no event handler can enforce,
    because the event is the *absence* of one: the app starting up with nothing
    in memory. App Review's list ends with "any previous preference says they
    were visible", and a `location_sharing` that outlived the run it was chosen
    in is precisely that preference. This is what stops it outliving.

    Called once per launch by the UI, never by the background location task —
    that task exists to keep a *current* session's position fresh, and having
    it end the session would make backgrounding a check-out, which is the thing
    we were told not to build.

    Idempotent and safe to call at any moment, which is the whole reason it
    takes a token rather than being a bare "pause me":

        no session on the row       nothing to end, already paused
        token matches               this run owns the visibility — left alone
        token missing or stale      a different run chose it — paused

    That last line is the one doing the work, and the middle line is what makes
    it safe to fire on startup at all: a user who opens the app and immediately
    taps Live has a token by the time this lands, so their tap survives a
    startup effect that was already in flight.
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = EndSharingSessionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = request.user
        presented = serializer.validated_data.get('session')

        if user.sharing_session is not None and presented == user.sharing_session:
            # This process owns the current visibility. Nothing to do — and
            # nothing said, so a client cannot tell a live session from a
            # missing one by the shape of the answer.
            return Response({'location_sharing': user.location_sharing})

        # `is_sharing`: a session whose position has already gone stale still
        # has to be torn down, and testing freshness here would skip exactly
        # the rows most in need of clearing.
        if user.is_sharing():
            # Same teardown as choosing Paused by hand, and deliberately the
            # same code path: a session ending must not leave a weaker trace
            # than the button does.
            user.location_sharing = User.LocationSharing.PAUSED
            user.latitude = None
            user.longitude = None
            user.location_updated_at = None
            user.location_accuracy_m = None
            user.motion_class = User.MotionClass.STATIONARY
            user.motion_updated_at = None
            user.save(update_fields=[
                'location_sharing', 'latitude', 'longitude',
                'location_updated_at', 'location_accuracy_m',
                'motion_class', 'motion_updated_at',
            ] + user.end_sharing_session())
        elif user.sharing_session is not None:
            # Already paused but holding a stale token. Tidy rather than leave
            # something a later request could match.
            user.save(update_fields=user.end_sharing_session())

        return Response({'location_sharing': user.location_sharing})


class NearbyQueryView(APIView):
    """Who is near a coordinate the caller supplies, for this request only.

    The browsing half of the map, and the reason a paused user can still use
    it. Everything about it is read-only by construction: it validates two
    numbers, hands them to `nearby_users` as an anchor, and returns the answer.
    It does not write `latitude`/`longitude`, `location_updated_at`,
    `location_accuracy_m` or the motion fields; it does not snapshot a place;
    it does not send an alert or record an encounter; and it cannot make the
    caller appear on anybody's map, because appearing is decided by
    `location_sharing` and nothing here touches that field either.

    The invariant, stated the way it is worth remembering:

        a public location update requires sharing to be on
        a private nearby query does not, and never turns it on

    Open to callers who *are* sharing, too. They have no reason to use it, but
    refusing them would mean a second policy to keep in step with this one, and
    the endpoint is harmless: it still writes nothing, and a client that lies
    about where it is could already have lied to the update endpoint instead.
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = NearbyQuerySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(nearby_users(
            request.user,
            float(serializer.validated_data['latitude']),
            float(serializer.validated_data['longitude']),
            request,
        ))


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


def _run(fn, *args):
    # These threads never see Django's request_started/finished signals, so
    # stale-connection cleanup has to happen by hand on both sides of the call.
    close_old_connections()
    try:
        return fn(*args)
    finally:
        close_old_connections()


def _map_payload(user, lat, lon, request):
    """The whole map from one anchor: people, stories, and club meetups.

    No shared transaction (ATOMIC_REQUESTS is off), so the three reads are
    independent and overlap instead of paying their latencies back to back.

    Meetups take no anchor at all — they are scoped by club membership, not by
    distance — so the coordinate never reaches that query.
    """
    from clubs.views import ClubEventNearbyView
    from stories.views import nearby_stories

    users_f = _MAP_POOL.submit(_run, nearby_users, user, lat, lon, request)
    stories_f = _MAP_POOL.submit(_run, nearby_stories, user, lat, lon, request)
    meetups_f = _MAP_POOL.submit(_run, _meetups, ClubEventNearbyView(), request)

    return {
        'users': users_f.result(),
        'stories': stories_f.result(),
        'meetups': meetups_f.result(),
    }


def _meetups(view, request):
    return view.get(request).data


class NearbyMapView(APIView):
    """The map, anchored on the position stored for the caller."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user
        # Same two-way split as NearbyUsersView, and for the same reason: the
        # client draws a different screen for each.
        if not user.is_sharing():
            return Response(
                {
                    'detail': 'Location sharing is paused.',
                    'code': CODE_SHARING_PAUSED,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if user.latitude is None or user.longitude is None:
            return location_not_available()
        return Response(_map_payload(
            user, float(user.latitude), float(user.longitude), request,
        ))


class MapQueryView(APIView):
    """The map, anchored on a coordinate supplied for this request only.

    The composite counterpart of NearbyQueryView, and read-only for the same
    reasons — see that class, which is where the invariant is written down.

    `place` rides along because a paused user still has to be able to tell
    which city they are in, and the ordinary route to that answer reads the
    stored position they do not have. Resolved cache-only: `allow_upstream` is
    False, so this cannot reach a geocoder, and the boundary lookups that log
    coordinates on the upstream path are unreachable from here.
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = NearbyQuerySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        lat = float(serializer.validated_data['latitude'])
        lon = float(serializer.validated_data['longitude'])

        payload = _map_payload(request.user, lat, lon, request)
        payload['place'] = _transient_place(lat, lon)
        return Response(payload)


def _transient_place(lat, lon):
    """Which city/district a point falls in, computed and thrown away.

    Reads the Region cache only. Nothing is written — in particular no
    UserPlace row, which is the persistent "where is this user" record and
    exactly the kind of presence this path must not create.
    """
    hierarchy = places.hierarchy_for_point(lat, lon, allow_upstream=False)
    return {
        'city': hierarchy.city_name or None,
        'district': hierarchy.district_name or None,
        'region': hierarchy.region_name or None,
        'country_code': hierarchy.country_code or None,
    }
