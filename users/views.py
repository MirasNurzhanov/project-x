from django.db.models import Q
from rest_framework import generics, permissions, status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import TokenError
from friends.models import FriendRequest
from .serializers import RegisterSerializer, UserProfileSerializer, EditProfileSerializer, PublicProfileSerializer, UserSearchSerializer, ZoneUpdateSerializer
from .models import User
from .presets import ZONE_PRESETS


class RegisterView(generics.CreateAPIView):
    queryset = User.objects.all()
    serializer_class = RegisterSerializer
    permission_classes = [permissions.AllowAny]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        refresh = RefreshToken.for_user(user)
        return Response({
            'user': UserProfileSerializer(user).data,
            'tokens': {
                'refresh': str(refresh),
                'access': str(refresh.access_token),
            }
        })


class ProfileView(generics.RetrieveUpdateAPIView):
    permission_classes = [permissions.IsAuthenticated]
    http_method_names = ['get', 'patch']

    def get_object(self):
        return self.request.user

    def get_serializer_class(self):
        if self.request.method == 'PATCH':
            return EditProfileSerializer
        return UserProfileSerializer

    def update(self, request, *_args, **_kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(UserProfileSerializer(user).data)


class LogoutView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        try:
            token = RefreshToken(request.data['refresh'])
            token.blacklist()
            return Response(status=status.HTTP_204_NO_CONTENT)
        except KeyError:
            return Response({'detail': 'Refresh token required.'}, status=status.HTTP_400_BAD_REQUEST)
        except TokenError:
            return Response({'detail': 'Token is invalid or expired.'}, status=status.HTTP_400_BAD_REQUEST)


class PublicProfileView(generics.RetrieveAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = PublicProfileSerializer
    queryset = User.objects.prefetch_related('interests')
    lookup_url_kwarg = 'user_id'


class PrivacySettingsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request):
        user = request.user
        mode = request.data.get('privacy_mode', user.privacy_mode)
        from .models import User as UserModel
        if mode not in UserModel.PrivacyMode.values:
            from rest_framework.exceptions import ValidationError
            raise ValidationError({'privacy_mode': f"'{mode}' is not a valid choice."})
        user.privacy_mode = mode
        user.save(update_fields=['privacy_mode'])
        return Response({'privacy_mode': user.privacy_mode})


class ZoneView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request):
        serializer = ZoneUpdateSerializer(request.user)
        return Response(serializer.data)

    def patch(self, request):
        serializer = ZoneUpdateSerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class ZonePresetsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response(ZONE_PRESETS)


class UserSearchView(generics.ListAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = UserSearchSerializer

    def get_queryset(self):
        q = self.request.query_params.get('q', '').strip()
        me = self.request.user
        blocked_ids = set(me.blocking.values_list('blocked_id', flat=True))
        blocked_by_ids = set(me.blocked_by.values_list('blocker_id', flat=True))
        excluded = blocked_ids | blocked_by_ids | {me.pk}
        return (
            User.objects.filter(username__icontains=q)
            .exclude(pk__in=excluded)
            .prefetch_related('interests')
        )

    def get_serializer_context(self):
        context = super().get_serializer_context()
        me = self.request.user
        sent, received = {}, {}
        for fr in FriendRequest.objects.filter(Q(sender=me) | Q(receiver=me)):
            if fr.sender_id == me.pk:
                sent[fr.receiver_id] = fr.status
            else:
                received[fr.sender_id] = fr.status
        context['sent_requests'] = sent
        context['received_requests'] = received
        return context


class DeleteAccountView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request):
        request.user.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)