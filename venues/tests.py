from datetime import timedelta
from decimal import Decimal
from importlib import import_module
from django.apps import apps
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from clubs.models import Club, ClubEvent, ClubMember, ClubMessage
from users.models import User
from .models import Venue

DUBAI_MARINA_VENUES = import_module('venues.migrations.0005_seed_dubai_marina').DUBAI_MARINA_VENUES


class VenueTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='venue-member', email='venue@example.test')
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.venue = Venue.objects.get(slug='dostyk-plaza')
        self.club = Club.objects.create(name='Venue club', creator=self.user,
            visibility_level=Club.Visibility.GLOBAL)
        ClubMember.objects.create(club=self.club, user=self.user, role=ClubMember.Role.MEMBER)
        self.events_url = f'/api/clubs/{self.club.pk}/events/'

    def propose(self, **overrides):
        return self.client.post(self.events_url, {
            'title': 'Saturday meetup', 'venue_id': self.venue.pk,
            'event_datetime': (timezone.now() + timedelta(days=1)).isoformat(),
            **overrides,
        }, format='json')

    def test_seeded_venues_and_numeric_coordinates(self):
        response = self.client.get('/api/venues/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 3 + len(DUBAI_MARINA_VENUES))
        rows = {row['slug']: row for row in response.data['results']}
        row = rows['dostyk-plaza']
        self.assertEqual(row['name'], 'Dostyk Plaza')
        self.assertEqual(row['latitude'], 43.233139)
        self.assertEqual(row['longitude'], 76.956562)
        self.assertNotIn('google_place_id', row)
        esentai = rows['esentai-mall']
        self.assertEqual(esentai['name'], 'Esentai Mall')
        self.assertEqual(esentai['latitude'], 43.218593)
        self.assertEqual(esentai['longitude'], 76.928083)
        six = rows['six-coffee-wine-abay']
        self.assertEqual(six['name'], 'Six Coffee+Wine')
        self.assertEqual(six['address'], 'Abay Avenue, 17, Almaty')
        self.assertEqual(six['latitude'], 43.24326)
        self.assertEqual(six['longitude'], 76.953786)

    def test_six_seed_preserves_edits_and_meetup_links_to_abay_branch(self):
        venue = Venue.objects.get(slug='six-coffee-wine-abay')
        venue.description = 'Staff description'
        venue.save()
        import_module('venues.migrations.0004_seed_six_coffee_wine').seed_six(
            apps, type('Editor', (), {'connection': connection})())
        venue.refresh_from_db()
        self.assertEqual(venue.description, 'Staff description')
        self.assertEqual(Venue.objects.filter(slug='six-coffee-wine-abay').count(), 1)
        self.assertEqual(self.propose(venue_id=venue.pk).status_code, 201)
        event = ClubEvent.objects.get()
        self.assertEqual(event.venue_id, venue.pk)
        self.assertEqual(event.location_name, 'Six Coffee+Wine')
        self.assertEqual(event.latitude, venue.latitude)
        self.assertEqual(event.longitude, venue.longitude)

    def test_esentai_seed_preserves_admin_changes(self):
        venue = Venue.objects.get(slug='esentai-mall')
        venue.is_active = False
        venue.description = 'Staff edit'
        venue.save()
        import_module('venues.migrations.0003_seed_esentai_mall').seed_esentai(
            apps, type('Editor', (), {'connection': connection})())
        venue.refresh_from_db()
        self.assertFalse(venue.is_active)
        self.assertEqual(venue.description, 'Staff edit')
        self.assertEqual(Venue.objects.filter(slug='esentai-mall').count(), 1)

    def test_esentai_meetup_uses_the_selected_venue(self):
        venue = Venue.objects.get(slug='esentai-mall')
        response = self.propose(venue_id=venue.pk)
        self.assertEqual(response.status_code, 201)
        event = ClubEvent.objects.get()
        self.assertEqual(event.venue_id, venue.pk)
        self.assertEqual(event.location_name, 'Esentai Mall')
        self.assertEqual(event.latitude, venue.latitude)
        self.assertEqual(event.longitude, venue.longitude)

    def test_seed_is_repeatable_without_overwriting_admin_edits(self):
        self.venue.description = 'Edited by staff'
        self.venue.save()
        import_module('venues.migrations.0002_seed_dostyk_plaza').seed_dostyk(
            apps, type('Editor', (), {'connection': connection})())
        self.venue.refresh_from_db()
        self.assertEqual(self.venue.description, 'Edited by staff')
        self.assertEqual(Venue.objects.count(), 3 + len(DUBAI_MARINA_VENUES))

    def test_api_is_authenticated_and_read_only(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get('/api/venues/').status_code, 401)
        self.assertEqual(self.client.get(f'/api/venues/{self.venue.pk}/').status_code, 401)
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.post('/api/venues/', {}, format='json').status_code, 405)
        self.assertEqual(self.client.patch(f'/api/venues/{self.venue.pk}/', {}, format='json').status_code, 405)
        self.assertEqual(self.client.delete(f'/api/venues/{self.venue.pk}/').status_code, 405)

    def test_inactive_venue_hidden_and_cannot_be_used(self):
        self.venue.is_active = False
        self.venue.save()
        rows = self.client.get('/api/venues/').data['results']
        self.assertNotIn(self.venue.pk, [row['id'] for row in rows])
        self.assertEqual(self.client.get(f'/api/venues/{self.venue.pk}/').status_code, 404)
        self.assertEqual(self.propose().status_code, 404)
        self.assertFalse(ClubEvent.objects.exists())
        self.assertFalse(ClubMessage.objects.exists())

    def test_venue_location_is_authoritative_and_proposal_still_pending(self):
        response = self.propose(location_name='Spoofed', latitude=0, longitude=0,
                                meeting_point='  Main entrance  ')
        self.assertEqual(response.status_code, 201, response.data)
        event = ClubEvent.objects.get()
        self.assertEqual(event.venue, self.venue)
        self.assertEqual(event.location_name, 'Dostyk Plaza')
        self.assertEqual(event.latitude, self.venue.latitude)
        self.assertEqual(event.longitude, self.venue.longitude)
        self.assertEqual(event.meeting_point, 'Main entrance')
        self.assertEqual(event.status, ClubEvent.Status.PENDING)
        self.assertEqual(ClubMessage.objects.get().event_id, event.pk)
        row = self.client.get(self.events_url).data[0]
        self.assertEqual(row['venue'], self.venue.pk)
        self.assertEqual(row['meeting_point'], 'Main entrance')

    def test_membership_required_even_with_valid_venue(self):
        ClubMember.objects.all().delete()
        self.assertEqual(self.propose().status_code, 403)
        self.assertFalse(ClubEvent.objects.exists())

    def test_invalid_or_missing_venue_and_oversized_note_rejected(self):
        for value in (None, [], {}, 'abc', -1, 0, 1.5):
            with self.subTest(value=value):
                self.assertEqual(self.propose(venue_id=value).status_code, 400)
        self.assertEqual(self.propose(venue_id=99999).status_code, 404)
        self.assertEqual(self.propose(meeting_point='x' * 201).status_code, 400)
        self.assertFalse(ClubEvent.objects.exists())

    def test_legacy_location_proposals_still_work(self):
        response = self.client.post(self.events_url, {
            'title': 'Park meetup', 'location_name': 'Park', 'latitude': 43.2, 'longitude': 76.9,
            'event_datetime': (timezone.now() + timedelta(days=1)).isoformat(),
        }, format='json')
        self.assertEqual(response.status_code, 201)
        event = ClubEvent.objects.get()
        self.assertIsNone(event.venue_id)
        self.assertEqual(event.location_name, 'Park')

    def test_deleting_venue_preserves_event_and_snapshot(self):
        self.assertEqual(self.propose().status_code, 201)
        self.venue.delete()
        event = ClubEvent.objects.get()
        self.assertIsNone(event.venue_id)
        self.assertEqual(event.location_name, 'Dostyk Plaza')
        self.assertEqual(event.latitude, Decimal('43.233139'))
        self.assertEqual(ClubMessage.objects.count(), 1)

    def test_admin_registration_and_coordinate_validation(self):
        self.assertIn(Venue, admin.site._registry)
        self.venue.latitude = 91
        with self.assertRaises(ValidationError):
            self.venue.full_clean()

    def test_list_is_bounded(self):
        seeded = Venue.objects.filter(is_active=True).count()
        Venue.objects.bulk_create([Venue(name=f'Place {i}', slug=f'place-{i}', address='Test',
            latitude=43, longitude=76, is_active=True) for i in range(101)])
        response = self.client.get('/api/venues/')
        self.assertEqual(len(response.data['results']), 100)
        self.assertIsNotNone(response.data['next'])
        self.assertEqual(len(self.client.get('/api/venues/?page=2').data['results']), seeded + 1)


class DubaiMarinaVenueTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='marina-member', email='marina@example.test')
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.club = Club.objects.create(name='Marina club', creator=self.user,
            visibility_level=Club.Visibility.GLOBAL)
        ClubMember.objects.create(club=self.club, user=self.user, role=ClubMember.Role.MEMBER)
        self.slugs = [row[0] for row in DUBAI_MARINA_VENUES]

    def test_seeded_live_with_valid_rows_in_dubai_marina(self):
        self.assertEqual(len(set(self.slugs)), len(self.slugs))
        venues = Venue.objects.filter(slug__in=self.slugs)
        self.assertEqual(venues.count(), len(self.slugs))
        for venue in venues:
            with self.subTest(slug=venue.slug):
                venue.full_clean()
                self.assertTrue(venue.is_active)
                self.assertTrue(venue.description)
                self.assertIn(venue.category, {'Shopping mall', 'Sport & fitness',
                                               'Beach & outdoors', 'Restaurant & cafe'})
                # Dubai Marina, JBR and Bluewaters; a swapped lat/lng lands elsewhere.
                self.assertTrue(Decimal('25.06') <= venue.latitude <= Decimal('25.10'))
                self.assertTrue(Decimal('55.11') <= venue.longitude <= Decimal('55.16'))

    def test_listed_and_retrievable_like_the_almaty_venues(self):
        listed = {row['slug']: row for row in self.client.get('/api/venues/').data['results']}
        for venue in Venue.objects.filter(slug__in=self.slugs):
            with self.subTest(slug=venue.slug):
                self.assertEqual(listed[venue.slug]['latitude'], float(venue.latitude))
                detail = self.client.get(f'/api/venues/{venue.pk}/')
                self.assertEqual(detail.status_code, 200)
                self.assertEqual(detail.data['name'], venue.name)

    def test_every_marina_venue_can_host_a_club_meetup(self):
        for venue in Venue.objects.filter(slug__in=self.slugs):
            with self.subTest(slug=venue.slug):
                response = self.client.post(f'/api/clubs/{self.club.pk}/events/', {
                    'title': 'Marina meetup', 'venue_id': venue.pk, 'meeting_point': 'Main entrance',
                    'location_name': 'Spoofed', 'latitude': 0, 'longitude': 0,
                    'event_datetime': (timezone.now() + timedelta(days=1)).isoformat(),
                }, format='json')
                self.assertEqual(response.status_code, 201, response.data)
                event = ClubEvent.objects.get(venue=venue)
                self.assertEqual(event.location_name, venue.name)
                self.assertEqual(event.latitude, venue.latitude)
                self.assertEqual(event.longitude, venue.longitude)
                self.assertEqual(event.status, ClubEvent.Status.PENDING)

    def test_seed_is_repeatable_without_overwriting_admin_edits(self):
        venue = Venue.objects.get(slug='pier-7')
        venue.description = 'Staff edit'
        venue.is_active = False
        venue.save()
        import_module('venues.migrations.0005_seed_dubai_marina').seed_dubai_marina(
            apps, type('Editor', (), {'connection': connection})())
        venue.refresh_from_db()
        self.assertEqual(venue.description, 'Staff edit')
        self.assertFalse(venue.is_active)
        self.assertEqual(Venue.objects.filter(slug__in=self.slugs).count(), len(self.slugs))
