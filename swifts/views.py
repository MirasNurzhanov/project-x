"""Swift endpoints.

Conventions follow the rest of the app: `APIView` subclasses rather than
viewsets, `permissions.IsAuthenticated` stated explicitly even though it is the
project default, hand-rolled `?limit=&offset=` paging, and
`users.visibility.not_found()` for anything the caller may not see — so that a
Swift they cannot reach is indistinguishable from one that does not exist.

Two things here are worth reading before changing anything:

  * `_position_for` is the only place a Swift's coordinate is decided, and it
    overrides whatever the client sent. Lifted from `stories.views._position_for`
    so that the two products cannot drift into different answers about what
    "paused" means.

  * Every write that must not duplicate — like, repost, view, reveal — goes
    through `get_or_create` inside `transaction.atomic`, with `IntegrityError`
    caught. The unique constraint is the correctness boundary; the Python is
    just the part that decides whether a counter moves.
"""
from decimal import ROUND_HALF_EVEN, Decimal

from django.db import IntegrityError, transaction
from django.db.models import Count, F, Q
from django.utils import timezone
from moderation import services as moderation_services
from moderation.models import ContentReport
from moderation.reasons import VISIBLE_STATES
from rest_framework import permissions, serializers, status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from notifications.utils import (
    notify_swift_comment, notify_swift_like, notify_swift_repost,
)
from users import visibility as user_visibility

from . import ai_moderation
from . import counters
from . import visibility as swift_visibility
from .models import (
    SavedSwift, SwiftComment, SwiftLike, SwiftLocationReveal, SwiftReport,
    SwiftRepost, SwiftVideo, SwiftView,
)
from .serializers import (
    SwiftCommentCreateSerializer,
    SwiftReportSerializer,
    SwiftCommentSerializer,
    SwiftCreateSerializer,
    SwiftLocationRevealAuditSerializer,
    SwiftLocationSerializer,
    SwiftSerializer,
    SwiftUpdateSerializer,
    SwiftViewReportSerializer,
)

#: Ceiling on one page, and the default. Matches the shape `stories` and
#: `clubs` use so the app has one paging convention rather than three.
PAGE_MAX = 50
PAGE_DEFAULT = 20

#: Swift coordinates are stored at the model's 6 decimal places, ~11cm.
_COORD_STEP = Decimal('0.000001')


def _page_params(request, default_limit=PAGE_DEFAULT):
    try:
        limit = min(max(int(request.query_params.get('limit', default_limit)), 1), PAGE_MAX)
    except (TypeError, ValueError):
        limit = default_limit
    try:
        offset = max(int(request.query_params.get('offset', 0)), 0)
    except (TypeError, ValueError):
        offset = 0
    return limit, offset


def _fit_coord(value):
    """Round to the precision the Swift columns actually declare.

    `users.visibility.store_coordinates` works at the User model's 8 places;
    SwiftVideo keeps 6, and handing a Decimal more places than the column
    allows is a validation error at save rather than a silent round. Rounding
    at ~11cm cannot meaningfully undo a 500m snap, so this is safe to apply to
    a fuzzed coordinate as well as a precise one.
    """
    return Decimal(value).quantize(_COORD_STEP, rounding=ROUND_HALF_EVEN)


def _position_for(user, validated):
    """The location fields to persist for a Swift `user` is posting.

    The whole coupling between Swifts and location sharing lives here, and it
    runs one way: whether the creator is on the map at all decides whether a
    location is stored, and posting is never refused over it.

    Ghost Mode    nothing. Not the coordinate the client sent, not the one
                  their private map queries travel with, not the last one we
                  held. A Swift posted while ghosted carries no location, and
                  the place name goes with the coordinate because it is one.
                  `location_shared` is forced off as well: there is nothing to
                  share, and leaving the flag on would publish a reveal button
                  over an empty row. A ghost dropping a pin by posting would be
                  the mode undone by using the app.

    Otherwise     what they sent, precisely. *Who* may see it precisely is not
                  decided here any more: friends do, everybody else gets the
                  ~500m cell, applied on the way out by
                  `SwiftLocationSerializer`.

    Enforced server-side because the client is not the one making the promise.
    An old build, or a modified one, that keeps sending coordinates while
    ghosted gets them dropped.
    """
    blank = {
        'latitude': None, 'longitude': None, 'place_name': '', 'location_shared': False,
    }

    # `is_sharing`, not `is_on_map`: this is the creator's own act, and a Swift
    # carries the coordinate it was made at rather than a claim about where its
    # author is now. Freshness governs the live user marker, which does make
    # that claim — see users.visibility for the split.
    if not user.is_sharing():
        return blank

    lat = validated.get('latitude')
    lon = validated.get('longitude')
    if lat is None or lon is None:
        # A sharing creator who posted without one. Locationless is the normal
        # kind of Swift, so this is not an error.
        return blank

    lat, lon = user_visibility.store_coordinates(user, lat, lon)
    return {
        'latitude': _fit_coord(lat),
        'longitude': _fit_coord(lon),
        'place_name': validated.get('place_name', ''),
        'location_shared': validated.get('location_shared', False),
    }


