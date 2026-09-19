from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from friends.models import FriendRequest
from notifications.models import Notification
from notifications.utils import notify_zone_music_entry
from proximity.serializers import LocationUpdateSerializer
from proximity.views import _trigger_zone_music_alerts
from users.models import User


class MusicZoneLocationTests(TestCase):
    def setUp(self):
        self.owner = self.make_user('owner', 10)
        self.visitor = self.make_user('visitor', 10.0001)
        FriendRequest.objects.create(
            sender=self.visitor, receiver=self.owner,
            status=FriendRequest.Status.ACCEPTED,
        )

    def make_user(self, name, latitude):
        return User.objects.create_user(
            username=name, email=f'{name}@example.com', password='testpass-123',
            latitude=latitude, longitude=10, ghost_mode=False,
            location_updated_at=timezone.now(), location_accuracy_m=5,
            music_zone_active=True, music_zone_radius=50, music_zone_alerts=True,
            zone_apple_preview_url='https://example.com/track.mp3',
        )

    def test_fresh_accurate_nearby_friend_receives_alert(self):
        _trigger_zone_music_alerts(self.visitor)
        self.assertEqual(Notification.objects.count(), 2)

    def test_far_friend_does_not_receive_alert_even_with_incorrect_caller_distance(self):
        self.visitor.latitude = 11
        notify_zone_music_entry(self.visitor, self.owner, 0)
        self.assertFalse(Notification.objects.exists())

    def test_stale_missing_or_future_position_is_not_presence(self):
        for person in (self.owner, self.visitor):
            original = person.location_updated_at
            for age in (121, 3600, -10, None):
                with self.subTest(person=person.username, age=age):
                    person.location_updated_at = (
                        None if age is None else timezone.now() - timedelta(seconds=age)
                    )
                    notify_zone_music_entry(self.visitor, self.owner, 0)
                    self.assertFalse(Notification.objects.exists())
            person.location_updated_at = original

    def test_uncertain_fixes_do_not_establish_entry(self):
        for person in (self.owner, self.visitor):
            for accuracy in (None, -1, float('nan'), float('inf'), 100):
                with self.subTest(person=person.username, accuracy=accuracy):
                    person.location_accuracy_m = accuracy
                    notify_zone_music_entry(self.visitor, self.owner, 0)
                    self.assertFalse(Notification.objects.exists())
            person.location_accuracy_m = 5

    def test_inside_center_distance_but_uncertain_boundary_is_not_entry(self):
        self.visitor.latitude = 10.0004  # ~44 m, plus 10 m combined uncertainty
        notify_zone_music_entry(self.visitor, self.owner, 44)
        self.assertFalse(Notification.objects.exists())

    def test_ghost_is_not_alerted(self):
        self.visitor.ghost_mode = True
        notify_zone_music_entry(self.visitor, self.owner, 0)
        self.assertFalse(Notification.objects.exists())

    def test_friend_last_known_pin_does_not_trigger_alert(self):
        self.owner.location_updated_at = timezone.now() - timedelta(hours=1)
        self.owner.save(update_fields=['location_updated_at'])
        _trigger_zone_music_alerts(self.visitor)
        self.assertFalse(Notification.objects.exists())

    def test_location_upload_preserves_measurement_time_and_clears_old_accuracy(self):
        measured_at = timezone.now() - timedelta(seconds=60)
        client = APIClient()
        client.force_authenticate(self.visitor)
        response = client.patch('/api/proximity/location/', {
            'latitude': 10.0001, 'longitude': 10,
            'measured_at': measured_at.isoformat(),
        }, format='json')
        self.assertEqual(response.status_code, 200)
        self.visitor.refresh_from_db()
        self.assertEqual(self.visitor.location_updated_at, measured_at)
        self.assertIsNone(self.visitor.location_accuracy_m)
        self.assertFalse(Notification.objects.filter(notification_type='zone_music').exists())

    def test_invalid_measurement_times_are_rejected(self):
        for age in (121, -10):
            serializer = LocationUpdateSerializer(data={
                'latitude': 10, 'longitude': 10,
                'measured_at': (timezone.now() - timedelta(seconds=age)).isoformat(),
            })
            self.assertFalse(serializer.is_valid())
            self.assertIn('measured_at', serializer.errors)
