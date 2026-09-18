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

    def test_seed_only_dostyk_and_numeric_coordinates(self):
        response = self.client.get('/api/venues/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 1)
        row = response.data['results'][0]
        self.assertEqual(row['name'], 'Dostyk Plaza')
        self.assertEqual(row['latitude'], 43.233139)
        self.assertEqual(row['longitude'], 76.956562)
        self.assertNotIn('google_place_id', row)

    def test_seed_is_repeatable_without_overwriting_admin_edits(self):
        self.venue.description = 'Edited by staff'
        self.venue.save()
        import_module('venues.migrations.0002_seed_dostyk_plaza').seed_dostyk(
            apps, type('Editor', (), {'connection': connection})())
        self.venue.refresh_from_db()
        self.assertEqual(self.venue.description, 'Edited by staff')
        self.assertEqual(Venue.objects.count(), 1)

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
        self.assertEqual(self.client.get('/api/venues/').data['count'], 0)
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
        Venue.objects.bulk_create([Venue(name=f'Place {i}', slug=f'place-{i}', address='Test',
            latitude=43, longitude=76, is_active=True) for i in range(101)])
        response = self.client.get('/api/venues/')
        self.assertEqual(len(response.data['results']), 100)
        self.assertIsNotNone(response.data['next'])
        self.assertEqual(len(self.client.get('/api/venues/?page=2').data['results']), 2)
