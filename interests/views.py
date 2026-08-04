from django.db.models import Case, Count, IntegerField, Q, Value, When
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from django.shortcuts import get_object_or_404
from . import categories
from .models import Interest
from .serializers import InterestSerializer, category_summary


def _category_rank():
    """Sort key putting the nine groups in display order, ungrouped last.

    Display order is not alphabetical and is not a property of the row, so it
    cannot be an `order_by` on a column. Doing it in SQL rather than leaving it
    to the client matters: it means the flat list this endpoint returns is
    *already* grouped, so even a screen that renders it as one long strip shows
    Sports together and Music together instead of interleaving 681 rows by
    name.
    """
    return Case(
        *[
            When(category=key, then=Value(index))
            for key, index in categories.ORDER.items()
        ],
        default=Value(categories.UNCATEGORISED_ORDER),
        output_field=IntegerField(),
    )


def _visible_to(user):
    # Unreviewed custom entries are only visible to the user who added them
    # (i.e. anyone who has them selected).
    return Interest.objects.filter(
        Q(needs_review=False) | Q(users=user)
    ).distinct()


class InterestListView(generics.ListAPIView):
    """Every interest, in group order.

    `?category=sports` narrows to one group — the catalogue is 681 entries and
    a screen drawing one section at a time should not download the other eight
    to find it.
    """

    serializer_class = InterestSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = _visible_to(self.request.user)

        wanted = (self.request.query_params.get('category') or '').strip().lower()
        if wanted:
            # An unknown key filters to nothing rather than being ignored:
            # quietly returning all 681 for a typo is how a picker ends up
            # showing the whole catalogue under one heading.
            qs = qs.filter(category=wanted) if wanted in categories.ORDER else qs.none()

        return qs.annotate(category_rank=_category_rank()).order_by(
            'category_rank', 'name',
        )


class InterestCategoryListView(APIView):
    """GET /api/interests/categories/ — the headings, in order, with counts.

    Lets a picker lay out and label its sections without first walking the
    whole catalogue, and keeps the order of those sections one decision made on
    the server rather than one each screen re-invents.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        counts = dict(
            _visible_to(request.user)
            .values_list('category')
            .annotate(total=Count('id', distinct=True))
        )
        return Response(category_summary(counts))


class SelectInterestView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        interest = get_object_or_404(Interest, pk=pk)
        request.user.interests.add(interest)
        return Response(status=status.HTTP_204_NO_CONTENT)

    def delete(self, request, pk):
        interest = get_object_or_404(Interest, pk=pk)
        request.user.interests.remove(interest)
        return Response(status=status.HTTP_204_NO_CONTENT)


class BulkSelectInterestsView(APIView):
    """Select several interests at once (onboarding flow)."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        ids = request.data.get('interest_ids')
        if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
            return Response({'detail': 'interest_ids must be a list of ids.'},
                            status=status.HTTP_400_BAD_REQUEST)
        interests = Interest.objects.filter(pk__in=ids)
        request.user.interests.add(*interests)
        return Response(status=status.HTTP_204_NO_CONTENT)


