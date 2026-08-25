from django.db.models import Max, Q
from django.utils import timezone
from rest_framework import generics, permissions, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.serializers import (
    TokenObtainPairSerializer, TokenRefreshSerializer,
)
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from friends.models import FriendRequest
from moderation.views import ReportUserView
from notifications.utils import notify_new_device
from . import applemusic, socialauth, terms, visibility
from .links import LinkError, MAX_LINKS_PER_USER, normalize_link
from .serializers import (
    RegisterSerializer, UserProfileSerializer, EditProfileSerializer,
    NotificationSettingsSerializer, PublicProfileSerializer,
    PrivacySettingsSerializer, ProfileLinkSerializer,
    UserSearchSerializer, ZoneUpdateSerializer, serialize_links,
)
from .models import Device, ProfileLink, User


class SafeTokenRefreshSerializer(TokenRefreshSerializer):
    """`TokenRefreshSerializer.validate()` (simplejwt 5.5.1) calls
    `User.objects.get()` on the refresh token's user id with no try/except —
    a token that has outlived its account (deleted after the token was
    issued, which a test/reset cycle does routinely) crashes with an
    uncaught 500 instead of the clean 401 the code clearly means to produce
    right below that line. Same client-facing outcome as an expired token
    either way: sign in again.
    """

    def validate(self, attrs):
        try:
            return super().validate(attrs)
        except User.DoesNotExist as exc:
            raise InvalidToken('No account matches this token anymore.') from exc


class SafeTokenRefreshView(TokenRefreshView):
    serializer_class = SafeTokenRefreshSerializer


class SuspensionAwareTokenObtainPairSerializer(TokenObtainPairSerializer):
    """Password login, refused for a suspended account with the reason attached.

    `users.authentication` would stop them a moment later anyway - every
    authenticated request re-reads the suspension - but a token that is issued
    and then rejected on first use looks to the app like a broken session
    rather than a ban. Saying so at the door is the honest version, and it is
    the only place the reason and the end date can be shown.
    """

    def validate(self, attrs):
        data = super().validate(attrs)
        detail = self.user.suspension_detail()
        if detail is not None:
            raise PermissionDenied(detail)
        return data


class SuspensionAwareTokenObtainPairView(TokenObtainPairView):
    serializer_class = SuspensionAwareTokenObtainPairSerializer


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


