from django.db.models import Q
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from django.shortcuts import get_object_or_404
from .models import Interest
from .serializers import InterestSerializer


class InterestListView(generics.ListAPIView):
    serializer_class = InterestSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        # Unreviewed custom entries are only visible to the user who added them
        # (i.e. anyone who has them selected).
        return (
            Interest.objects.filter(
                Q(needs_review=False) | Q(users=self.request.user)
            )
            .distinct()
            .order_by('name')
        )


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


