from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from datetime import timedelta
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from moderation import services as moderation_services
from moderation.models import ContentReport
from moderation.reasons import VISIBLE_STATES
from rest_framework import permissions, status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from notifications.utils import (
    notify_club_event_created, notify_club_event_decided, notify_club_invite,
    notify_club_message,
)
from regions import upstream
from users import visibility as user_visibility
from . import visibility
from .models import Club, ClubEvent, ClubInvitation, ClubMember, ClubMessage
from .serializers import (
    ClubDetailSerializer, ClubEventNearbySerializer, ClubEventSerializer,
    ClubInvitationSerializer, ClubMemberSerializer, ClubMessageCursorSerializer,
    ClubMessageInputSerializer, ClubMessageSerializer, ClubSerializer, PlaceSerializer,
    first_error, with_card_data,
)


#: Ceiling on one page of the club list, and the default page size.
#
# The endpoint used to return every matching club with no bound at all, which
# was survivable only because no city has many yet. It still answers with a bare
# JSON array — the app reads `Club[]` and changing that shape would break every
# build in the field — so paging is opt-in through `?limit=`/`?offset=` rather
# than a wrapper object. The default is set well above any real district's club
# count so that today nothing is cut off; see the audit note about moving the
# app onto the parameters before a city can plausibly exceed it.
LIST_MAX = 200


def _page(request):
    """`(limit, offset)` for the club list, clamped to sane bounds."""
    try:
        limit = min(max(int(request.query_params.get('limit', LIST_MAX)), 1), LIST_MAX)
    except (TypeError, ValueError):
        limit = LIST_MAX
    try:
        offset = max(int(request.query_params.get('offset', 0)), 0)
    except (TypeError, ValueError):
        offset = 0
    return limit, offset


def _get_membership(club, user):
    try:
        return club.members.get(user=user)
    except ClubMember.DoesNotExist:
        return None


def _out_of_area(place):
    """The answer for a club the requester may not have from where they are.

    A 404, and the same 404 a deleted club gives, so the endpoint cannot be
    used to ask whether a club exists somewhere else — the name is unique
    across the app, which makes that a real question rather than a theoretical
    one.

    What it does add is `location_status`, and that is a fact about the
    *requester*, not about any club: it is how the app knows to offer "turn on
    location" instead of "this club is gone". It is present only when we could
    not place them at all.
    """
    body = {'detail': 'Not found.'}
    if hasattr(place, 'as_payload'):
        body.update(place.as_payload())
        body['detail'] = 'Not found.'
    return Response(body, status=status.HTTP_404_NOT_FOUND)


def _accessible_club_or_none(pk, user, place, queryset=None, **filters):
    """A club this user may open, or None. Membership counts; see visibility."""
    qs = Club.objects.all() if queryset is None else queryset
    club = qs.filter(pk=pk, **filters).first()
    if club is None or not visibility.can_access(club, user, place):
        return None
    return club


def _invalid(serializer):
    """A 400 carrying DRF's field errors plus the single `detail` line the app shows."""
    return Response(
        {'detail': first_error(serializer.errors), **serializer.errors},
        status=status.HTTP_400_BAD_REQUEST,
    )


def _invitable_user(owner, user_id):
    """Whom `owner` may invite, or None; a missing, blocked or private account all read the same."""
    try:
        target = user_visibility.visible_users(owner).filter(pk=user_id, is_active=True).first()
    except (TypeError, ValueError, OverflowError):
        return None
    if target is None or target.discoverable_by_username:
        return target
    # Opted out of username search, so only people who already know them may invite them.
    rel = user_visibility.relationship(owner, target)
    return target if user_visibility.is_approved_viewer(rel) else None


