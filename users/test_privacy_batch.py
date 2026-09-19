from datetime import date
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from moderation.text import normalise
from proximity.serializers import NearbyUserSerializer
from users.ages import age_in_years
from users.models import User
from users.serializers import PublicProfileSerializer
from users.testing import share_location


class AgeAndAccuracyTests(TestCase):
    def setUp(self):
        self.viewer = User.objects.create_user(username='viewer', email='viewer@example.test')
        self.subject = User.objects.create_user(
            username='subject', email='subject@example.test',
            date_of_birth=date(1995, 6, 15), latitude=10, longitude=20,
        )
        share_location(self.viewer)
        share_location(self.subject)
        self.client = APIClient()
        self.client.force_authenticate(self.subject)

    def test_stranger_serializers_return_age_never_birthday(self):
        for serializer in (PublicProfileSerializer, NearbyUserSerializer):
            data = serializer(self.subject, context={'request': SimpleNamespace(user=self.viewer)}).data
            self.assertNotIn('date_of_birth', data)
            self.assertEqual(data['age'], age_in_years(self.subject.date_of_birth))
        self.assertEqual(self.client.get('/api/users/profile/').data['date_of_birth'], '1995-06-15')

    def test_public_profile_endpoint_omits_exact_birthday(self):
        self.client.force_authenticate(self.viewer)
        response = self.client.get(f'/api/users/{self.subject.pk}/profile/')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('date_of_birth', response.data)
        self.assertNotIn('1995-06-15', response.content.decode())

    def test_new_fix_does_not_inherit_previous_accuracy(self):
        self.subject.location_accuracy_m = 17
        self.subject.save(update_fields=['location_accuracy_m'])
        for payload, expected in [({}, None), ({'accuracy': 6}, 6), ({'accuracy': 0}, 0),
                                  ({'accuracy': None}, None), ({'accuracy': -1}, None)]:
            response = self.client.patch('/api/proximity/location/', {
                'latitude': 10, 'longitude': 20, **payload,
            }, format='json')
            self.assertEqual(response.status_code, 200)
            self.subject.refresh_from_db()
            self.assertEqual(self.subject.location_accuracy_m, expected)


class AgeAndUnicodeTests(SimpleTestCase):
    def test_age_uses_calendar_birthday(self):
        self.assertEqual(age_in_years(date(2000, 9, 15), date(2026, 9, 14)), 25)
        self.assertEqual(age_in_years(date(2000, 9, 15), date(2026, 9, 15)), 26)
        self.assertEqual(age_in_years(date(2000, 2, 29), date(2025, 3, 1)), 25)
        self.assertIsNone(age_in_years(None))
        self.assertIsNone(age_in_years(date(2099, 1, 1), date(2026, 1, 1)))

    def test_non_latin_words_and_marks_survive(self):
        for text in ('Привет мир', 'Қазақ тілі', '日本語 中文', 'مرحبا بالعالم', 'नमस्ते दुनिया'):
            spaced, joined = normalise(text)
            self.assertEqual(spaced, text.casefold())
            self.assertEqual(joined, text.casefold().replace(' ', ''))
        self.assertEqual(normalise('й ё')[0], 'й ё')
        self.assertEqual(normalise('nàïve')[0], 'naive')
        self.assertEqual(normalise('при\u200bвет')[0], 'привет')
