from django.db.models import Max, Q
from django.utils import timezone
from rest_framework import generics, permissions, status
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import TokenError
from friends.models import FriendRequest
from . import visibility
from .links import LinkError, MAX_LINKS_PER_USER, normalize_link
from .serializers import (
    RegisterSerializer, UserProfileSerializer, EditProfileSerializer,
    PublicProfileSerializer, PrivacySettingsSerializer, ProfileLinkSerializer,
    UserSearchSerializer, ZoneUpdateSerializer, serialize_links,
)
from .models import ProfileLink, User
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
    """Revoke the session and stop the device being addressable.

    Blacklisting the refresh token is the part that actually ends the session —
    deleting it client-side only stops that client from using it, and it stays
    valid for its full seven days for anyone else holding a copy.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        # Unconditionally, and before the token work: a signed-out device must
        # stop receiving pushes even when the refresh token it sent is already
        # expired or malformed — which is exactly the offline-retry case.
        # Updated by pk so a stale in-memory user object cannot write it back.
        User.objects.filter(pk=request.user.pk).update(
            push_token=None,
            # Signing out is leaving, not idling. Backdated rather than nulled,
            # so "last seen" still reads correctly for friends.
            last_seen=timezone.now() - visibility.ONLINE_WINDOW,
            motion_class=User.MotionClass.STATIONARY,
            motion_updated_at=None,
        )

        try:
            token = RefreshToken(request.data['refresh'])
            token.blacklist()
            return Response(status=status.HTTP_204_NO_CONTENT)
        except KeyError:
            return Response({'detail': 'Refresh token required.'}, status=status.HTTP_400_BAD_REQUEST)
        except TokenError:
            return Response({'detail': 'Token is invalid or expired.'}, status=status.HTTP_400_BAD_REQUEST)


class PublicProfileView(APIView):
    """Someone else's profile. A blocked pair, in either direction, gets the
    same 404 a deleted account would — invisibility must not be detectable."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, user_id):
        target = visibility.visible_users(
            request.user, exclude_self=False,
        ).prefetch_related('interests').filter(pk=user_id).first()
        if target is None:
            return visibility.not_found()
        return Response(
            PublicProfileSerializer(target, context={'request': request}).data
        )


