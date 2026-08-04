"""Query-count probe for the hot endpoints. Not a test — a measurement.

    python manage.py test core.perf_probe --settings=core.test_settings -v2

Prints the query count and wall time for each endpoint under a fixed, realistic
fixture, so an optimisation can be stated as a number instead of a claim.
"""
import time

from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.utils import timezone
from datetime import date, timedelta
from decimal import Decimal

from rest_framework.test import APIClient

from clubs.models import Club, ClubEvent, ClubMember
from friends.models import FriendRequest, HiRequest, Message
from interests.models import Interest
from notifications.models import Notification
from regions.models import Region, UserPlace
from stories.models import Story
from users.models import User

LAT, LON = Decimal('25.20000000'), Decimal('55.27000000')  # Dubai


def ring(min_lat, min_lon, max_lat, max_lon):
    return [[[min_lon, min_lat], [max_lon, min_lat], [max_lon, max_lat],
             [min_lon, max_lat], [min_lon, min_lat]]]


class Probe(TestCase):
    N_NEARBY = 30
    N_CLUBS = 40
    N_FRIENDS = 15

    @classmethod
    def setUpTestData(cls):
        cls.interests = [
            Interest.objects.create(name=f'interest-{i}') for i in range(8)
        ]

        cls.city = Region.objects.create(
            name='Dubai', display_name='Dubai, UAE', country_code='ae',
            kind=Region.Kind.CITY, admin_level=4,
            min_lat=24.8, min_lon=54.9, max_lat=25.6, max_lon=55.6,
            centroid_lat=25.2, centroid_lon=55.27,
            rings=ring(24.8, 54.9, 25.6, 55.6),
        )
        cls.district = Region.objects.create(
            name='Al Barsha', display_name='Al Barsha, Dubai', country_code='ae',
            kind=Region.Kind.DISTRICT, admin_level=7,
            min_lat=25.1, min_lon=55.2, max_lat=25.3, max_lon=55.35,
            centroid_lat=25.2, centroid_lon=55.27,
            rings=ring(25.1, 55.2, 25.3, 55.35),
        )

        cls.me = cls._make_user('me', 0)
        cls._place(cls.me)

        cls.others = []
        for i in range(cls.N_NEARBY):
            u = cls._make_user(f'user{i}', i + 1)
            cls._place(u)
            cls.others.append(u)

        # Friends / connections, so the relationship maps have real work to do.
        for u in cls.others[:cls.N_FRIENDS]:
            HiRequest.objects.create(sender=cls.me, receiver=u,
                                     status=HiRequest.Status.ACCEPTED)
            FriendRequest.objects.create(sender=cls.me, receiver=u,
                                         status=FriendRequest.Status.ACCEPTED)
            Message.objects.create(sender=u, receiver=cls.me, content='hi there')
            Message.objects.create(sender=cls.me, receiver=u, content='hello back')
        for u in cls.others[cls.N_FRIENDS:cls.N_FRIENDS + 5]:
            HiRequest.objects.create(sender=u, receiver=cls.me,
                                     status=HiRequest.Status.ACCEPTED)

        # Clubs in the city, half of them district-scoped.
        for i in range(cls.N_CLUBS):
            level = Club.Visibility.DISTRICT if i % 2 else Club.Visibility.CITY
            club = Club.objects.create(
                name=f'club-{i}', description='x' * 120,
                creator=cls.others[i % len(cls.others)],
                visibility_level=level,
                visibility_city=cls.city, visibility_city_name='Dubai',
                visibility_city_osm=cls.city.osm_ref,
                visibility_district=cls.district,
                visibility_district_name='Al Barsha',
                visibility_district_osm=cls.district.osm_ref,
                visibility_country_code='ae',
            )
            for u in cls.others[: (i % 7) + 1]:
                ClubMember.objects.create(user=u, club=club)
            if i < 5:
                ClubMember.objects.create(user=cls.me, club=club,
                                          role=ClubMember.Role.OWNER)
                ClubEvent.objects.create(
                    club=club, created_by=cls.me, title=f'meetup-{i}',
                    event_datetime=timezone.now() + timedelta(hours=2),
                    status=ClubEvent.Status.APPROVED,
                    expires_at=timezone.now() + timedelta(hours=20),
                    latitude=LAT, longitude=LON,
                )

        for i, u in enumerate(cls.others[:20]):
            Story.objects.create(
                user=u, content=f'story {i}', latitude=LAT, longitude=LON,
                expires_at=timezone.now() + timedelta(hours=5),
            )

        for i, u in enumerate(cls.others[:25]):
            Notification.objects.create(
                recipient=cls.me, sender=u,
                notification_type=Notification.Type.NEARBY_USER,
                message='someone is near',
            )

    @classmethod
    def _make_user(cls, name, i):
        u = User.objects.create_user(
            username=name, email=f'{name}@example.com', password='pw',
            latitude=LAT + Decimal(i) / Decimal('100000'),
            longitude=LON + Decimal(i) / Decimal('100000'),
            location_updated_at=timezone.now(),
            location_accuracy_m=10.0,
            last_seen=timezone.now(),
            age_tier=User.AgeTier.ADULT,
            date_of_birth=date(1995, 1, 1),
        )
        u.interests.set(cls.interests[: (i % 5) + 2])
        return u

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
        self.client = APIClient()
        self.client.force_authenticate(user=self.me)

    def _measure_write(self, label, path, body):
        self.client.patch(path, body, format='json')
        with CaptureQueriesContext(connection) as ctx:
            start = time.perf_counter()
            response = self.client.patch(path, body, format='json')
            elapsed = (time.perf_counter() - start) * 1000
        print(f'{label:38s} status={response.status_code} '
              f'queries={len(ctx):4d}  {elapsed:7.1f}ms')
        return len(ctx)

    def _measure(self, label, path):
        # Warm: first call pays for content-type / permission caches.
        self.client.get(path)
        with CaptureQueriesContext(connection) as ctx:
            start = time.perf_counter()
            response = self.client.get(path)
            elapsed = (time.perf_counter() - start) * 1000
        size = len(response.content) if hasattr(response, 'content') else 0
        print(f'{label:38s} status={response.status_code} '
              f'queries={len(ctx):4d}  {elapsed:7.1f}ms  {size / 1024:7.1f}KB')
        return len(ctx)

    def test_probe(self):
        print('\n' + '=' * 92)
        print(f'FIXTURE: {self.N_NEARBY} nearby users, {self.N_CLUBS} clubs, '
              f'20 stories, {self.N_FRIENDS} friends')
        print('=' * 92)
        # /api/map/nearby/ fans the three below out across a thread pool, so its
        # query count is their sum. It is not probed directly because in-memory
        # SQLite cannot serve those threads concurrently.
        self._measure('GET /api/proximity/nearby/', '/api/proximity/nearby/')
        self._measure('GET /api/clubs/ (discovery)', '/api/clubs/')
        self._measure('GET /api/clubs/?joined=true', '/api/clubs/?joined=true')
        self._measure('GET /api/clubs/<id>/', f'/api/clubs/{Club.objects.first().pk}/')
        self._measure('GET /api/clubs/events/nearby/', '/api/clubs/events/nearby/')
        self._measure('GET /api/stories/nearby/', '/api/stories/nearby/')
        self._measure('GET /api/friends/list/', '/api/friends/list/')
        self._measure('GET /api/notifications/', '/api/notifications/')
        self._measure('GET /api/users/search/?q=user', '/api/users/search/?q=user')
        # The hottest write in the app: every phone PATCHes this every ~30s,
        # foregrounded or not.
        self._measure_write(
            'PATCH /api/proximity/location/', '/api/proximity/location/',
            {'latitude': '25.20050000', 'longitude': '55.27050000',
             'accuracy': 8.0, 'speed': 1.1},
        )
        print('=' * 92 + '\n')
