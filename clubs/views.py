from django.db import transaction
from django.db.models import Count
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from users.models import User
from .models import Club, ClubEvent, ClubMember
from .serializers import ClubDetailSerializer, ClubEventSerializer, ClubMemberSerializer, ClubSerializer


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
        qs = qs.annotate(member_count=Count('members'))

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

        club.member_count = 1
        return Response(ClubSerializer(club, context={'request': request}).data, status=status.HTTP_201_CREATED)


class ClubDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        club = get_object_or_404(Club.objects.annotate(member_count=Count('members')), pk=pk)
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
        events = club.events.filter(event_datetime__gte=timezone.now()).select_related('created_by')
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

        event = ClubEvent.objects.create(
            club=club,
            created_by=request.user,
            title=title,
            description=request.data.get('description', '').strip(),
            location_name=request.data.get('location_name', '').strip(),
            latitude=request.data.get('latitude') or None,
            longitude=request.data.get('longitude') or None,
            event_datetime=event_datetime,
        )
        return Response(ClubEventSerializer(event).data, status=status.HTTP_201_CREATED)


class ClubEventDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, event_id):
        event = get_object_or_404(ClubEvent.objects.select_related('created_by', 'club'), pk=event_id)
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