def _visible_swift(viewer, pk):
    """A Swift `viewer` may watch and act on, or None.

    Blocked authors, removed rows, soft-deleted rows and Swifts outside the
    viewer's audience are all indistinguishable from a Swift that does not
    exist. `select_related('author')` because every caller goes on to ask
    something about them.
    """
    return (
        swift_visibility.watchable(viewer)
        .filter(pk=pk)
        .select_related('author')
        .first()
    )


def _own_or_visible_swift(viewer, pk):
    """A Swift `viewer` may watch, **or** one of their own that is not live yet.

    The one deliberate exception to "a Swift you cannot see is indistinguishable
    from one that does not exist", and it exists because the composer needs it:
    publishing navigates straight to `/swifts/<id>`, and with AI moderation on
    that Swift is `PENDING` at the moment the phone arrives there. Answering
    404 to the person who just posted it would read as "your upload vanished".

    Scoped as tightly as the exception allows. It is the author only, it is
    never a REMOVED row — a takedown the author can still browse is not a
    takedown, which is the rule `SwiftMineView` already states — and it is not
    a soft-deleted one. What they get back is the ordinary payload with
    `review_state` telling them where it is; what everybody else gets is the
    404 they got before this function existed.
    """
    swift = _visible_swift(viewer, pk)
    if swift is not None:
        return swift
    return (
        SwiftVideo.objects.filter(
            pk=pk, author=viewer, deleted_at__isnull=True,
        )
        .exclude(moderation=SwiftVideo.Moderation.REMOVED)
        .select_related('author')
        .first()
    )


def _verified(request):
    """Context for a single row that has already been through the gate.

    Every caller of this fetched its row via `_visible_swift` or via an
    author-scoped queryset, so `watchable_q` has already been applied in SQL.
    See `SwiftSerializer.get_location_available`.
    """
    return {'request': request, 'watchable_verified': True}


def _viewer_context(request, swifts):
    """Serializer context that answers the viewer-relative flags in 4 queries.

    Without this the serializer falls back to a per-object `.exists()` for each
    of liked / reposted / saved / viewed — four queries per Swift on a page of
    twenty.
    """
    ids = [s.pk for s in swifts]
    base = {'request': request, 'watchable_verified': True}
    if not ids:
        return {**base, 'liked_ids': set(), 'reposted_ids': set(),
                'saved_ids': set(), 'viewed_ids': set()}
    user = request.user
    return {
        **base,
        'liked_ids': set(
            SwiftLike.objects.filter(user=user, swift_id__in=ids)
            .values_list('swift_id', flat=True)
        ),
        'reposted_ids': set(
            SwiftRepost.objects.filter(user=user, swift_id__in=ids)
            .values_list('swift_id', flat=True)
        ),
        'saved_ids': set(
            SavedSwift.objects.filter(user=user, swift_id__in=ids)
            .values_list('swift_id', flat=True)
        ),
        'viewed_ids': set(
            SwiftView.objects.filter(viewer=user, swift_id__in=ids)
            .values_list('swift_id', flat=True)
        ),
    }


