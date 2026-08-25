"""Report endpoints for everything that is not a Swift.

Swifts keep `POST /api/swifts/<pk>/report/` - it was here first, it is tested
and throttled, and there was no reason to move it. These four are the same shape
so the app's one `ReportSheet` can post to any of them:

    POST /api/moderation/report/user/<id>/
    POST /api/moderation/report/story/<id>/
    POST /api/moderation/report/comment/<id>/
    POST /api/moderation/report/message/<id>/
    POST /api/moderation/report/club-message/<id>/

    {"reason": "hate", "note": "optional"}  ->  202 {"reported": true}

Two behaviours worth stating because they are deliberate and look like bugs
otherwise:

**The response never says whether this was a first report or a repeat.** A
reporter learning they had already reported something learns nothing useful, and
a reporter learning that *others* had would be the start of a brigading signal.

**Reporting something you cannot see answers 404, identically to something that
does not exist.** Anything else turns the report endpoint into an oracle for
which rows exist - the same rule `users.visibility.not_found` exists to enforce.
"""
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from users import visibility as user_visibility

from . import services
from .models import ContentReport
from .serializers import ReportSerializer


class _ReportView(APIView):
    """Shared body. Subclasses set `target_type` and, where the content is not
    world-readable, override `can_see`."""

    permission_classes = [permissions.IsAuthenticated]
    #: One scope for every report route, so twenty reports an hour is twenty
    #: reports an hour however they are spread across content types. Matches
    #: `swift_report`, deliberately: a client that hits the ceiling on one
    #: surface should find the same ceiling on the next.
    throttle_scope = 'content_report'
    target_type = None

    def can_see(self, user, instance, author):
        """Whether `user` is allowed to know this row exists.

        Default: they may report anything whose author they can see at all,
        which drops both directions of a block. Subclasses tighten it where the
        content has its own audience.
        """
        if author is None:
            return True
        return user_visibility.relationship(user, author) != user_visibility.BLOCKED

    def post(self, request, pk):
        instance, author, _ = services.resolve_target(self.target_type, pk)
        if instance is None or not self.can_see(request.user, instance, author):
            return user_visibility.not_found()

        if author is not None and author.pk == request.user.pk:
            return Response(
                {'detail': 'You cannot report your own content.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = ReportSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        services.file_report(
            request.user, self.target_type, pk,
            reason=serializer.validated_data['reason'],
            note=serializer.validated_data.get('note', ''),
        )
        return Response({'reported': True}, status=status.HTTP_202_ACCEPTED)


class ReportUserView(_ReportView):
    target_type = ContentReport.Target.USER


class ReportStoryView(_ReportView):
    target_type = ContentReport.Target.STORY

    def can_see(self, user, instance, author):
        from stories import visibility as story_visibility

        return story_visibility.can_read(user, instance)


class ReportCommentView(_ReportView):
    target_type = ContentReport.Target.COMMENT

    def can_see(self, user, instance, author):
        from swifts import visibility as swift_visibility

        if instance.deleted_at is not None:
            return False
        return swift_visibility.can_watch(user, instance.swift)


class ReportMessageView(_ReportView):
    target_type = ContentReport.Target.MESSAGE

    def can_see(self, user, instance, author):
        # Only the two people in the conversation, and only the *recipient* can
        # report - reporting a message you sent is not a thing, and allowing it
        # would let somebody manufacture a report trail against themselves.
        return instance.receiver_id == user.pk


class ReportClubMessageView(_ReportView):
    target_type = ContentReport.Target.CLUB_MESSAGE

    def can_see(self, user, instance, author):
        from clubs.models import ClubMember

        return ClubMember.objects.filter(
            club_id=instance.club_id, user=user,
        ).exists()
