from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from rest_framework import permissions, status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from users.models import User
from .models import Club, ClubMember
from .serializers import BasicUserSerializer, ClubDetailSerializer, ClubMemberSerializer, ClubSerializer


def _get_membership(club, user):
    try:
        return club.members.get(user=user)
    except ClubMember.DoesNotExist:
        return None


def _handle_president_leave(club, leaving_member):
    """Promote VP or oldest member, or delete club if alone."""
    vp = club.members.filter(role=ClubMember.Role.VICE).exclude(user=leaving_member.user).first()
    if vp:
        vp.role = ClubMember.Role.PRESIDENT
        vp.save(update_fields=['role'])
        return

    next_president = (
        club.members.exclude(user=leaving_member.user)
        .order_by('joined_at')
        .first()
    )
    if next_president:
        next_president.role = ClubMember.Role.PRESIDENT
        next_president.save(update_fields=['role'])
    else:
        club.delete()


class ClubListCreateView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request):
        qs = Club.objects.filter(is_active=True).annotate(member_count=Count('members'))

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

        if ClubMember.objects.filter(user=request.user, role=ClubMember.Role.PRESIDENT).exists():
            return Response({'detail': 'You are already president of another club.'}, status=status.HTTP_400_BAD_REQUEST)

        description = request.data.get('description', '').strip()
        interest_id = request.data.get('interest')
        emblem = request.FILES.get('emblem')

        with transaction.atomic():
            club = Club.objects.create(
                name=name,
                description=description,
                interest_id=interest_id or None,
                emblem=emblem,
            )
            ClubMember.objects.create(user=request.user, club=club, role=ClubMember.Role.PRESIDENT)

        club.member_count = 1
        return Response(ClubSerializer(club, context={'request': request}).data, status=status.HTTP_201_CREATED)


class ClubDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        club = get_object_or_404(Club.objects.annotate(member_count=Count('members')), pk=pk)
        return Response(ClubDetailSerializer(club, context={'request': request}).data)


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

        with transaction.atomic():
            if membership.role == ClubMember.Role.PRESIDENT:
                _handle_president_leave(club, membership)
                # Club may have been deleted in the handler
                try:
                    Club.objects.get(pk=pk)
                except Club.DoesNotExist:
                    return Response(status=status.HTTP_204_NO_CONTENT)

            membership.delete()

        return Response(status=status.HTTP_204_NO_CONTENT)


class ClubInviteView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk, user_id):
        club = get_object_or_404(Club, pk=pk, is_active=True)
        membership = _get_membership(club, request.user)
        if not membership or membership.role not in (ClubMember.Role.PRESIDENT, ClubMember.Role.VICE):
            return Response({'detail': 'Only president or VP can invite.'}, status=status.HTTP_403_FORBIDDEN)

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
        if not acting or acting.role not in (ClubMember.Role.PRESIDENT, ClubMember.Role.VICE):
            return Response({'detail': 'Only president or VP can kick members.'}, status=status.HTTP_403_FORBIDDEN)

        target_membership = get_object_or_404(ClubMember, club=club, user_id=user_id)

        if target_membership.user == request.user:
            return Response({'detail': 'Use the leave endpoint to leave the club.'}, status=status.HTTP_400_BAD_REQUEST)

        # VP cannot kick the president
        if acting.role == ClubMember.Role.VICE and target_membership.role == ClubMember.Role.PRESIDENT:
            return Response({'detail': 'VP cannot kick the president.'}, status=status.HTTP_403_FORBIDDEN)

        target_membership.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ClubPromoteView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, pk, user_id):
        club = get_object_or_404(Club, pk=pk)
        acting = _get_membership(club, request.user)
        if not acting or acting.role != ClubMember.Role.PRESIDENT:
            return Response({'detail': 'Only the president can promote members.'}, status=status.HTTP_403_FORBIDDEN)

        target_membership = get_object_or_404(ClubMember, club=club, user_id=user_id)
        if target_membership.user == request.user:
            return Response({'detail': 'Cannot promote yourself.'}, status=status.HTTP_400_BAD_REQUEST)

        if target_membership.role == ClubMember.Role.VICE:
            return Response({'detail': 'User is already VP.'}, status=status.HTTP_400_BAD_REQUEST)

        # Demote any existing VP first
        club.members.filter(role=ClubMember.Role.VICE).update(role=ClubMember.Role.MEMBER)

        target_membership.role = ClubMember.Role.VICE
        target_membership.save(update_fields=['role'])
        return Response(ClubMemberSerializer(target_membership).data)


class ClubTransferView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, pk, user_id):
        club = get_object_or_404(Club, pk=pk)
        acting = _get_membership(club, request.user)
        if not acting or acting.role != ClubMember.Role.PRESIDENT:
            return Response({'detail': 'Only the president can transfer presidency.'}, status=status.HTTP_403_FORBIDDEN)

        target_membership = get_object_or_404(ClubMember, club=club, user_id=user_id)
        if target_membership.user == request.user:
            return Response({'detail': 'You are already president.'}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            acting.role = ClubMember.Role.MEMBER
            acting.save(update_fields=['role'])
            target_membership.role = ClubMember.Role.PRESIDENT
            target_membership.save(update_fields=['role'])

        return Response(ClubMemberSerializer(target_membership).data)


class ClubMembersView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        club = get_object_or_404(Club, pk=pk)
        members = club.members.select_related('user').order_by('joined_at')
        return Response(ClubMemberSerializer(members, many=True).data)