class SocialLoginView(APIView):
    """Sign in — or sign up — with Google or Apple.

    One endpoint for both, and for both halves of the flow, because from here
    they are the same request: a provider identity token comes in, an Aura
    session goes out. Whether that meant creating a row or finding one is an
    outcome, not a different call — the app cannot know in advance which it
    will be, and a client that had to guess would get it wrong for exactly the
    people who already had an account.

    Rate limiting is deliberately absent: every path through here is gated on a
    signature from Google or Apple, so there is nothing to guess at and nothing
    to enumerate.
    """
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        provider = str(request.data.get('provider', '')).lower().strip()
        token = request.data.get('id_token') or request.data.get('identity_token')
        full_name = str(request.data.get('full_name', ''))[:100]
        # The app puts the agreement in front of the Google and Apple buttons,
        # not after them, so a first-time sign-in arrives already carrying it.
        # Absent is not refused here the way it is on `RegisterView`: this same
        # endpoint is how *existing* accounts sign in, and refusing them would
        # lock out everyone who predates the gate. They come back with
        # `needs_terms` instead and meet it on the next screen.
        accepted = bool(request.data.get('accept_terms'))

        try:
            claims = socialauth.verify_identity_token(provider, token)
            user, created = socialauth.resolve_user(provider, claims, full_name)
        except socialauth.SocialAuthError as exc:
            return Response(
                {'detail': str(exc), 'code': exc.code}, status=exc.status
            )

        # A brand-new account that did not carry agreement has no business
        # existing, so this is the one case that is undone rather than gated.
        if created and not accepted:
            user.delete()
            return Response(
                {
                    'detail': 'You must agree to the Terms of Use and '
                              'Community Guidelines to create an account.',
                    'code': 'terms_required',
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if accepted and not terms.has_accepted(user):
            terms.record_acceptance(user)

        suspended = user.suspension_detail()
        if suspended is not None:
            return Response(suspended, status=status.HTTP_403_FORBIDDEN)

        refresh = RefreshToken.for_user(user)
        return Response({
            'user': UserProfileSerializer(user).data,
            'tokens': {
                'refresh': str(refresh),
                'access': str(refresh.access_token),
            },
            'created': created,
            # Not `created`, though it usually agrees with it. The age gate is
            # about whether we know how old this person is, and someone whose
            # first sign-in was interrupted before they answered comes back as
            # an existing account that still owes us the answer.
            'needs_date_of_birth': user.date_of_birth is None,
            # Same shape, same reason, for the agreement.
            'needs_terms': not terms.has_accepted(user),
        })


class SocialLinkView(APIView):
    """Attach a Google or Apple identity to the *signed-in* account.

    A different question to SocialLoginView's, even though both start from the
    same provider token: that one has no account yet and has to find or make
    one, which is the one place email-matching belongs. Here the account is
    already known — `request.user` — so linking never touches email at all;
    see socialauth.link_identity for why that distinction matters.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        provider = str(request.data.get('provider', '')).lower().strip()
        token = request.data.get('id_token') or request.data.get('identity_token')

        try:
            claims = socialauth.verify_identity_token(provider, token)
            socialauth.link_identity(request.user, provider, claims)
        except socialauth.SocialAuthError as exc:
            return Response(
                {'detail': str(exc), 'code': exc.code}, status=exc.status
            )

        return Response(UserProfileSerializer(request.user).data)


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
        #
        # The device row is emptied rather than deleted, and only this one:
        # signing out of a phone must not silence the tablet that is still
        # signed in, and keeping the row means signing back in on this phone
        # is recognised rather than announced as a new device. A client too old
        # to send `install_id` falls through to the column below, which is the
        # only thing it ever registered.
        install_id = str(request.data.get('install_id') or '').strip()
        if install_id:
            Device.objects.filter(
                user=request.user, install_id=install_id,
            ).update(push_token='')

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


class DeviceRegisterView(APIView):
    """Announce this installation, and its current push token.

    Called on every launch once signed in, not only on the sign-in itself: the
    push token is reissued by Expo on its own schedule, and `last_seen` is what
    makes a device list readable.

    The account's own security notice comes from here (see
    `notifications.utils.notify_new_device`) — a row appearing for an account
    that already had devices means this account was just signed in to somewhere
    new. `get_or_create` is what draws that line: an install that has been seen
    before updates in place and says nothing, so a token reissue or a relaunch
    is silent and only a genuinely new device is announced.
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        install_id = str(request.data.get('install_id') or '').strip()
        if not install_id:
            return Response(
                {'detail': 'install_id is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # The client generates this and it is never shown to anyone, so the
        # only thing worth enforcing is that it fits the column.
        install_id = install_id[:64]

        platform = str(request.data.get('platform') or '').strip().lower()
        if platform not in Device.Platform.values:
            platform = Device.Platform.UNKNOWN

        device, created = Device.objects.get_or_create(
            user=request.user,
            install_id=install_id,
            defaults={
                'push_token': str(request.data.get('push_token') or '')[:200],
                'name': str(request.data.get('name') or '')[:120],
                'platform': platform,
            },
        )

        if not created:
            # A name or token that arrives empty is "unchanged", not "cleared":
            # push permission may simply not have been granted this launch, and
            # dropping a token we already hold would silence a working device.
            token = str(request.data.get('push_token') or '')[:200]
            name = str(request.data.get('name') or '')[:120]
            if token:
                device.push_token = token
            if name:
                device.name = name
            if platform != Device.Platform.UNKNOWN:
                device.platform = platform
            # `last_seen` is auto_now, so saving at all moves it.
            device.save(update_fields=['push_token', 'name', 'platform', 'last_seen'])

        if created:
            notify_new_device(device)

        return Response(
            {'install_id': device.install_id, 'created': created},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


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


class NotificationSettingsView(APIView):
    """Read and write which kinds of notification this user wants.

    Deliberately separate from the OS permission, which the app manages
    through Settings → Permissions. This says what to send to someone who has
    already agreed to be reachable; that says whether anything may reach them
    at all. Turning everything off here is not the same as revoking the
    permission, and a user who does the latter still keeps these choices for
    when they change their mind.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response(NotificationSettingsSerializer(request.user).data)

    def patch(self, request):
        serializer = NotificationSettingsSerializer(
            request.user, data=request.data, partial=True,
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


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

        # Pausing is the strongest statement on this screen — "I am not out
        # there at all" — so it forgets the position already on file rather than
        # only stopping new ones. Pausing used to leave the last reported
        # position sitting in the row, which meant the map stopped drawing you
        # but the database still knew exactly where you had been.
        #
        # Motion goes with it: a speed trail is drawn from a published position,
        # so leaving one behind would outlive the position it belongs to.
        #
        # Done here, on the server, so a client that never gets the memo — or
        # never runs again — changes nothing about the outcome.
        if (
            user.location_sharing != previous_sharing
            and user.location_sharing == User.LocationSharing.PAUSED
        ):
            user.latitude = None
            user.longitude = None
            user.location_updated_at = None
            user.location_accuracy_m = None
            user.motion_class = User.MotionClass.STATIONARY
            user.motion_updated_at = None
            user.save(update_fields=[
                'latitude', 'longitude', 'location_updated_at', 'location_accuracy_m',
                'motion_class', 'motion_updated_at',
            ] + user.end_sharing_session())

        # Going visible ties this run of the app to the choice that was just
        # made. Minted on every transition off Paused rather than only the
        # first, so a user who pauses and resumes gets a new token and the old
        # one can never be replayed to keep them visible. See
        # `User.sharing_session`.
        if (
            user.location_sharing != previous_sharing
            and user.location_sharing != User.LocationSharing.PAUSED
        ):
            user.save(update_fields=user.begin_sharing_session())

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


class AppleMusicStatusView(APIView):
    """Is Apple Music usable, and if not, precisely why.

    Exists so the answer does not have to be inferred from a failed search.
    It reports which variables are missing by name, and — when they are all
    present — actually mints a token and asks Apple, because a key that is set
    and rejected looks identical to a working one from the outside.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        absent = applemusic.missing_settings()
        if absent:
            return Response({
                'configured': False,
                'code': 'not_configured',
                'missing': list(absent),
                'detail': f"Not set on this server: {', '.join(absent)}.",
            })
        try:
            # One real search, because signing succeeding proves nothing about
            # whether Apple will accept the result.
            applemusic.search_songs('a', limit=1)
        except applemusic.AppleMusicError as exc:
            return Response({
                'configured': False,
                'code': exc.code,
                'missing': [],
                'detail': str(exc),
            })
        return Response({'configured': True, 'code': 'ok', 'missing': [], 'detail': ''})


class AppleMusicSearchView(APIView):
    """Search the Apple Music catalogue on the caller's behalf.

    Authenticated, because it spends this server's Apple credentials: an open
    proxy to a rate-limited third-party API is someone else's free search
    backend. Two characters minimum for the same reason.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        term = (request.query_params.get('q') or '').strip()
        if len(term) < MIN_SEARCH_LENGTH:
            return Response([])
        try:
            songs = applemusic.search_songs(
                term,
                storefront=request.query_params.get('storefront'),
                limit=request.query_params.get('limit') or 25,
            )
        except applemusic.AppleMusicError as exc:
            # `code` travels with the message: the app picks its wording from
            # the code and shows the message underneath, so a misconfiguration
            # reads the same in the UI as it does in the logs.
            return Response({'detail': str(exc), 'code': exc.code}, status=exc.status)
        return Response(songs)


class ZoneAppleMusicView(APIView):
    """The Apple track a zone is playing: set it by id, or clear it.

    POST takes `{song_id}` and nothing else that matters. Title, artist,
    artwork and the preview URL are read back from Apple here — a client that
    could name its own preview URL could point every listener's player at any
    file on the internet.
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        try:
            song = applemusic.get_song(
                request.data.get('song_id') or request.data.get('id'),
                storefront=request.data.get('storefront'),
            )
        except applemusic.AppleMusicError as exc:
            return Response({'detail': str(exc), 'code': exc.code}, status=exc.status)

        user = request.user
        user.zone_apple_id = song['id']
        user.zone_apple_title = song['title'][:200]
        user.zone_apple_artist = song['artist'][:200]
        user.zone_apple_artwork = song['artwork'][:500]
        user.zone_apple_preview_url = song['preview_url'][:500]
        # A zone plays one thing. The upload is the other way to fill this
        # slot, and it loses — the same rule ZoneUpdate applies in the other
        # direction.
        user.zone_audio = None
        # A new song means a new loop — "seconds 12 to 24" was chosen against a
        # different piece of audio and is meaningless here, and on a shorter
        # preview it would point past the end.
        user.zone_track_start_ms = 0
        user.zone_track_end_ms = None
        user.save(update_fields=[
            'zone_apple_id', 'zone_apple_title', 'zone_apple_artist',
            'zone_apple_artwork', 'zone_apple_preview_url',
            'zone_audio',
            'zone_track_start_ms', 'zone_track_end_ms', 'updated_at',
        ])
        return Response(ZoneUpdateSerializer(user).data)

    def delete(self, request):
        user = request.user
        user.zone_apple_id = ''
        user.zone_apple_title = ''
        user.zone_apple_artist = ''
        user.zone_apple_artwork = ''
        user.zone_apple_preview_url = ''
        user.save(update_fields=[
            'zone_apple_id', 'zone_apple_title', 'zone_apple_artist',
            'zone_apple_artwork', 'zone_apple_preview_url', 'updated_at',
        ])
        return Response(ZoneUpdateSerializer(user).data)


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


# `POST /api/users/<id>/report/` is the same view `moderation.urls` serves at
# `/api/moderation/report/user/<id>/`, not a second implementation. It lives at
# both paths because the app already had the users-prefixed one wired into the
# profile menu (against nothing - see the audit), and because a report route
# next to the profile route is where the next person will look for it.
UserReportView = ReportUserView


class TermsView(APIView):
    """What the sign-up gate renders: the current version, and where to read it.

    Unauthenticated on purpose — the gate is shown *before* anybody has an
    account, so it cannot be behind one. Serving the URLs from here rather than
    baking them into the app means a change of hosting is a deploy, not an App
    Store release.
    """

    permission_classes = [permissions.AllowAny]

    def get(self, request):
        return Response(terms.current_terms())


class TermsAcceptView(APIView):
    """Record that the signed-in account agreed to the agreement as it stands.

    This is the path for accounts that already existed when the gate landed,
    and for anybody meeting a new version of the text. It never accepts a
    version from the client — `users.terms` decides which document was in force
    at the moment of the tap, so an old build cannot record agreement to a
    document nobody was shown.
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        if not request.data.get('accept'):
            return Response(
                {'detail': 'You must agree to continue.', 'code': 'terms_required'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        terms.record_acceptance(request.user)
        return Response({
            'terms_version': request.user.terms_version,
            'terms_accepted_at': request.user.terms_accepted_at.isoformat(),
            'needs_terms': False,
        })


class DeleteAccountView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request):
        request.user.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)