# ─── Feed and detail ──────────────────────────────────────────────────────────
class SwiftFeedView(APIView):
    """The feed. Personalised by default, reverse-chronological on request.

    The ordering moved to `recs/` and nothing else did. A row is the same row
    it always was — same serializer, same fields, same absence of coordinates —
    because the recommender ranks ids and never touches what a Swift *is*. That
    separation is what let ranking arrive without re-testing the payload.

    Two modes, and the difference is the pagination mechanism as much as the
    order:

    `cursor`   ranked. One ranking pass fills a session, and pages are slices
               of it, so the fifth swipe costs a cache read rather than a
               re-rank — and so a Swift cannot appear on page 1 and again on
               page 3, which is what offset paging over a changing ranked list
               would produce.
    `offset`   reverse-chronological, unranked. Kept because it is stable
               without a session, because `?chrono=1` is the answer to "is the
               recommender doing this", and because an installed client that
               has not learned about cursors keeps working.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        limit, offset = _page_params(request)
        chrono = request.query_params.get('chrono') in ('1', 'true', 'yes')

        if chrono:
            return self._chronological(request, limit, offset)
        return self._ranked(request, limit)

    def _ranked(self, request, limit):
        from recs import pipeline

        page_result = pipeline.page(
            request.user, cursor=request.query_params.get('cursor'), limit=limit,
        )
        rows = {
            row.id: row
            # Through the gate again, not `SwiftVideo.objects` — a ranked
            # session is a *cache*, and its ids were checked when it was built.
            #
            # Up to `session_ttl_s` can pass between ranking and serving a
            # later page of it. In that window a Swift can be deleted, switched
            # to private, taken down by moderation, or its author can block
            # this viewer — and a bare `filter(id__in=...)` would serve it
            # anyway, for half an hour, off a permission decision made before
            # any of that happened. Re-applying `watchable` checks current
            # relationships in the row query and keeps the session a hint
            # about *order* rather than a standing grant.
            for row in swift_visibility.watchable(request.user)
            .filter(id__in=page_result.swift_ids)
            .select_related('author')
        }
        # Re-imposed in the recommender's order: `filter(id__in=...)` returns
        # whatever order the database likes, and losing the ranking at the last
        # step is a mistake that looks exactly like a ranking bug.
        ordered = [rows[i] for i in page_result.swift_ids if i in rows]

        serializer = SwiftSerializer(
            ordered, many=True, context=_viewer_context(request, ordered),
        )
        return Response({
            # No total. A ranked session has no meaningful count — the honest
            # answer is "as many as we found for you", and inventing a number
            # here would make the client draw a progress bar against a fiction.
            'count': None,
            'results': serializer.data,
            'next_cursor': page_result.cursor,
            'has_more': page_result.has_more,
            # Which source won each slot, so the client can attribute the
            # signals it sends back. Ids and source names only.
            'sources': page_result.sources_by_id,
        })

    def _chronological(self, request, limit, offset):
        qs = (
            swift_visibility.watchable(request.user)
            .select_related('author')
            .order_by('-created_at')
        )
        count = qs.count()
        page = list(qs[offset:offset + limit])
        serializer = SwiftSerializer(
            page, many=True, context=_viewer_context(request, page),
        )
        return Response({
            'count': count,
            'results': serializer.data,
            'next_cursor': None,
            'has_more': offset + limit < count,
        })


class SwiftMineView(APIView):
    """The creator's own Swifts, including ones nobody else can watch.

    Their own drafts, their private ones and their still-processing uploads all
    belong here — this is the only surface that shows a creator a row the feed
    would withhold. Removed and soft-deleted rows stay hidden even from them:
    a takedown that the author can still browse is not a takedown.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        limit, offset = _page_params(request)
        qs = (
            SwiftVideo.objects.filter(author=request.user, deleted_at__isnull=True)
            .exclude(moderation=SwiftVideo.Moderation.REMOVED)
            .select_related('author')
            .order_by('-created_at')
        )
        count = qs.count()
        page = list(qs[offset:offset + limit])
        serializer = SwiftSerializer(
            page, many=True, context=_viewer_context(request, page),
        )
        return Response({'count': count, 'results': serializer.data})