class PrivacySettingsView(APIView):
    """Read and write every privacy control in one place."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response(PrivacySettingsSerializer(request.user).data)

    def patch(self, request):
        user = request.user
        data = request.data.copy() if hasattr(request.data, 'copy') else dict(request.data)

        # Builds predating the location/profile split still send the old
        # active/ghost/invisible values; translate rather than reject them.
        legacy = data.pop('privacy_mode', None)
        if legacy is not None:
            legacy_value = legacy[0] if isinstance(legacy, (list, tuple)) else legacy
            mapped = User.LEGACY_PRIVACY_MODE_REVERSE.get(legacy_value)
            if mapped is None:
                raise ValidationError({'privacy_mode': f"'{legacy_value}' is not a valid choice."})
            data.setdefault('location_sharing', mapped)

        serializer = PrivacySettingsSerializer(user, data=data, partial=True)
        serializer.is_valid(raise_exception=True)
        previous_sharing = user.location_sharing
        user = serializer.save()

        # Switching to Approximate must destroy the precise position already on
        # file, not merely stop reporting it.
        if (
            user.location_sharing != previous_sharing
            and user.location_sharing == User.LocationSharing.APPROXIMATE
            and user.latitude is not None
            and user.longitude is not None
        ):
            user.latitude, user.longitude = visibility.snap_coordinates(
                user, user.latitude, user.longitude
            )
            user.save(update_fields=['latitude', 'longitude'])

        payload = PrivacySettingsSerializer(user).data
        payload['privacy_mode'] = user.privacy_mode
        return Response(payload)


class ProfileLinksView(APIView):
    """The user's own links: list, add, reorder.

    Validation happens here and only here — `normalize_link` decides what is
    storable, and what it returns is what gets written. Nothing about the link
    is ever fetched over the network.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response(serialize_links(request.user))

    def post(self, request):
        if request.user.links.count() >= MAX_LINKS_PER_USER:
            return Response(
                {'detail': f'You can have up to {MAX_LINKS_PER_USER} links.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            url, platform, handle, label = normalize_link(
                request.data.get('url'),
                platform_hint=request.data.get('platform'),
            )
        except LinkError as exc:
            return Response({'url': [str(exc)]}, status=status.HTTP_400_BAD_REQUEST)

        if request.user.links.filter(url=url).exists():
            return Response({'url': ['That link is already on your profile.']},
                            status=status.HTTP_400_BAD_REQUEST)

        last = request.user.links.aggregate(m=Max('position'))['m']
        link = ProfileLink.objects.create(
            user=request.user, url=url, platform=platform,
            handle=handle, label=label,
            position=0 if last is None else last + 1,
        )
        return Response(ProfileLinkSerializer(link).data, status=status.HTTP_201_CREATED)

    def patch(self, request):
        """Reorder. Body: {"order": [id, id, ...]} — ids not owned by the
        caller are ignored rather than errored, so a stale client cannot
        reveal whether some other id exists."""
        order = request.data.get('order')
        if not isinstance(order, list):
            return Response({'order': ['Expected a list of link ids.']},
                            status=status.HTTP_400_BAD_REQUEST)
        owned = {link.pk: link for link in request.user.links.all()}
        position = 0
        for raw_id in order:
            link = owned.pop(raw_id, None)
            if link is None:
                continue
            link.position = position
            link.save(update_fields=['position'])
            position += 1
        # Anything the client did not mention keeps a stable slot at the end.
        for link in owned.values():
            link.position = position
            link.save(update_fields=['position'])
            position += 1
        return Response(serialize_links(request.user))


class ProfileLinkDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, link_id):
        deleted, _ = ProfileLink.objects.filter(pk=link_id, user=request.user).delete()
        if not deleted:
            return visibility.not_found()
        return Response(status=status.HTTP_204_NO_CONTENT)


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


MIN_SEARCH_LENGTH = 2


class UserSearchView(generics.ListAPIView):
    """Find people by username (`?q=`) or by interest (`?interest=`).

    The two surfaces are gated separately, because opting out of one is not
    the same as opting out of the other. An empty query returns nothing: this
    used to be `username__icontains=''`, which matched every row and handed
    any caller the entire user directory.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = UserSearchSerializer

    def get_queryset(self):
        q = self.request.query_params.get('q', '').strip()
        interest = self.request.query_params.get('interest', '').strip()
        me = self.request.user

        if interest:
            qs = visibility.visible_users(me, discoverable_by='interest')
            if interest.isdigit():
                qs = qs.filter(interests__pk=int(interest))
            else:
                qs = qs.filter(interests__name__iexact=interest)
            qs = qs.distinct()
            if len(q) >= MIN_SEARCH_LENGTH:
                # Narrowing an interest search by name still needs the caller
                # to be findable by username.
                qs = qs.filter(username__icontains=q, discoverable_by_username=True)
            return qs.prefetch_related('interests')

        if len(q) < MIN_SEARCH_LENGTH:
            return User.objects.none()

        return (
            visibility.visible_users(me, discoverable_by='username')
            .filter(username__icontains=q)
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
        context.setdefault('relationships', {})
        return context

    def list(self, request, *args, **kwargs):
        results = list(self.filter_queryset(self.get_queryset()))
        context = self.get_serializer_context()
        # One relationship lookup for the whole page rather than per row.
        context['relationships'] = visibility.relationship_map(
            request.user, [u.pk for u in results]
        )
        serializer = self.get_serializer_class()(results, many=True, context=context)
        return Response(serializer.data)


class MeView(generics.RetrieveAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = UserProfileSerializer

    def get_object(self):
        return self.request.user


class DeleteAccountView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request):
        request.user.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)