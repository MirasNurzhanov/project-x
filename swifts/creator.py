"""Creator galleries and private, aggregate-only Swift performance reports."""
from datetime import datetime, time, timedelta, timezone as datetime_timezone

from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import permissions
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from moderation.reasons import VISIBLE_STATES
from users import visibility as user_visibility
from . import media, visibility
from .models import SwiftComment, SwiftLike, SwiftRepost, SwiftVideo, SwiftView
from .serializers import SwiftSerializer
from .views import _page_params, _viewer_context


def _owned_swifts(user):
    return SwiftVideo.objects.filter(
        author=user, deleted_at__isnull=True,
    ).exclude(moderation=SwiftVideo.Moderation.REMOVED).order_by()


class SwiftCreatorView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, user_id):
        target = get_object_or_404(
            user_visibility.visible_users(request.user, exclude_self=False), pk=user_id,
        )
        if target.pk == request.user.pk:
            queryset = _owned_swifts(request.user)
        else:
            if not user_visibility.can_view_profile_details(request.user, target):
                return user_visibility.not_found()
            queryset = visibility.watchable(request.user).filter(author_id=target.pk)

        limit, offset = _page_params(request, default_limit=24)
        queryset = queryset.select_related('author').order_by('-created_at', '-pk')
        count = queryset.count()
        page = list(queryset[offset:offset + limit])
        return Response({
            'count': count,
            'results': SwiftSerializer(
                page, many=True, context=_viewer_context(request, page),
            ).data,
        }, headers={'Cache-Control': 'no-store'})


METRICS = ('views', 'likes', 'comments', 'reposts')


def _activity_series(videos, days, now):
    """Four grouped queries, bounded to two reporting windows; no viewer data."""
    today = now.astimezone(datetime_timezone.utc).date()
    start = today - timedelta(days=days - 1)
    previous_start = start - timedelta(days=days)
    since = datetime.combine(previous_start, time.min, tzinfo=datetime_timezone.utc)
    records = {
        'views': (SwiftView.objects.all(), 'first_viewed_at'),
        'likes': (SwiftLike.objects.all(), 'created_at'),
        'comments': (SwiftComment.objects.filter(
            deleted_at__isnull=True, moderation__in=VISIBLE_STATES,
        ), 'created_at'),
        'reposts': (SwiftRepost.objects.all(), 'created_at'),
    }
    series = {
        start + timedelta(days=offset): {metric: 0 for metric in METRICS}
        for offset in range(days)
    }
    period = {metric: 0 for metric in METRICS}
    previous = {metric: 0 for metric in METRICS}
    for metric, (queryset, stamp) in records.items():
        buckets = (
            queryset.filter(swift_id__in=videos.values('pk'), **{
                f'{stamp}__gte': since, f'{stamp}__lte': now,
            }).order_by()
            .annotate(day=TruncDate(stamp, tzinfo=datetime_timezone.utc))
            .values('day').annotate(total=Count('pk'))
        )
        for bucket in buckets:
            if bucket['day'] < start:
                previous[metric] += bucket['total']
            else:
                series[bucket['day']][metric] = bucket['total']
                period[metric] += bucket['total']
    return start, today, period, previous, [
        {'date': day.isoformat(), **values} for day, values in series.items()
    ]


class SwiftAnalyticsView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = 'swift_analytics'

    def get(self, request):
        try:
            days = int(request.query_params.get('days', '7'))
        except (TypeError, ValueError):
            raise ValidationError({'days': 'Choose 7, 28 or 90 days.'})
        if days not in (7, 28, 90):
            raise ValidationError({'days': 'Choose 7, 28 or 90 days.'})

        # The account always comes from authentication, never a URL parameter.
        videos = _owned_swifts(request.user)
        swift_id = request.query_params.get('swift_id')
        if swift_id is not None:
            try:
                swift_id = int(swift_id)
                if swift_id < 1:
                    raise ValueError
            except (TypeError, ValueError):
                raise ValidationError({'swift_id': 'Enter a positive Swift ID.'})
            get_object_or_404(videos, pk=swift_id)
            videos = videos.filter(pk=swift_id)

        totals = videos.aggregate(
            videos=Count('pk'), views=Sum('view_count', default=0),
            likes=Sum('like_count', default=0), comments=Sum('comment_count', default=0),
            reposts=Sum('repost_count', default=0),
        )
        now = timezone.now()
        start, end, period, previous, series = _activity_series(videos, days, now)
        start_time = datetime.combine(start, time.min, tzinfo=datetime_timezone.utc)
        top = videos.annotate(period_views=Count('views', filter=Q(
            views__first_viewed_at__gte=start_time, views__first_viewed_at__lte=now,
        ))).filter(period_views__gt=0).order_by('-period_views', '-created_at', '-pk')[:10]
        return Response({
            'days': days, 'start_date': start.isoformat(), 'end_date': end.isoformat(),
            'timezone': 'UTC', 'totals': totals, 'period': period,
            'previous_period': previous, 'series': series,
            'top_swifts': [{
                'id': swift.pk, 'caption': swift.caption,
                'thumbnail_url': media.poster_url(swift),
                'created_at': swift.created_at, 'views': swift.period_views,
            } for swift in top],
            'definitions': {
                'views': 'Each viewer counts once per Swift, on their first view. '
                         'Viewing several Swifts counts separately; replays do not add views.',
                'engagements': 'Likes, comments and reposts reflect retained actions by creation date. '
                               'Undoing or removing an action changes historical counts. '
                               'Removed and deleted Swifts are excluded. Dates use UTC.',
            },
        }, headers={'Cache-Control': 'no-store'})
