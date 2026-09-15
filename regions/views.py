"""Region lookup for zones that follow a real border.

Every endpoint here answers from the cache first. Upstream is only consulted
when the cache cannot answer, which for a given place happens exactly once —
after that the row is shared by everyone.

`for-point` is the interesting one, and it has three shapes of answer rather
than two. It resolves; or it found several plausible places and hands back
ranked choices rather than guessing; or it found nothing worth trusting and
says which. See regions.resolver for why the middle case exists.
"""
import logging

from django.conf import settings
from django.db.models import Q
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from . import geometry, nominatim, places, resolver, upstream
from .models import Region
from .resolver import Outcome
from .serializers import (
    CandidateSerializer,
    RegionSerializer,
    RegionSummarySerializer,
    TracedRegionSerializer,
)

logger = logging.getLogger(__name__)

MIN_QUERY_LEN = 2
LOCAL_RESULT_TARGET = 5

# The two sizes a zone can actually be. Callers may narrow to them with
# `?kinds=city,district`; without the parameter nothing is filtered, which is
# what every existing caller expects.
SELECTABLE_KINDS = (Region.Kind.CITY, Region.Kind.DISTRICT)


def _upstream_unavailable(exc, detail):
    """Turn an upstream failure into a response the client can act on.

    A busy limiter and a broken upstream are both 503, but only one of them is
    worth retrying in a second, so only one carries Retry-After. Without the
    distinction a client either hammers a dead service or gives up on a queue
    that was about to clear.
    """
    if isinstance(exc, upstream.UpstreamBusy):
        response = Response(
            {
                'detail': 'Too many place lookups at once. Try again in a moment.',
                'retry_after': exc.retry_after,
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
        response['Retry-After'] = str(exc.retry_after)
        return response
    return Response({'detail': detail}, status=status.HTTP_503_SERVICE_UNAVAILABLE)


def _requested_kinds(request):
    """`?kinds=city,district` → a tuple, or None for "no filter".

    Unrecognised names are dropped rather than rejected: the parameter is a
    narrowing convenience, and a typo that silently widens the results is a
    better failure than a 400 in the middle of someone typing a city name.
    """
    raw = (request.query_params.get('kinds') or '').strip()
    if not raw:
        return None
    wanted = tuple(
        k for k in (part.strip().lower() for part in raw.split(','))
        if k in Region.Kind.values
    )
    return wanted or None


class RegionSearchView(APIView):
    """GET /api/regions/search/?q=almaty[&kinds=city,district]

    Cached rows first. Upstream is only asked when the cache is thin, so the
    common case — someone typing a city thousands of people already picked —
    never leaves the database.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        query = (request.query_params.get('q') or '').strip()
        if len(query) < MIN_QUERY_LEN:
            return Response([])

        kinds = _requested_kinds(request)
        rows = Region.objects.filter(
            Q(name__icontains=query) | Q(display_name__icontains=query)
        ).filter(Q(verified=True) | Q(submitted_by=request.user))
        if kinds:
            rows = rows.filter(kind__in=kinds)
        local = list(rows[:LOCAL_RESULT_TARGET])
        if len(local) >= LOCAL_RESULT_TARGET:
            return Response(RegionSummarySerializer(local, many=True).data)

        try:
            fetched = nominatim.search(query)
        except nominatim.NominatimError as exc:
            logger.warning('region_search_failed', extra={'error_type': type(exc).__name__})
            # Partial results beat an error page: whatever is cached is still a
            # usable answer, and the client shows it rather than an empty state.
            if local:
                return Response(RegionSummarySerializer(local, many=True).data)
            return _upstream_unavailable(
                exc, 'Could not search places right now. Please try again.',
            )

        seen = {r.pk for r in local}
        merged = local + [
            r for r in fetched
            if r.pk not in seen and (not kinds or r.kind in kinds)
        ]
        return Response(RegionSummarySerializer(merged, many=True).data)


class RegionForPointView(APIView):
    """GET /api/regions/for-point/?lat=..&lon=..&level=city|district

    "The place I am standing in", at one of the two sizes a zone offers.
    `level` defaults to city.

    Three answers, distinguished by shape so an existing client keeps working:

      * resolved — 200, the region's own fields at the top level, as before,
        with `outcome` and `stage` added.
      * needs a choice — 200 with `candidates` and no `id`. Either several
        places fit, or none fits well enough to assume.
      * nothing — 404, with whatever the address hierarchy could name.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        try:
            lat = float(request.query_params.get('lat'))
            lon = float(request.query_params.get('lon'))
        except (TypeError, ValueError):
            return Response(
                {'detail': 'lat and lon are required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return Response(
                {'detail': 'lat and lon are out of range.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        level = (request.query_params.get('level') or Region.Kind.CITY).lower()
        if level not in SELECTABLE_KINDS:
            return Response(
                {'detail': "level must be 'city' or 'district'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = resolver.resolve(lat, lon, kind=level, viewer=request.user)
        except upstream.UpstreamError as exc:
            logger.warning('region_lookup_failed', extra={'error_type': type(exc).__name__})
            return _upstream_unavailable(
                exc, 'Could not find your area right now. Please try again.',
            )

        if result.resolved:
            body = RegionSummarySerializer(result.region).data
            body['outcome'] = result.outcome.value
            body['stage'] = result.stage
            return Response(body)

        if result.outcome in (Outcome.AMBIGUOUS, Outcome.REJECTED):
            return Response({
                'outcome': result.outcome.value,
                'detail': result.detail,
                'city_name': result.city_name,
                'candidates': CandidateSerializer(result.candidates, many=True).data,
                'can_trace': True,
            })

        # A miss still knows *where* it looked, because the address hierarchy
        # names the place even when no boundary is mapped for it. Saying "no
        # district is mapped inside Almaty" is worth far more than "not found",
        # and it is what lets the client point at the other button.
        return Response(
            {
                'outcome': result.outcome.value,
                'detail': result.detail,
                'city_name': result.city_name,
                'name': result.subdivision_names[0] if result.subdivision_names else None,
                'can_trace': True,
            },
            status=status.HTTP_404_NOT_FOUND,
        )


class RegionTraceView(APIView):
    """POST /api/regions/trace/ — a boundary somebody drew by hand.

    The end of the line for coverage: when OSM has nothing and no fallback
    found anything, the alternative to "you cannot have a zone here" is letting
    the person who is standing in the place say where it ends.

    Trusted narrowly. The shape has to contain the submitter, it has to be a
    plausible size, and until somebody verifies it only its author can see it —
    otherwise one person's rough outline becomes everyone's district.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = TracedRegionSerializer(
            data=request.data,
            context={
                'max_area': getattr(settings, 'REGION_MAX_TRACED_AREA_DEG2', 1.0),
            },
        )
        serializer.is_valid(raise_exception=True)

        user = request.user
        if user.latitude is None or user.longitude is None:
            return Response(
                {'detail': 'Approx needs your location before you can trace an area.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        rings = serializer.context['unwrapped']
        min_lat, min_lon, max_lat, max_lon = serializer.context['bounds']

        # The user's coordinates are stored as Decimal; the geometry is float arithmetic.
        if not geometry.contains(
            rings, min_lat, min_lon, max_lat, max_lon, float(user.latitude), float(user.longitude),
        ):
            # The same rule the zone endpoint enforces, applied at the source:
            # you can only draw the place you are in.
            return Response(
                {'detail': 'That outline does not cover where you are.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        centroid_lat, centroid_lon = geometry.centroid(min_lat, min_lon, max_lat, max_lon)
        data = serializer.validated_data
        region = Region.objects.create(
            osm_type='', osm_id=None,
            name=data['name'].strip()[:200],
            display_name=', '.join(
                part for part in (data['name'].strip(), data.get('parent_name', '').strip())
                if part
            )[:500],
            country_code='',
            kind=data['kind'],
            admin_level=None,
            parent_name=data.get('parent_name', '').strip()[:200],
            min_lat=min_lat, min_lon=min_lon, max_lat=max_lat, max_lon=max_lon,
            centroid_lat=centroid_lat, centroid_lon=centroid_lon,
            rings=rings,
            point_count=sum(len(r) for r in rings),
            source=Region.Source.USER,
            verified=False,
            submitted_by=user,
        )
        upstream.event(
            'regions', 'traced', region_id=region.pk, kind=region.kind,
            points=region.point_count, user=user.pk,
        )
        return Response(
            RegionSummarySerializer(region).data, status=status.HTTP_201_CREATED,
        )


class RegionDetailView(APIView):
    """GET /api/regions/<pk>/ — the outline, for drawing.

    Split from the summary because the rings are ~100x the rest of the payload
    and the map only needs them once per region, however many people share it.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        region = Region.objects.filter(pk=pk).filter(
            Q(verified=True) | Q(submitted_by=request.user)
        ).first()
        if not region:
            return Response(
                {'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND,
            )
        return Response(RegionSerializer(region).data)
