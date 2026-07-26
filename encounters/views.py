from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from django.db.models import Q
from django.shortcuts import get_object_or_404
from users.visibility import proximity_visible_ids
from .models import Encounter
from .serializers import EncounterSerializer


class EncounterListView(generics.ListAPIView):
    serializer_class = EncounterSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        qs = (
            Encounter.objects.filter(Q(user_one=user) | Q(user_two=user))
            .select_related('user_one', 'user_two')
        )
        # An encounter row is symmetric but visibility is not: A may still be
        # allowed to see B after B has opted out of being discovered by A.
        # Filtering on read is also what makes blocking clear the history
        # rather than only stopping new rows.
        other_ids = {
            (two if one == user.pk else one)
            for one, two in qs.values_list('user_one_id', 'user_two_id')
        }
        allowed = proximity_visible_ids(user, other_ids)
        return qs.filter(Q(user_one_id__in=allowed) | Q(user_two_id__in=allowed))


class ClearEncountersView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request):
        user = request.user
        Encounter.objects.filter(Q(user_one=user) | Q(user_two=user)).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AcknowledgeEncounterView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, pk):
        user = request.user
        encounter = get_object_or_404(
            Encounter,
            Q(user_one=user) | Q(user_two=user),
            pk=pk,
        )

        if encounter.user_one_id == user.pk:
            encounter.acknowledged_by_one = True
            encounter.save(update_fields=['acknowledged_by_one'])
        else:
            encounter.acknowledged_by_two = True
            encounter.save(update_fields=['acknowledged_by_two'])

        return Response(EncounterSerializer(encounter, context={'request': request}).data)
