from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from datetime import timedelta
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from users.models import User
from .models import Club, ClubEvent, ClubMember, ClubMessage
from .serializers import (
    ClubDetailSerializer, ClubEventNearbySerializer, ClubEventSerializer,
    ClubMemberSerializer, ClubMessageSerializer, ClubSerializer,
)


def _get_membership(club, user):
    try:
        return club.members.get(user=user)
    except ClubMember.DoesNotExist:
        return None


class ClubListCreateView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request):
        if request.query_params.get('joined') == 'true':
            qs = Club.objects.filter(is_active=True, members__user=request.user)
        else:
            qs = Club.objects.filter(is_active=True).exclude(members__user=request.user)

        interest_id = request.query_params.get('interest')
        if interest_id:
            qs = qs.filter(interest_id=interest_id)

        search = request.query_params.get('search')
        if search:
            qs = qs.filter(name__icontains=search)

        return Response(ClubSerializer(qs, many=True, context={'request': request}).data)

    def post(self, request):
        name = request.data.get('name', '').strip()
        if not name:
            return Response({'detail': 'name is required.'}, status=status.HTTP_400_BAD_REQUEST)

        if Club.objects.filter(name__iexact=name).exists():
            return Response({'detail': 'A club with this name already exists.'}, status=status.HTTP_400_BAD_REQUEST)

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
            )
            ClubMember.objects.create(user=request.user, club=club, role=ClubMember.Role.OWNER)

        return Response(ClubSerializer(club, context={'request': request}).data, status=status.HTTP_201_CREATED)


class ClubDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        club = get_object_or_404(Club, pk=pk)
        return Response(ClubDetailSerializer(club, context={'request': request}).data)

    def delete(self, request, pk):
        club = get_object_or_404(Club, pk=pk)
        if request.user != club.creator:
            return Response({'detail': 'Only the club creator can delete the club.'}, status=status.HTTP_403_FORBIDDEN)
        club.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ClubJoinView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        club = get_object_or_404(Club, pk=pk, is_active=True)
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
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk, user_id):
        club = get_object_or_404(Club, pk=pk, is_active=True)
        membership = _get_membership(club, request.user)
        if not membership or membership.role != ClubMember.Role.OWNER:
            return Response({'detail': 'Only owners can invite.'}, status=status.HTTP_403_FORBIDDEN)

        target = get_object_or_404(User, pk=user_id)
        if club.members.filter(user=target).exists():
            return Response({'detail': 'User is already a member.'}, status=status.HTTP_400_BAD_REQUEST)

        member = ClubMember.objects.create(user=target, club=club)
        return Response(ClubMemberSerializer(member).data, status=status.HTTP_201_CREATED)


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
        club = get_object_or_404(Club, pk=pk)
        members = club.members.select_related('user').order_by('joined_at')
        return Response(ClubMemberSerializer(members, many=True).data)


class ClubEventListCreateView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        club = get_object_or_404(Club, pk=pk)
        if not club.members.filter(user=request.user).exists():
            return Response({'detail': 'Members only.'}, status=status.HTTP_403_FORBIDDEN)
        events = club.events.filter(event_datetime__gte=timezone.now()).select_related('created_by', 'approved_by')
        return Response(ClubEventSerializer(events, many=True).data)

    def post(self, request, pk):
        club = get_object_or_404(Club, pk=pk)
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

        async_to_sync(get_channel_layer().group_send)(
            f'club_{club.pk}',
            {
                'type': 'club.message',
                'id': message.id,
                'sender_id': request.user.pk,
                'sender_username': request.user.username,
                'content': message.content,
                'message_type': ClubMessage.MessageType.MEETUP_PROPOSAL,
                'image_url': None,
                'event_id': event.id,
                'created_at': message.created_at.isoformat(),
            },
        )
        return Response(ClubEventSerializer(event).data, status=status.HTTP_201_CREATED)


class ClubEventApproveView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, event_id):
        event = get_object_or_404(ClubEvent.objects.select_related('club'), pk=event_id)
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

        async_to_sync(get_channel_layer().group_send)(
            f'club_{event.club_id}',
            {
                'type': 'club.event_status',
                'event_id': event.id,
                'status': ClubEvent.Status.APPROVED,
                'approved_at': event.approved_at.isoformat(),
                'expires_at': event.expires_at.isoformat(),
            },
        )
        return Response(ClubEventSerializer(event).data)


class ClubEventRejectView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, event_id):
        event = get_object_or_404(ClubEvent.objects.select_related('club'), pk=event_id)
        if not event.club.members.filter(user=request.user, role=ClubMember.Role.OWNER).exists():
            return Response({'detail': 'Only club owners can reject events.'}, status=status.HTTP_403_FORBIDDEN)
        if event.status == ClubEvent.Status.REJECTED:
            return Response({'detail': 'Event is already rejected.'}, status=status.HTTP_400_BAD_REQUEST)

        event.status = ClubEvent.Status.REJECTED
        event.save(update_fields=['status'])

        async_to_sync(get_channel_layer().group_send)(
            f'club_{event.club_id}',
            {
                'type': 'club.event_status',
                'event_id': event.id,
                'status': ClubEvent.Status.REJECTED,
                'approved_at': None,
                'expires_at': None,
            },
        )
        return Response(ClubEventSerializer(event).data)


class ClubEventNearbyView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
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
        club = get_object_or_404(Club, pk=pk)
        if not club.members.filter(user=request.user).exists():
            return Response({'detail': 'Members only.'}, status=status.HTTP_403_FORBIDDEN)

        qs = ClubMessage.objects.filter(club=club).select_related('sender').order_by('-created_at')
        before_id = request.query_params.get('before')
        if before_id:
            qs = qs.filter(id__lt=before_id)
        messages = list(reversed(qs[:50]))
        return Response(ClubMessageSerializer(messages, many=True).data)

    def post(self, request, pk):
        club = get_object_or_404(Club, pk=pk)
        if not club.members.filter(user=request.user).exists():
            return Response({'detail': 'Members only.'}, status=status.HTTP_403_FORBIDDEN)

        content = request.data.get('content', '').strip()
        image = request.FILES.get('image')
        if not content and not image:
            return Response({'detail': 'content or image is required.'}, status=status.HTTP_400_BAD_REQUEST)

        message_type = request.data.get('message_type', ClubMessage.MessageType.TEXT)
        if message_type not in ClubMessage.MessageType.values:
            message_type = ClubMessage.MessageType.TEXT

        message = ClubMessage.objects.create(
            club=club,
            sender=request.user,
            content=content,
            image=image,
            message_type=message_type,
        )
        return Response(ClubMessageSerializer(message).data, status=status.HTTP_201_CREATED)


class ClubEventDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, event_id):
        event = get_object_or_404(ClubEvent.objects.select_related('created_by', 'approved_by', 'club'), pk=event_id)
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