class SwiftListCreateView(APIView):
    """POST a new Swift."""

    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    #: See core/settings.py. Inert on every view that does not name a scope,
    #: which is why introducing throttling to this project was safe.
    throttle_scope = 'swift_create'

    def post(self, request):
        key = serializers.UUIDField(allow_null=True).run_validation(
            request.data.get('idempotency_key'))
        with transaction.atomic():
            if key is not None:
                # Serialize keyed publishes for this author before saving files.
                # A losing retry must not upload an orphan file or enqueue a job.
                type(request.user).objects.select_for_update().only('pk').get(pk=request.user.pk)
                existing = SwiftVideo.objects.filter(
                    author=request.user, idempotency_key=key,
                ).first()
                if existing is not None:
                    if existing.deleted_at or existing.moderation == SwiftVideo.Moderation.REMOVED:
                        return user_visibility.not_found()
                    return Response(SwiftSerializer(existing, context=_verified(request)).data)
            return self._create(request)

    def _create(self, request):
        serializer = SwiftCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        # Blocked captions never reach here - `ScreenedTextMixin` refused
        # them during `is_valid()`. A flagged one is stored quarantined, which
        # `swifts.visibility.live_q` keeps out of every feed until a moderator
        # has looked at it.
        #
        # With AI video moderation switched on, everything else is stored
        # `PENDING` — also hidden by `live_q`, and hidden *before* the response
        # to this request is written. That ordering is requirement 7: there is
        # no window, not even a millisecond, in which the row exists in a
        # publishable state. The verdict arrives later and can only move it
        # from PENDING; it cannot un-publish something that was briefly public,
        # because nothing was ever briefly public.
        verdict = serializer.moderation_verdict
        with transaction.atomic():
            swift = serializer.save(
                author=request.user,
                moderation=ai_moderation.initial_moderation(verdict),
                **_position_for(request.user, serializer.validated_data),
            )
            # Inside the transaction, so the durable job record commits with
            # the row it is about. The *work* is not inside it — `enqueue`
            # defers that to `on_commit` — so the phone is not waiting on
            # inference to get its 201.
            ai_moderation.enqueue(swift)
        # No `ContentReport` row for this one, unlike a story or a comment:
        # Swifts have their own report table and their own admin, and
        # `SwiftVideoAdmin` already filters on `moderation`. A quarantined
        # Swift is therefore already in front of a moderator, and inventing a
        # report with no reporter would put it in a second queue as well. An
        # *AI* quarantine does file one — see `ai_moderation.record_ai_report`
        # — because that one has evidence attached that the column alone
        # cannot carry.
        return Response(
            SwiftSerializer(swift, context=_verified(request)).data,
            status=status.HTTP_201_CREATED,
        )


