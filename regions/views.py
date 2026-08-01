"""Region lookup for zones that follow a real border.

Every endpoint here answers from the cache first. Upstream is only consulted
when the cache cannot answer, which for a given place happens exactly once —
after that the row is shared by everyone.
"""
import logging

from django.db.models import Q
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from . import nominatim
from .models import Region
from .serializers import RegionSerializer, RegionSummarySerializer

logger = logging.getLogger(__name__)

MIN_QUERY_LEN = 2
LOCAL_RESULT_TARGET = 5


class RegionSearchView(APIView):
    """GET /api/regions/search/?q=almaty

    Cached rows first. Upstream is only asked when the cache is thin, so the
    common case — someone typing a city thousands of people already picked —
    never leaves the database.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        query = (request.query_params.get('q') or '').strip()
        if len(query) < MIN_QUERY_LEN:
            return Response([])

        local = list(
            Region.objects.filter(
                Q(name__icontains=query) | Q(display_name__icontains=query)
            )[:LOCAL_RESULT_TARGET]
        )
        if len(local) >= LOCAL_RESULT_TARGET:
            return Response(RegionSummarySerializer(local, many=True).data)

        try:
            fetched = nominatim.search(query)
        except nominatim.NominatimError as exc:
            logger.warning('Region search failed for %r: %s', query, exc)
            # Partial results beat an error page: whatever is cached is still a
            # usable answer, and the client shows it rather than an empty state.
            if local:
                return Response(RegionSummarySerializer(local, many=True).data)
            return Response(
                {'detail': 'Could not search places right now. Please try again.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        seen = {r.pk for r in local}
        merged = local + [r for r in fetched if r.pk not in seen]
        return Response(RegionSummarySerializer(merged, many=True).data)


class RegionForPointView(APIView):
    """GET /api/regions/for-point/?lat=..&lon=..

    "The city I am standing in". Answers from the cache when a stored region
    already contains the point, which makes the common case — a user in a city
    somebody has already picked — free.
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

        # Bounding box in SQL, exact containment in Python: the box query is
        # indexed and throws away all but a handful of rows, and only those get
        # the ring walk.
        candidates = Region.objects.filter(
            min_lat__lte=lat, max_lat__gte=lat,
            min_lon__lte=lon, max_lon__gte=lon,
        )
        # Smallest first: a point inside a city is also inside its province, and
        # "my city" is the tighter of the two.
        hits = sorted(
            (r for r in candidates if r.contains(lat, lon)),
            key=lambda r: (r.max_lat - r.min_lat) * (r.max_lon - r.min_lon),
        )
        if hits:
            return Response(RegionSummarySerializer(hits[0]).data)

        try:
            region = nominatim.reverse(lat, lon)
        except nominatim.NominatimError as exc:
            logger.warning('Reverse region lookup failed at %s,%s: %s', lat, lon, exc)
            return Response(
                {'detail': 'Could not find your area right now. Please try again.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        if not region:
            return Response(
                {'detail': 'No mapped area covers this location.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(RegionSummarySerializer(region).data)


class RegionDetailView(APIView):
    """GET /api/regions/<pk>/ — the outline, for drawing.

    Split from the summary because the rings are ~100x the rest of the payload
    and the map only needs them once per region, however many people share it.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        region = Region.objects.filter(pk=pk).first()
        if not region:
            return Response(
                {'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND,
            )
        return Response(RegionSerializer(region).data)
