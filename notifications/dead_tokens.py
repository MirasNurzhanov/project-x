"""Invalidate credentials, never delete the device identity or account."""
from django.conf import settings
from django.db import transaction
from django.utils.crypto import constant_time_compare
from rest_framework import serializers
from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView


def forget_tokens(tokens):
    from users.models import Device, User
    with transaction.atomic():
        count = Device.objects.filter(push_token__in=tokens).update(push_token='')
        User.objects.filter(push_token__in=tokens).update(push_token='')
    return count


class DispatcherPermission(BasePermission):
    def has_permission(self, request, view):
        expected = getattr(settings, 'PUSH_DISPATCHER_TOKEN', '')
        actual = request.headers.get('Authorization', '')
        return bool(expected) and constant_time_compare(actual, f'Bearer {expected}')


class DeadTokensInput(serializers.Serializer):
    tokens = serializers.ListField(
        child=serializers.RegexField(r'^(?:Expo|Exponent)PushToken\[[A-Za-z0-9_-]+\]$', max_length=256),
        allow_empty=False, max_length=100,
    )


class DeadPushTokensView(APIView):
    authentication_classes = []
    permission_classes = [DispatcherPermission]

    def post(self, request):
        serializer = DeadTokensInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        count = forget_tokens(serializer.validated_data['tokens'])
        return Response({'cleared_devices': count}, headers={'Cache-Control': 'no-store'})