class SwiftDetailView(APIView):
    """One Swift.

    GET is gated by `watchable_q`, so it answers 404 rather than 403 for
    anything out of reach. PATCH and DELETE are author-only and answer the same
    404 to everybody else — an endpoint that said 403 would confirm the Swift
    exists, which is the one thing the uniform-absence rule is protecting.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        # `_own_or_visible_swift`, not `_visible_swift`: the author of a Swift
        # still being screened has to be able to open the thing they just
        # posted. Nobody else's view of it changes — see that function.
        swift = _own_or_visible_swift(request.user, pk)
        if swift is None:
            return user_visibility.not_found()
        return Response(SwiftSerializer(swift, context=_verified(request)).data)

    def _own_swift(self, request, pk):
        return (
            SwiftVideo.objects.filter(
                pk=pk, author=request.user, deleted_at__isnull=True,
            )
            .exclude(moderation=SwiftVideo.Moderation.REMOVED)
            .first()
        )

    def patch(self, request, pk):
        swift = self._own_swift(request, pk)
        if swift is None:
            return user_visibility.not_found()
        serializer = SwiftUpdateSerializer(swift, data=request.data, partial=True)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        serializer.save()
        swift.refresh_from_db()
        return Response(SwiftSerializer(swift, context=_verified(request)).data)

    def delete(self, request, pk):
        """Soft delete.

        The row stays so that the reveal audit, the comments and the likes
        survive it — the audit in particular is worth more after a Swift comes
        down than before. Every read path filters `deleted_at__isnull=True`, so
        it is gone from the product either way.
        """
        swift = self._own_swift(request, pk)
        if swift is None:
            return user_visibility.not_found()
        swift.deleted_at = timezone.now()
        swift.save(update_fields=['deleted_at', 'updated_at'])
        return Response(status=status.HTTP_204_NO_CONTENT)


# ─── Views ────────────────────────────────────────────────────────────────────
class RecordSwiftViewView(APIView):
    """Record that the caller has watched this Swift.

    **The only writer of `SwiftView`**, and therefore the only thing that can
    ever satisfy the reveal gate. That is why it re-checks `watchable` itself
    rather than trusting a caller: a view row written for a Swift the viewer
    was not allowed to watch would be a key cut for a door they should never
    reach.

    Idempotent by construction. One row per (swift, viewer) forever, enforced
    by the unique constraint; a repeat call finds the existing row, bumps its
    own counters and leaves `SwiftVideo.view_count` alone. So a component that
    remounts on every render cannot inflate anything — which is the stated
    requirement, and it is met by the schema rather than by debouncing.
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        swift = _visible_swift(request.user, pk)
        if swift is None:
            return user_visibility.not_found()

        report = SwiftViewReportSerializer(data=request.data)
        if not report.is_valid():
            return Response(report.errors, status=status.HTTP_400_BAD_REQUEST)
        watch_ms = report.validated_data.get('watch_ms', 0)
        completed = report.validated_data.get('completed', False)

        created = False
        try:
            with transaction.atomic():
                _, created = SwiftView.objects.get_or_create(
                    swift=swift, viewer=request.user,
                    defaults={'watch_ms': watch_ms, 'completed': completed},
                )
        except IntegrityError:
            # Two first-views raced and the constraint caught the loser. Not an
            # error — the row it wanted exists, so fall through to the update.
            created = False

        if created:
            # Distinct viewers, so this moves exactly once per person, ever.
            counters.adjust(swift.pk, 'view_count', 1)
        else:
            # A rewatch. `watch_ms` is a high-water mark rather than a sum, so
            # replaying a Swift cannot inflate the reported watch time, and
            # `completed` only ever latches on.
            update = {'view_count': F('view_count') + 1, 'last_viewed_at': timezone.now()}
            if completed:
                update['completed'] = True
            SwiftView.objects.filter(swift=swift, viewer=request.user).update(**update)
            if watch_ms:
                SwiftView.objects.filter(
                    swift=swift, viewer=request.user, watch_ms__lt=watch_ms,
                ).update(watch_ms=watch_ms)

        return Response(
            {'recorded': True, 'first_view': created},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


# ─── Location reveal ──────────────────────────────────────────────────────────
class RevealSwiftLocationView(APIView):
    """Tell the caller where this Swift was made — if they have earned it.

    Every condition is checked server-side in `swifts.visibility.reveal_denial`
    and none of them can be influenced by the request body. In particular there
    is no `hasViewed` parameter, and adding one would be the bug: the fact that
    matters is a `SwiftView` row this server wrote, and a client cannot write
    one by claiming it exists.

    **Idempotent.** The first call creates the audit row; every call after it
    finds the same row and returns a byte-identical body. `revealed_at` is the
    *first* reveal and never moves, so the creator's audit answers "since when
    does this person know" rather than "when did they last ask". `reveal_count`
    tracks repeats for abuse detection and is deliberately kept out of the
    response, because a field that changes between two calls would make this
    endpoint non-idempotent in the only sense the caller can observe.

    Refusals are all 404, whatever the reason. A 403 here would confirm that a
    Swift exists and is hiding a location, which is exactly the fact being
    protected.

    **Throttled**, and the rate counts refusals as well as grants. That is the
    point rather than an oversight: the traffic this limit exists to stop is
    somebody walking the id space to harvest coordinates, and every one of
    those probes answers 404. A legitimate viewer re-opening the same sheet
    spends quota too, which is why the rate is set well above what watching
    videos could plausibly produce.
    """

    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = 'swift_location_reveal'

    def post(self, request, pk):
        # Deliberately not `_visible_swift` — the denial helper does the
        # watchability check itself, so fetching an unfiltered row and letting
        # one function make every decision keeps the gate in one place.
        swift = (
            SwiftVideo.objects.filter(pk=pk).select_related('author').first()
        )
        if swift is None:
            return user_visibility.not_found()

        denial = swift_visibility.reveal_denial(request.user, swift)
        if denial is not swift_visibility.ALLOW:
            return user_visibility.not_found()

        # The creator looking at their own Swift is not a reveal and leaves no
        # audit row — there is nobody to audit, and a self-entry would pollute
        # the "who looked" list they are the audience for.
        if swift.author_id != request.user.pk:
            try:
                with transaction.atomic():
                    _, created = SwiftLocationReveal.objects.get_or_create(
                        swift=swift, viewer=request.user,
                    )
            except IntegrityError:
                created = False
            if not created:
                SwiftLocationReveal.objects.filter(
                    swift=swift, viewer=request.user,
                ).update(reveal_count=F('reveal_count') + 1)

        # Context is not optional here: precision is the viewer's relationship
        # to the author, so a serializer without a request has no way to tell
        # the creator from a stranger and coarsens the answer for everybody.
        return Response(
            SwiftLocationSerializer(swift, context={'request': request}).data
        )


class SwiftLocationRevealAuditView(APIView):
    """Who has looked up where this Swift was made. Creator only.

    The other half of what makes attaching a location a reviewable decision
    rather than a switch somebody flips and forgets.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        swift = SwiftVideo.objects.filter(
            pk=pk, author=request.user, deleted_at__isnull=True,
        ).first()
        if swift is None:
            return user_visibility.not_found()

        limit, offset = _page_params(request)
        qs = (
            SwiftLocationReveal.objects.filter(swift=swift)
            .select_related('viewer')
            .order_by('-revealed_at')
        )
        count = qs.count()
        page = qs[offset:offset + limit]
        return Response({
            'count': count,
            'results': SwiftLocationRevealAuditSerializer(page, many=True).data,
        })


# ─── Likes ────────────────────────────────────────────────────────────────────
class SwiftLikeView(APIView):
    """POST to like, DELETE to unlike. Both idempotent.

    The unique constraint is what makes a duplicate like impossible; this only
    decides whether the counter moves. `get_or_create` returns `created=False`
    for a like that already exists, so a double tap — or two requests racing —
    leaves one row and one increment.
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        swift = _visible_swift(request.user, pk)
        if swift is None:
            return user_visibility.not_found()

        try:
            with transaction.atomic():
                _, created = SwiftLike.objects.get_or_create(
                    swift=swift, user=request.user,
                )
        except IntegrityError:
            created = False

        if created:
            counters.adjust(swift.pk, 'like_count', 1)
            # Only on a genuine first like. A repeat request must not push the
            # author again — which is the same reason the counter is guarded.
            notify_swift_like(swift, request.user)

        return Response(
            {'liked': True, 'like_count': _count_of(swift.pk, 'like_count')},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    def delete(self, request, pk):
        swift = _visible_swift(request.user, pk)
        if swift is None:
            return user_visibility.not_found()

        deleted, _ = SwiftLike.objects.filter(swift=swift, user=request.user).delete()
        if deleted:
            counters.adjust(swift.pk, 'like_count', -1)
        return Response({'liked': False, 'like_count': _count_of(swift.pk, 'like_count')})


# ─── Reposts ──────────────────────────────────────────────────────────────────
class SwiftRepostView(APIView):
    """POST to repost, DELETE to undo. Same constraint-first design as likes.

    A creator reposting their own Swift is refused: it would be a self-boost
    with no meaning to any reader, and it makes the repost count a number that
    cannot be trusted.
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        swift = _visible_swift(request.user, pk)
        if swift is None:
            return user_visibility.not_found()
        if not swift.allow_reposts:
            return Response(
                {'detail': 'Reposting is turned off for this Swift.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        if swift.author_id == request.user.pk:
            return Response(
                {'detail': 'You cannot repost your own Swift.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            with transaction.atomic():
                _, created = SwiftRepost.objects.get_or_create(
                    swift=swift, user=request.user,
                )
        except IntegrityError:
            created = False

        if created:
            counters.adjust(swift.pk, 'repost_count', 1)
            notify_swift_repost(swift, request.user)

        return Response(
            {'reposted': True, 'repost_count': _count_of(swift.pk, 'repost_count')},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    def delete(self, request, pk):
        swift = _visible_swift(request.user, pk)
        if swift is None:
            return user_visibility.not_found()
        deleted, _ = SwiftRepost.objects.filter(swift=swift, user=request.user).delete()
        if deleted:
            counters.adjust(swift.pk, 'repost_count', -1)
        return Response(
            {'reposted': False, 'repost_count': _count_of(swift.pk, 'repost_count')}
        )


class SwiftSaveView(APIView):
    """POST to save a Swift to your shortlist, DELETE to take it off.

    Same constraint-first shape as likes and reposts, and two deliberate
    differences from both:

    **No counter moves.** Saving is private, so there is nothing public to
    count. The response carries `saved` and nothing else — a `save_count`
    would be the one field on the Swift that leaked who had bookmarked it,
    inferable by watching the number after your own tap.

    **Saving your own Swift is allowed**, where reposting it is refused. The
    refusal there exists because a self-repost inflates a public number that
    other people read as endorsement. A shortlist has no such number, and
    "keep my own video handy" is an ordinary thing to want.

    Idempotent in both directions: saving twice is one row (the unique
    constraint decides that, not this view), and un-saving something that was
    never saved is a 200 rather than a 404. Both matter on a feed where the
    same Swift is reachable from several screens at once.
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        swift = _visible_swift(request.user, pk)
        if swift is None:
            return user_visibility.not_found()

        # `get_or_create` inside `atomic`, with IntegrityError caught rather
        # than pre-checked: two taps racing both reach the insert, and the one
        # that loses is a duplicate the database refuses. Catching it here is
        # what turns that race into "already saved" instead of a 500.
        try:
            with transaction.atomic():
                _, created = SavedSwift.objects.get_or_create(
                    swift=swift, user=request.user,
                )
        except IntegrityError:
            created = False

        return Response(
            {'saved': True},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    def delete(self, request, pk):
        # Deliberately *not* `_visible_swift`. Un-saving has to keep working
        # for a Swift that has since been made private or been taken down —
        # otherwise the one row a user most wants out of their list is the one
        # row they cannot remove.
        SavedSwift.objects.filter(swift_id=pk, user=request.user).delete()
        return Response({'saved': False})


class SwiftSavedView(APIView):
    """The viewer's own shortlist, newest save first.

    Filtered through the same visibility rules as any other list, which is the
    point rather than an oversight: a Swift that was public when it was saved
    and has since been made private, taken down or soft-deleted drops out of
    the list. The bookmark row survives — nothing is silently deleted from
    under the user — but a shortlist is not a way to keep watching something
    its author has withdrawn.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        limit, offset = _page_params(request)
        # Paged in the database, not in Python.
        #
        # The obvious shape — read every saved id, fetch the visible ones,
        # order the result in Python and slice — is correct and does not
        # scale: it loads one row per save on every request, so somebody with
        # ten thousand bookmarks pays for all ten thousand to look at the first
        # twenty. Ordering is the thing that makes it tempting, because the
        # order lives on `SavedSwift.created_at` while the filter lives on
        # `SwiftVideo`.
        #
        # Starting from `SavedSwift` resolves that: the ordering column is on
        # the model being paged, and visibility becomes a subquery against
        # `watchable`. One `LIMIT`/`OFFSET` in SQL, and the page is the only
        # thing that comes back.
        qs = (
            SavedSwift.objects
            .filter(user=request.user)
            .filter(swift__in=swift_visibility.watchable(request.user))
            .select_related('swift', 'swift__author')
            .order_by('-created_at')
        )
        count = qs.count()
        page = [row.swift for row in qs[offset:offset + limit]]
        serializer = SwiftSerializer(
            page, many=True, context=_viewer_context(request, page),
        )
        return Response({'count': count, 'results': serializer.data})


def _count_of(swift_id, field):
    """Re-read one counter after moving it.

    A separate query rather than reading the in-memory model, which still holds
    the value from before the `F()` update — returning that would report a
    stale count to the very client that just changed it.
    """
    return SwiftVideo.objects.filter(pk=swift_id).values_list(field, flat=True).first() or 0


# ─── Comments ─────────────────────────────────────────────────────────────────
class SwiftCommentsView(APIView):
    """GET the thread, POST a comment or a reply."""

    permission_classes = [permissions.IsAuthenticated]
    # Posting only — a GET of a comment list is an ordinary read. DRF
    # applies a scope to the whole view, so the ceiling is set high enough
    # that reading a busy thread repeatedly cannot hit it.
    throttle_scope = 'swift_comment'

    def get(self, request, pk):
        swift = _visible_swift(request.user, pk)
        if swift is None:
            return user_visibility.not_found()

        limit, offset = _page_params(request)
        blocked = user_visibility.blocked_ids_for(request.user)
        qs = (
            SwiftComment.objects.filter(swift=swift, deleted_at__isnull=True)
            .exclude(author_id__in=blocked)
            # Quarantined and removed comments are out of the thread for
            # everybody except the person who wrote them, who keeps seeing
            # their own so the app does not look like it silently dropped it.
            .filter(
                Q(moderation__in=VISIBLE_STATES) | Q(author_id=request.user.pk),
            )
            .select_related('author')
            .order_by('-created_at')
        )
        count = qs.count()
        page = list(qs[offset:offset + limit])

        # One grouped query for every reply count on the page, instead of one
        # `.count()` per comment inside the serializer.
        reply_counts = dict(
            SwiftComment.objects.filter(
                parent_id__in=[c.pk for c in page], deleted_at__isnull=True,
            )
            .values_list('parent_id')
            .annotate(n=Count('pk'))
        )

        serializer = SwiftCommentSerializer(
            page, many=True,
            context={'request': request, 'reply_counts': reply_counts},
        )
        return Response({'count': count, 'results': serializer.data})

    def post(self, request, pk):
        swift = _visible_swift(request.user, pk)
        if swift is None:
            return user_visibility.not_found()
        if not swift.allow_comments:
            return Response(
                {'detail': 'Comments are turned off for this Swift.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = SwiftCommentCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        parent = serializer.validated_data.get('parent')
        if parent is not None:
            # A reply has to belong to this Swift, and threads stay one level
            # deep — a reply to a reply is flattened onto its grandparent
            # rather than refused, which is what every phone client expects.
            if parent.swift_id != swift.pk or parent.deleted_at is not None:
                return Response(
                    {'parent': 'That comment is not on this Swift.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if parent.parent_id is not None:
                serializer.validated_data['parent'] = parent.parent

        verdict = serializer.moderation_verdict
        comment = serializer.save(
            swift=swift, author=request.user,
            moderation=moderation_services.moderation_for(verdict),
        )
        if verdict.flagged:
            moderation_services.record_automated_flag(
                comment, ContentReport.Target.COMMENT, verdict, author=request.user,
            )
            # A quarantined comment is not in the thread, so it does not move
            # the counter and it does not wake the creator's phone. Both would
            # be the app announcing something nobody can see.
            return Response(
                SwiftCommentSerializer(comment, context={'request': request}).data,
                status=status.HTTP_201_CREATED,
            )
        counters.adjust(swift.pk, 'comment_count', 1)
        notify_swift_comment(swift, request.user, comment.text)
        return Response(
            SwiftCommentSerializer(comment, context={'request': request}).data,
            status=status.HTTP_201_CREATED,
        )


class SwiftCommentDetailView(APIView):
    """DELETE one comment.

    The comment's author may remove it, and so may the Swift's author —
    somebody has to be able to clear their own replies, and a creator with no
    control over what sits under their video has no moderation at all.
    """

    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, comment_id):
        comment = (
            SwiftComment.objects.filter(pk=comment_id, deleted_at__isnull=True)
            .select_related('swift')
            .first()
        )
        if comment is None:
            return user_visibility.not_found()
        if request.user.pk not in (comment.author_id, comment.swift.author_id):
            return user_visibility.not_found()

        comment.deleted_at = timezone.now()
        comment.save(update_fields=['deleted_at'])
        counters.adjust(comment.swift_id, 'comment_count', -1)
        return Response(status=status.HTTP_204_NO_CONTENT)


# ─── Reporting ────────────────────────────────────────────────────────────────
class SwiftReportView(APIView):
    """Report a Swift for moderation.

    Sets `Moderation.FLAGGED`, withholding the Swift from public view until a
    moderator restores it to OK. Repeated requests never duplicate reports;
    once the Swift is hidden, subsequent requests receive the usual 404.
    """

    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = 'swift_report'

    def post(self, request, pk):
        swift = _visible_swift(request.user, pk)
        if swift is None:
            return user_visibility.not_found()
        if swift.author_id == request.user.pk:
            return Response(
                {'detail': 'You cannot report your own Swift.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = SwiftReportSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        try:
            with transaction.atomic():
                SwiftReport.objects.get_or_create(
                    swift=swift, reporter=request.user,
                    defaults=serializer.validated_data,
                )
        except IntegrityError:
            pass

        # Flag for review, but never overwrite a verdict a moderator has
        # already reached: a REMOVED Swift that gets reported again must not
        # quietly come back to FLAGGED.
        SwiftVideo.objects.filter(
            pk=swift.pk, moderation=SwiftVideo.Moderation.OK,
        ).update(moderation=SwiftVideo.Moderation.FLAGGED)

        return Response({'reported': True}, status=status.HTTP_202_ACCEPTED)


# ─── Music catalogue ──────────────────────────────────────────────────────────
class SwiftTrackListView(APIView):
    """Approx's own music catalogue, for the composer's track picker.

    Only `is_active` rows, ever. A track is inactive until somebody has looked
    at its licence and published it, and this endpoint is the only way a
    creator sees the catalogue at all — so an unpublished row is unreachable
    rather than merely un-listed.

    `?q=` searches title, artist and genre; `?genre=` filters. Both are
    deliberately simple string matching: the catalogue is curated and small,
    and a search index for a few hundred rows would be infrastructure with
    nothing to do.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .models import SwiftTrack
        from .serializers import SwiftTrackSerializer

        qs = SwiftTrack.objects.filter(is_active=True)

        query = (request.query_params.get('q') or '').strip()
        if query:
            qs = qs.filter(
                Q(title__icontains=query)
                | Q(artist__icontains=query)
                | Q(genre__icontains=query)
            )
        genre = (request.query_params.get('genre') or '').strip()
        if genre:
            qs = qs.filter(genre__iexact=genre)

        limit, offset = _page_params(request)
        count = qs.count()
        page = qs[offset:offset + limit]
        return Response({
            'count': count,
            'results': SwiftTrackSerializer(page, many=True).data,
        })


class SwiftTrackGenresView(APIView):
    """The genre chips above the track list, derived from what is published."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .models import SwiftTrack

        genres = (
            SwiftTrack.objects.filter(is_active=True)
            .exclude(genre='')
            .values_list('genre', flat=True)
            .distinct()
            .order_by('genre')
        )
        return Response({'results': list(genres)})


class SwiftMusicProviderStatusView(APIView):
    """Which music providers are wired up, and what is missing if not.

    Staff only. The list names environment variables, which is operational
    detail a creator has no use for and an attacker would enjoy — and the app
    never needs it: the picker reads Approx's own catalogue whether an importer
    is configured or not.
    """

    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        from .music import registry

        return Response({'results': registry.status()})
