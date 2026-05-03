from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from django.db.models import Q
from django.shortcuts import get_object_or_404
from .models import Encounter
from .serializers import EncounterSerializer


class EncounterListView(generics.ListAPIView):
    serializer_class = EncounterSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        return (
            Encounter.objects.filter(Q(user_one=user) | Q(user_two=user))
            .select_related('user_one', 'user_two')
        )


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
