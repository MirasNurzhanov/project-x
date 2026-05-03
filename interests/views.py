from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from django.shortcuts import get_object_or_404
from .models import Interest
from .serializers import InterestSerializer


class InterestListView(generics.ListAPIView):
    queryset = Interest.objects.all().order_by('name')
    serializer_class = InterestSerializer
    permission_classes = [permissions.IsAuthenticated]


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
