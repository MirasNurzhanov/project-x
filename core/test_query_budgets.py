"""Ceilings on how many queries the hot endpoints may run.

These are regression guards, not micro-benchmarks. Each one existed as an N+1
that grew with the size of the response, and the way that comes back is a
serializer method or a loop added later that looks harmless on a list of three.
The fixture is deliberately large enough that a per-row query would blow the
budget by an order of magnitude rather than by one.

The budgets are ceilings with a little headroom, so an honest extra query is a
one-line change here rather than a puzzle. What they will not tolerate is a
count that scales with `N_*` below.

Run just these:
    python manage.py test core.test_query_budgets --settings=core.test_settings
"""
from datetime import date, timedelta
from decimal import Decimal

from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from clubs.models import Club, ClubEvent, ClubMember
from friends.models import FriendRequest, HiRequest, Message
from interests.models import Interest
from notifications.models import Notification
from regions.models import Region, UserPlace
from stories.models import Story
from swifts.models import SwiftLike, SwiftVideo, SwiftView
from users.models import User

LAT, LON = Decimal('25.20000000'), Decimal('55.27000000')  # Dubai


def _ring(min_lat, min_lon, max_lat, max_lon):
    return [[[min_lon, min_lat], [max_lon, min_lat], [max_lon, max_lat],
             [min_lon, max_lat], [min_lon, min_lat]]]