class ClubListCreateView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request):
        """Discovery, and the joined list.

        The two branches differ by more than a filter. Discovery is narrowed to
        the requester's own area, because that is what discovery *means* now.
        The joined list is not narrowed at all: those are clubs this person
        belongs to, and belonging does not lapse because they went somewhere.
        """
        place = visibility.place_for(request.user)

        if request.query_params.get('joined') == 'true':
            qs = Club.objects.filter(is_active=True, members__user=request.user)
        else:
            qs = visibility.visible_to(
                Club.objects.filter(is_active=True), place,
            ).exclude(members__user=request.user)

        interest_id = request.query_params.get('interest')
        if interest_id:
            qs = qs.filter(interest_id=interest_id)

        # Search runs *inside* the visible set rather than beside it: a name
        # somebody types is not a key to a club in another city.
        search = request.query_params.get('search')
        if search:
            qs = qs.filter(name__icontains=search)

        # Newest first, and explicitly: an unordered queryset with a LIMIT on it
        # is free to return a different slice each time it is asked.
        limit, offset = _page(request)
        qs = with_card_data(qs, request.user).order_by('-created_at', '-pk')
        return Response(
            ClubSerializer(
                qs[offset:offset + limit], many=True, context={'request': request},
            ).data
        )

    def post(self, request):
        name = request.data.get('name', '').strip()
        if not name:
            return Response({'detail': 'name is required.'}, status=status.HTTP_400_BAD_REQUEST)

        if Club.objects.filter(name__iexact=name).exists():
            return Response({'detail': 'A club with this name already exists.'}, status=status.HTTP_400_BAD_REQUEST)

        level = (request.data.get('visibility_level') or '').strip().lower()
        if level not in Club.SELECTABLE:
            return Response(
                {
                    'detail': 'Choose how far this club should spread.',
                    'visibility_level': f'Must be one of: {", ".join(Club.SELECTABLE)}.',
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Resolved here, from the position the server holds, and never taken
        # from the request: a client that could name its own city could scope a
        # club to a city it is not in, which is the whole rule.
        try:
            place = visibility.place_for(request.user, allow_upstream=True)
        except upstream.UpstreamError as exc:
            return Response(
                {
                    'detail': 'Could not work out where you are right now. Please try again.',
                    'retry_after': getattr(exc, 'retry_after', None),
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        if not place.resolved:
            return Response(place.as_payload(), status=status.HTTP_400_BAD_REQUEST)

        if level not in visibility.available_levels(place):
            return Response(
                {
                    'detail': (
                        f'Approx has no {level} boundary for where you are, so a club '
                        f'cannot be limited to one. Pick a wider option.'
                    ),
                    'available_levels': visibility.available_levels(place),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # The screen sends back the tier ids it showed. They are not trusted for
        # anything — the scope comes from `place` either way — but disagreeing
        # with them means the user has moved since the picker was drawn, and
        # silently scoping the club to somewhere they are no longer standing is
        # worse than asking them to look again.
        claimed = request.data.get('visibility_place_id')
        if claimed and str(claimed) != str(getattr(place, f'{level}_id', '')):
            return Response(
                {
                    'detail': (
                        'Your location changed while you were filling this in. '
                        'Check the area and create the club again.'
                    ),
                    'location_changed': True,
                },
                status=status.HTTP_409_CONFLICT,
            )

        description = request.data.get('description', '').strip()
        interest_id = request.data.get('interest')
        emblem = request.FILES.get('emblem')

        with transaction.atomic():
            club = Club.objects.create(
                name=name,
                description=description,
                interest_id=interest_id or None,
                emblem=emblem,
                creator=request.user,
                **visibility.scope_fields(level, place),
            )
            ClubMember.objects.create(user=request.user, club=club, role=ClubMember.Role.OWNER)

        return Response(ClubSerializer(club, context={'request': request}).data, status=status.HTTP_201_CREATED)


class MyClubLocationView(APIView):
    """GET  /api/clubs/my-location/          — where the app thinks you are
    POST /api/clubs/my-location/           — work it out again, properly

    Two verbs because the two cost wildly different amounts. The GET answers
    from stored boundaries and never leaves the database, so the club screens
    can call it freely. The POST is allowed to ask OpenStreetMap, which is what
    the create screen does once — and what the "I have moved" button does — so
    the first person to open a club screen in a new city pays for everybody who
    follows them there.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        place = visibility.place_for(request.user)
        return Response(PlaceSerializer(place).data)

    def post(self, request):
        try:
            place = visibility.place_for(request.user, allow_upstream=True, force=True)
        except upstream.UpstreamError as exc:
            body = {'detail': 'Could not find your area right now. Please try again.'}
            retry_after = getattr(exc, 'retry_after', None)
            response = Response(body, status=status.HTTP_503_SERVICE_UNAVAILABLE)
            if retry_after:
                body['retry_after'] = retry_after
                response['Retry-After'] = str(retry_after)
            return response
        return Response(PlaceSerializer(place).data)


class ClubDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        # The direct-lookup hole this feature exists to close: without this, a
        # club hidden from every list is still one guessed id away.
        place = visibility.place_for(request.user)
        club = _accessible_club_or_none(
            pk, request.user, place,
            # The roster is nested in this response, so its users are fetched
            # here rather than one per member by the serializer.
            queryset=with_card_data(
                Club.objects.all(), request.user,
            ).prefetch_related('members__user'),
        )
        if club is None:
            return _out_of_area(place)
        return Response(ClubDetailSerializer(club, context={'request': request}).data)

    def delete(self, request, pk):
        # Deliberately not location-gated. Deleting is management, and a
        # creator on holiday is still the creator.
        club = get_object_or_404(Club, pk=pk)
        if request.user != club.creator:
            return Response({'detail': 'Only the club creator can delete the club.'}, status=status.HTTP_403_FORBIDDEN)
        club.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ClubJoinView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        # Joining needs `can_see`, not `can_access`: membership is what turns
        # the location rule off, so it cannot also be what gets you past it.
        place = visibility.place_for(request.user)
        club = Club.objects.filter(pk=pk, is_active=True).first()
        if club is None or not visibility.can_see(club, place):
            if club is not None and visibility.is_member(club, request.user):
                return Response({'detail': 'Already a member.'}, status=status.HTTP_400_BAD_REQUEST)
            return _out_of_area(place)
        if club.members.filter(user=request.user).exists():
            return Response({'detail': 'Already a member.'}, status=status.HTTP_400_BAD_REQUEST)
        member = ClubMember.objects.create(user=request.user, club=club)
        return Response(ClubMemberSerializer(member).data, status=status.HTTP_201_CREATED)


class ClubLeaveView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        club = get_object_or_404(Club, pk=pk)
        membership = _get_membership(club, request.user)
        if not membership:
            return Response({'detail': 'Not a member.'}, status=status.HTTP_400_BAD_REQUEST)

        if request.user == club.creator:
            return Response(
                {'detail': 'Creator cannot leave. Delete the club or transfer creator status first.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        membership.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ClubInviteView(APIView):
    """Ask someone to join a club. Membership only ever comes from their acceptance."""
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = 'social_request'

    def post(self, request, pk, user_id):
        # Not location-gated: the owner vouches, the invitee consents, and membership then outranks location.
        club = get_object_or_404(Club, pk=pk, is_active=True)
        membership = _get_membership(club, request.user)
        if not membership or membership.role != ClubMember.Role.OWNER:
            return Response({'detail': 'Only owners can invite.'}, status=status.HTTP_403_FORBIDDEN)

        target = _invitable_user(request.user, user_id)
        if target is None:
            return user_visibility.not_found()
        if club.members.filter(user=target).exists():
            return Response({'detail': 'User is already a member.'}, status=status.HTTP_400_BAD_REQUEST)

        # One row per person per club, as with a Hi: repeats keep a pending or declined answer and never re-notify.
        with transaction.atomic():
            invitation, created = ClubInvitation.objects.get_or_create(
                club=club, receiver=target, defaults={'sender': request.user},
            )
            # Accepted before but no longer a member means they left; coming back needs a fresh yes.
            if not created and invitation.status == ClubInvitation.Status.ACCEPTED:
                created = bool(ClubInvitation.objects.filter(
                    pk=invitation.pk, status=ClubInvitation.Status.ACCEPTED,
                ).update(
                    status=ClubInvitation.Status.PENDING, sender=request.user,
                    responded_at=None, created_at=timezone.now(),
                ))
                invitation.refresh_from_db()
            if created:
                transaction.on_commit(lambda: notify_club_invite(invitation))
        return Response(
            ClubInvitationSerializer(invitation).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class ClubInvitationListView(APIView):
    """The club invitations waiting on the requester's answer."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        invitations = (
            ClubInvitation.objects.pending_for(request.user)
            .select_related('club', 'sender', 'receiver')
            .order_by('-created_at')
        )
        return Response(ClubInvitationSerializer(invitations, many=True).data)


class ClubInvitationAcceptView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        with transaction.atomic():
            # The conditional update is the claim: a repeat, a decline or a block since the invite all lose it.
            claimed = ClubInvitation.objects.pending_for(request.user).filter(pk=pk).update(
                status=ClubInvitation.Status.ACCEPTED, responded_at=timezone.now(),
            )
            if not claimed:
                return user_visibility.not_found()
            club = Club.objects.get(invitations__pk=pk)
            member, _ = ClubMember.objects.get_or_create(user=request.user, club=club)
        return Response(ClubMemberSerializer(member).data, status=status.HTTP_201_CREATED)


class ClubInvitationDeclineView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        declined = ClubInvitation.objects.pending_for(request.user).filter(pk=pk).update(
            status=ClubInvitation.Status.DECLINED, responded_at=timezone.now(),
        )
        if not declined:
            return user_visibility.not_found()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ClubKickView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, pk, user_id):
        club = get_object_or_404(Club, pk=pk)
        acting = _get_membership(club, request.user)
        if not acting or acting.role != ClubMember.Role.OWNER:
            return Response({'detail': 'Only owners can kick members.'}, status=status.HTTP_403_FORBIDDEN)

        target_membership = get_object_or_404(ClubMember, club=club, user_id=user_id)

        if target_membership.user == request.user:
            return Response({'detail': 'Use the leave endpoint to leave the club.'}, status=status.HTTP_400_BAD_REQUEST)

        if target_membership.user == club.creator:
            return Response({'detail': 'The club creator cannot be kicked.'}, status=status.HTTP_403_FORBIDDEN)

        if target_membership.role == ClubMember.Role.OWNER and request.user != club.creator:
            return Response({'detail': 'Only the club creator can kick another owner.'}, status=status.HTTP_403_FORBIDDEN)

        target_membership.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ClubPromoteOwnerView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, pk, user_id):
        club = get_object_or_404(Club, pk=pk)
        acting = _get_membership(club, request.user)
        if not acting or acting.role != ClubMember.Role.OWNER:
            return Response({'detail': 'Only owners can promote members.'}, status=status.HTTP_403_FORBIDDEN)

        target_membership = get_object_or_404(ClubMember, club=club, user_id=user_id)
        if target_membership.role == ClubMember.Role.OWNER:
            return Response({'detail': 'User is already an owner.'}, status=status.HTTP_400_BAD_REQUEST)

        target_membership.role = ClubMember.Role.OWNER
        target_membership.save(update_fields=['role'])
        return Response(ClubMemberSerializer(target_membership).data)


class ClubMembersView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        # Same gate as the club page it belongs to, or the roster becomes the
        # way to read a hidden club's contents.
        place = visibility.place_for(request.user)
        club = _accessible_club_or_none(pk, request.user, place)
        if club is None:
            return _out_of_area(place)
        members = club.members.select_related('user').order_by('joined_at')
        return Response(ClubMemberSerializer(members, many=True).data)


class ClubEventListCreateView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        # Two gates in order, and the order is the point: somewhere they cannot
        # see is "not found", somewhere they can see but have not joined is
        # "members only". Reversing them would let a stranger tell a hidden
        # club apart from a missing one by the wording of the refusal.
        place = visibility.place_for(request.user)
        club = _accessible_club_or_none(pk, request.user, place)
        if club is None:
            return _out_of_area(place)
        if not club.members.filter(user=request.user).exists():
            return Response({'detail': 'Members only.'}, status=status.HTTP_403_FORBIDDEN)
        events = club.events.filter(event_datetime__gte=timezone.now()).select_related('created_by', 'approved_by')
        return Response(ClubEventSerializer(events, many=True).data)

    def post(self, request, pk):
        place = visibility.place_for(request.user)
        club = _accessible_club_or_none(pk, request.user, place)
        if club is None:
            return _out_of_area(place)
        if not club.members.filter(user=request.user).exists():
            return Response({'detail': 'Must be a club member to create events.'}, status=status.HTTP_403_FORBIDDEN)

        title = request.data.get('title', '').strip()
        if not title:
            return Response({'detail': 'title is required.'}, status=status.HTTP_400_BAD_REQUEST)

        event_datetime = request.data.get('event_datetime')
        if not event_datetime:
            return Response({'detail': 'event_datetime is required.'}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            event = ClubEvent.objects.create(
                club=club,
                created_by=request.user,
                title=title,
                description=request.data.get('description', '').strip(),
                location_name=request.data.get('location_name', '').strip(),
                latitude=request.data.get('latitude') or None,
                longitude=request.data.get('longitude') or None,
                event_datetime=event_datetime,
                status=ClubEvent.Status.PENDING,
            )
            message = ClubMessage.objects.create(
                club=club,
                sender=request.user,
                content=f'Proposed a meetup: {title}',
                message_type=ClubMessage.MessageType.MEETUP_PROPOSAL,
                event=event,
            )

        # Outside the atomic block above: a failed push must not roll back
        # the meetup it is announcing, and the alert rows are written against
        # an event that is already committed.
        notify_club_event_created(event)

        msg = ClubMessage.objects.select_related(
            'sender', 'event__created_by', 'event__approved_by',
        ).get(pk=message.pk)
        async_to_sync(get_channel_layer().group_send)(
            f'club_{club.pk}',
            {
                'type': 'club.message',
                'message_data': dict(ClubMessageSerializer(msg).data),
            },
        )
        return Response(ClubEventSerializer(event).data, status=status.HTTP_201_CREATED)


class ClubEventApproveView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, event_id):
        event = get_object_or_404(ClubEvent.objects.select_related('club', 'created_by'), pk=event_id)
        if not event.club.members.filter(user=request.user, role=ClubMember.Role.OWNER).exists():
            return Response({'detail': 'Only club owners can approve events.'}, status=status.HTTP_403_FORBIDDEN)
        if event.status == ClubEvent.Status.APPROVED:
            return Response({'detail': 'Event is already approved.'}, status=status.HTTP_400_BAD_REQUEST)

        now = timezone.now()
        event.status = ClubEvent.Status.APPROVED
        event.approved_by = request.user
        event.approved_at = now
        event.expires_at = now + timedelta(hours=24)
        event.save(update_fields=['status', 'approved_by', 'approved_at', 'expires_at'])

        notify_club_event_decided(event, request.user, approved=True)

        async_to_sync(get_channel_layer().group_send)(
            f'club_{event.club_id}',
            {
                'type': 'club.event_status',
                'event_id': event.id,
                'status': ClubEvent.Status.APPROVED,
                'approved_at': event.approved_at.isoformat(),
                'expires_at': event.expires_at.isoformat(),
                'proposal': dict(ClubEventSerializer(event).data),
            },
        )
        return Response(ClubEventSerializer(event).data)


class ClubEventRejectView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, event_id):
        event = get_object_or_404(ClubEvent.objects.select_related('club', 'created_by'), pk=event_id)
        if not event.club.members.filter(user=request.user, role=ClubMember.Role.OWNER).exists():
            return Response({'detail': 'Only club owners can reject events.'}, status=status.HTTP_403_FORBIDDEN)
        if event.status == ClubEvent.Status.REJECTED:
            return Response({'detail': 'Event is already rejected.'}, status=status.HTTP_400_BAD_REQUEST)

        event.status = ClubEvent.Status.REJECTED
        event.save(update_fields=['status'])

        notify_club_event_decided(event, request.user, approved=False)

        async_to_sync(get_channel_layer().group_send)(
            f'club_{event.club_id}',
            {
                'type': 'club.event_status',
                'event_id': event.id,
                'status': ClubEvent.Status.REJECTED,
                'approved_at': None,
                'expires_at': None,
                'proposal': dict(ClubEventSerializer(event).data),
            },
        )
        return Response(ClubEventSerializer(event).data)


class ClubEventNearbyView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        # The map's club pins. Already scoped to clubs this user belongs to,
        # which is the membership rule doing the work — there is no path here
        # to a club they could not open, so no second gate is needed.
        now = timezone.now()
        events = (
            ClubEvent.objects
            .filter(
                status=ClubEvent.Status.APPROVED,
                expires_at__gt=now,
                club__members__user=request.user,
            )
            .select_related('club', 'created_by', 'approved_by')
            .order_by('event_datetime')
        )
        return Response(ClubEventNearbySerializer(events, many=True).data)


class ClubMessageListView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request, pk):
        place = visibility.place_for(request.user)
        club = _accessible_club_or_none(pk, request.user, place)
        if club is None:
            return _out_of_area(place)
        if not club.members.filter(user=request.user).exists():
            return Response({'detail': 'Members only.'}, status=status.HTTP_403_FORBIDDEN)

        # Quarantined and removed messages leave the history for everybody but
        # the person who wrote them, who keeps seeing their own so the app does
        # not look like it silently dropped the message.
        qs = (
            ClubMessage.objects.filter(club=club)
            .filter(
                Q(moderation__in=VISIBLE_STATES) | Q(sender_id=request.user.pk),
            )
            .select_related('sender')
            .order_by('-created_at')
        )
        cursor = ClubMessageCursorSerializer(data=request.query_params)
        if not cursor.is_valid():
            return _invalid(cursor)
        before_id = cursor.validated_data.get('before')
        if before_id is not None:
            qs = qs.filter(id__lt=before_id)
        messages = list(reversed(qs[:50]))
        return Response(ClubMessageSerializer(messages, many=True).data)

    def post(self, request, pk):
        place = visibility.place_for(request.user)
        club = _accessible_club_or_none(pk, request.user, place)
        if club is None:
            return _out_of_area(place)
        if not club.members.filter(user=request.user).exists():
            return Response({'detail': 'Members only.'}, status=status.HTTP_403_FORBIDDEN)

        payload = ClubMessageInputSerializer(data=request.data)
        if not payload.is_valid():
            return _invalid(payload)
        content = payload.validated_data['content']
        message_type = payload.validated_data['message_type']
        image = request.FILES.get('image')
        if image is not None:
            from core.uploads import validate_image
            image = validate_image(image)
        if not content and not image:
            return Response({'detail': 'content or image is required.'}, status=status.HTTP_400_BAD_REQUEST)

        # A club is semi-public - anyone who joins reads the whole history -
        # so club text takes the same screening story captions and comments do.
        verdict = moderation_services.screen(content, raise_on_block=False)
        if verdict.blocked:
            return Response(
                {'detail': verdict.message(), 'code': 'objectionable_content'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        message = ClubMessage.objects.create(
            club=club,
            sender=request.user,
            content=content,
            image=image,
            message_type=message_type,
            moderation=moderation_services.moderation_for(verdict),
        )
        if verdict.flagged:
            moderation_services.record_automated_flag(
                message, ContentReport.Target.CLUB_MESSAGE, verdict, author=request.user,
            )

        # The consumer pushes for socket-sent messages; this is the same
        # announcement for the ones that come this way. `notify_club_message`
        # holds one cooldown per member per club, so a photo posted straight
        # after a line of text does not arrive as a second buzz.
        notify_club_message(club, request.user, content, image=image)

        # Socket-sent messages are broadcast by the consumer; REST-sent ones
        # (photos) previously reached other members only on their next fetch.
        data = ClubMessageSerializer(message).data
        async_to_sync(get_channel_layer().group_send)(
            f'club_{club.pk}',
            {'type': 'club.message', 'message_data': dict(data)},
        )
        return Response(data, status=status.HTTP_201_CREATED)


class ClubEventDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, event_id):
        # An event is a club's business, so it inherits the club's gate. Without
        # this, a meetup carries the name, the place and the time of a club the
        # requester is not allowed to know about.
        event = get_object_or_404(ClubEvent.objects.select_related('created_by', 'approved_by', 'club'), pk=event_id)
        place = visibility.place_for(request.user)
        if not visibility.can_access(event.club, request.user, place):
            return _out_of_area(place)
        return Response(ClubEventSerializer(event).data)

    def delete(self, request, event_id):
        event = get_object_or_404(ClubEvent, pk=event_id)
        is_owner = event.club.members.filter(user=request.user, role=ClubMember.Role.OWNER).exists()
        if event.created_by != request.user and not is_owner:
            return Response(
                {'detail': 'Only the event creator or a club owner can delete events.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        event.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