class QueryBudgetTests(TestCase):
    #: Big enough that a per-row query is unmistakable in the count.
    N_NEARBY = 25
    N_CLUBS = 30
    N_FRIENDS = 12

    @classmethod
    def setUpTestData(cls):
        cls.interests = [Interest.objects.create(name=f'i{i}') for i in range(6)]

        cls.city = Region.objects.create(
            name='Dubai', display_name='Dubai, UAE', country_code='ae',
            kind=Region.Kind.CITY, admin_level=4,
            min_lat=24.8, min_lon=54.9, max_lat=25.6, max_lon=55.6,
            centroid_lat=25.2, centroid_lon=55.27,
            rings=_ring(24.8, 54.9, 25.6, 55.6),
        )
        cls.district = Region.objects.create(
            name='Al Barsha', display_name='Al Barsha, Dubai', country_code='ae',
            kind=Region.Kind.DISTRICT, admin_level=7,
            min_lat=25.1, min_lon=55.2, max_lat=25.3, max_lon=55.35,
            centroid_lat=25.2, centroid_lon=55.27,
            rings=_ring(25.1, 55.2, 25.3, 55.35),
        )

        cls.me = cls._user('me', 0)
        cls._place(cls.me)
        cls.others = []
        for i in range(cls.N_NEARBY):
            other = cls._user(f'u{i}', i + 1)
            cls._place(other)
            cls.others.append(other)

        for other in cls.others[:cls.N_FRIENDS]:
            HiRequest.objects.create(
                sender=cls.me, receiver=other, status=HiRequest.Status.ACCEPTED)
            FriendRequest.objects.create(
                sender=cls.me, receiver=other, status=FriendRequest.Status.ACCEPTED)
            Message.objects.create(sender=other, receiver=cls.me, content='hi')

        for i in range(cls.N_CLUBS):
            club = Club.objects.create(
                name=f'club-{i}',
                creator=cls.others[i % len(cls.others)],
                visibility_level=(
                    Club.Visibility.DISTRICT if i % 2 else Club.Visibility.CITY
                ),
                visibility_city=cls.city, visibility_city_name='Dubai',
                visibility_city_osm=cls.city.osm_ref,
                visibility_district=cls.district,
                visibility_district_name='Al Barsha',
                visibility_district_osm=cls.district.osm_ref,
                visibility_country_code='ae',
            )
            for other in cls.others[: (i % 5) + 1]:
                ClubMember.objects.create(user=other, club=club)
            if i < 4:
                ClubMember.objects.create(
                    user=cls.me, club=club, role=ClubMember.Role.OWNER)
                ClubEvent.objects.create(
                    club=club, created_by=cls.me, title=f'meet-{i}',
                    event_datetime=timezone.now() + timedelta(hours=2),
                    status=ClubEvent.Status.APPROVED,
                    expires_at=timezone.now() + timedelta(hours=20),
                    latitude=LAT, longitude=LON,
                )

        for i, other in enumerate(cls.others[:15]):
            Story.objects.create(
                user=other, content=f's{i}', latitude=LAT, longitude=LON,
                expires_at=timezone.now() + timedelta(hours=5),
            )
            Notification.objects.create(
                recipient=cls.me, sender=other,
                notification_type=Notification.Type.NEARBY_USER, message='near',
            )

        # Swifts, with engagement by `me` on some of them — the viewer flags
        # (liked / reposted / viewed) are three separate lookups and the whole
        # point of the budget is that they stay three rather than three per row.
        for i, other in enumerate(cls.others[:20]):
            swift = SwiftVideo.objects.create(
                author=other, caption=f'sw{i}',
                video_url='https://cdn.example.com/v/x.mp4',
                duration_ms=15_000, width=1080, height=1920,
                latitude=LAT if i % 3 else None,
                longitude=LON if i % 3 else None,
                location_shared=bool(i % 3),
            )
            if i % 2:
                SwiftLike.objects.create(swift=swift, user=cls.me)
            if i % 4 == 0:
                SwiftView.objects.create(swift=swift, viewer=cls.me)

    @classmethod
    def _user(cls, name, i):
        user = User.objects.create_user(
            username=name, email=f'{name}@example.com', password='pw',
            latitude=LAT + Decimal(i) / Decimal('100000'),
            longitude=LON + Decimal(i) / Decimal('100000'),
            location_updated_at=timezone.now(), location_accuracy_m=10.0,
            last_seen=timezone.now(), age_tier=User.AgeTier.ADULT,
            date_of_birth=date(1995, 1, 1),
            # Sharing, because these budgets are about the cost of the hot
            # paths for people who are actually on the map. `location_sharing`
            # defaults to Paused, and a fixture full of paused users would
            # measure the early returns rather than the work.
            ghost_mode=False,
        )
        user.interests.set(cls.interests[: (i % 4) + 2])
        return user

    @classmethod
    def _place(cls, user):
        UserPlace.objects.create(
            user=user, city=cls.city, region=cls.district,
            city_name='Dubai', region_name='Al Barsha',
            city_osm=cls.city.osm_ref, region_osm=cls.district.osm_ref,
            country_code='ae',
            area_ids=[cls.district.pk, cls.city.pk],
            area_osm_refs=[cls.district.osm_ref, cls.city.osm_ref],
            latitude=float(user.latitude), longitude=float(user.longitude),
            outcome='resolved',
        )

    def setUp(self):
        # The encounter-cleanup throttle is a cache key; a leaked one from
        # another test would silently change the count under /nearby/.
        cache.clear()
        self.client = APIClient()
        self.client.force_authenticate(user=self.me)

    def assertWithinBudget(self, budget, method, path, data=None):
        """Assert the request runs at most `budget` queries.

        A ceiling rather than `assertNumQueries`' exact match, deliberately: an
        exact count turns every honest refactor into a failing test and trains
        people to bump the number without reading it. What these guard against
        is a count that scales with the fixture, and a ceiling catches that just
        as well while tolerating a query moving from one place to another.
        """
        call = getattr(self.client, method)

        def fire():
            return call(path, data, format='json') if data else call(path)

        # Warm first: the very first request of a test also populates
        # content-type and permission caches, which are not per-request costs.
        fire()
        with CaptureQueriesContext(connection) as captured:
            response = fire()
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(
            len(captured), budget,
            f'{method.upper()} {path} ran {len(captured)} queries, budget is '
            f'{budget}. If this grew with the fixture size it is an N+1:\n'
            + '\n'.join(q['sql'][:160] for q in captured.captured_queries),
        )
        return response

    # ── Discovery ─────────────────────────────────────────────────────────────
    def test_club_discovery_does_not_query_per_club(self):
        """Was 3 queries per club — creator, member count, my role — plus 2."""
        response = self.assertWithinBudget(4, 'get', '/api/clubs/')
        self.assertGreaterEqual(len(response.data), 10)

    def test_club_discovery_still_reports_member_counts(self):
        """The annotation has to produce the same numbers the loop did."""
        response = self.client.get('/api/clubs/')
        by_name = {club['name']: club for club in response.data}
        for club in Club.objects.all():
            if club.name in by_name:
                self.assertEqual(
                    by_name[club.name]['member_count'],
                    club.members.count(),
                    f'member_count wrong for {club.name}',
                )

    def test_joined_list_does_not_query_per_club(self):
        response = self.assertWithinBudget(4, 'get', '/api/clubs/?joined=true')
        # Every club here is one the viewer owns, and the annotation must say so.
        self.assertTrue(response.data)
        for club in response.data:
            self.assertEqual(club['my_role'], ClubMember.Role.OWNER)

    def test_discovery_reports_no_role_for_unjoined_clubs(self):
        response = self.client.get('/api/clubs/')
        for club in response.data:
            self.assertIsNone(club['my_role'])

    def test_club_detail_does_not_query_per_member(self):
        club = Club.objects.filter(members__user=self.me).first()
        self.assertWithinBudget(6, 'get', f'/api/clubs/{club.pk}/')

    # ── Map ───────────────────────────────────────────────────────────────────
    def test_nearby_users_is_a_fixed_number_of_queries(self):
        self.assertWithinBudget(20, 'get', '/api/proximity/nearby/')

    def test_nearby_stories_is_a_fixed_number_of_queries(self):
        # 14 rather than 13 since story pins became viewer-dependent: a friend
        # of the author sees the precise point, everybody else the ~500m cell,
        # and deciding that costs one friend-set lookup. Fixed, not per story —
        # `StorySerializer` caches the set for the whole response, and without
        # that cache this same request reads about 106.
        self.assertWithinBudget(14, 'get', '/api/stories/nearby/')

    def test_friend_stories_are_a_fixed_number_of_queries(self):
        """The chat screen polls this for its story rings, so it is a hot path
        like the map feeds beside it - and it must not grow a query per story
        or per author."""
        response = self.assertWithinBudget(12, 'get', '/api/stories/friends/')
        self.assertTrue(response.data['results'], 'the feed answered empty')

    def test_nearby_meetups_is_a_fixed_number_of_queries(self):
        self.assertWithinBudget(3, 'get', '/api/clubs/events/nearby/')

    # ── Everything else the app polls ─────────────────────────────────────────
    def test_friends_list_is_a_fixed_number_of_queries(self):
        self.assertWithinBudget(8, 'get', '/api/friends/list/')

    def test_notifications_are_a_fixed_number_of_queries(self):
        self.assertWithinBudget(10, 'get', '/api/notifications/')

    # ── Swifts ────────────────────────────────────────────────────────────────
    def test_swift_feed_does_not_query_per_swift(self):
        """The feed is the hottest read Swifts will have.

        Four things could each have been a query per row and are not: the
        author (select_related), and the three viewer flags, which the view
        batches into one lookup each via `_viewer_context`. `location_available`
        is the one to watch — it runs the reveal gate per row, and the gate can
        reach the relationship tables, so a change there is exactly how this
        budget would quietly start scaling with the page size.
        """
        response = self.assertWithinBudget(12, 'get', '/api/swifts/feed/?chrono=1')
        self.assertGreaterEqual(len(response.data['results']), 10)

    def test_ranked_feed_costs_one_ranking_pass_not_one_per_swipe(self):
        """The personalised path has a different budget, and it is honest.

        A cold ranked page is genuinely more expensive than a chronological
        one: six candidate sources, two filter passes, feature hydration and an
        impression write. Around forty statements, none of which scales with
        the candidate count — `recs/tests/test_query_budget.py` proves that part
        separately.

        What makes it affordable is that it is paid **once per session**. One
        ranking pass fills sixty ordered ids into the cache and the next five
        pages are a cache read and a slice. So the number a viewer actually
        experiences per swipe is the warm one below, and the cold one is
        amortised across a whole browsing run.

        Both are asserted, because a regression that turned the warm path cold
        would leave the cold budget green and make the feed six times more
        expensive with nothing failing.
        """
        # Measured directly rather than through `assertWithinBudget`, which
        # fires each request twice to warm the permission and content-type
        # caches. That is right for every other endpoint here and wrong for
        # this one: a ranked request *records impressions*, so the warm-up call
        # consumes the very Swifts the measured call would have ranked, and the
        # measurement lands on an exhausted session. Worth knowing beyond this
        # test — the ranked feed is deliberately not idempotent.
        from recs.models import SwiftImpression

        self.client.get('/api/swifts/feed/?chrono=1')  # warm the caches only
        SwiftImpression.objects.all().delete()

        with CaptureQueriesContext(connection) as cold:
            # A small page, so there is a second one to continue to — the
            # default limit exceeds this fixture and would exhaust the
            # session in one go, leaving no cursor to measure the warm path
            # with.
            response = self.client.get('/api/swifts/feed/?limit=3')
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(
            len(cold), 50,
            f'a cold ranked page ran {len(cold)} queries, budget is 50',
        )

        cursor = response.data.get('next_cursor')
        self.assertTrue(cursor, 'expected a cursor to continue the session')

        with CaptureQueriesContext(connection) as warm:
            second = self.client.get(f'/api/swifts/feed/?limit=3&cursor={cursor}')
        self.assertEqual(second.status_code, 200)
        # Ten, not eight: the tag payload added two. They are a `select_related`
        # for the track and a `prefetch_related` for the hashtag and mention
        # lists — see `swifts.views._payload_qs` — and the number that matters
        # is that both are *per page* rather than per Swift. Serving the same
        # payload without them would be three queries per row, so this budget
        # went up by two in order not to go up by 3N.
        self.assertLessEqual(
            len(warm), 10,
            f'a continued page ran {len(warm)} queries, budget is 10 — the '
            'session cache is what keeps this cheap, so this failing means '
            'every swipe is now re-ranking',
        )

    def test_swift_feed_still_answers_the_viewer_flags(self):
        """The batching has to produce the same answers the per-row queries did.

        Read from `?chrono=1`: this is about the serializer, and the ranked
        path deliberately withholds Swifts it has already served, which makes
        it an unstable place to look a row up.
        """
        response = self.client.get('/api/swifts/feed/?chrono=1')
        by_id = {row['id']: row for row in response.data['results']}

        for swift in SwiftVideo.objects.filter(pk__in=by_id):
            self.assertEqual(
                by_id[swift.pk]['viewer_liked'],
                SwiftLike.objects.filter(swift=swift, user=self.me).exists(),
                f'viewer_liked wrong for swift {swift.pk}',
            )
            self.assertEqual(
                by_id[swift.pk]['viewer_viewed'],
                SwiftView.objects.filter(swift=swift, viewer=self.me).exists(),
                f'viewer_viewed wrong for swift {swift.pk}',
            )

    def test_swift_feed_never_carries_a_coordinate(self):
        """The location rule, guarded from outside the swifts app as well.

        `swifts/test_location_privacy.py` owns this assertion properly. It is
        repeated here against a large, mixed fixture because that suite builds
        its own small one, and a leak that only appears at page size is exactly
        the kind this file exists to catch.
        """
        raw = self.client.get('/api/swifts/feed/').content.decode()
        for banned in ('latitude', 'longitude', 'place_name'):
            self.assertNotIn(banned, raw)

    # ── Response size ─────────────────────────────────────────────────────────
    def test_large_json_responses_are_compressed(self):
        """The map poll and the club list are the two big repetitive payloads,
        and every client on mobile data pays for them every few seconds."""
        plain = self.client.get('/api/clubs/')
        gzipped = self.client.get('/api/clubs/', headers={'accept-encoding': 'gzip'})

        self.assertEqual(gzipped['Content-Encoding'], 'gzip')
        self.assertLess(
            len(gzipped.content), len(plain.content) / 2,
            'JSON this repetitive should compress by well over half',
        )

    def test_small_responses_are_left_alone(self):
        """Below ~200 bytes compression costs more than it saves, and Django
        declines it — asserted so a future middleware reorder is visible."""
        response = self.client.get(
            '/api/friends/messages/unread/', headers={'accept-encoding': 'gzip'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('Content-Encoding', response)

    # ── The hottest write ─────────────────────────────────────────────────────
    def test_location_report_does_not_query_per_person_in_range(self):
        """Was ~10 queries per nearby candidate: two relationship lookups, two
        cooldown checks and the writes, for everyone within 500m."""
        self.assertWithinBudget(
            20, 'patch', '/api/proximity/location/',
            {'latitude': '25.20050000', 'longitude': '55.27050000',
             'accuracy': 8.0, 'speed': 1.1},
        )
