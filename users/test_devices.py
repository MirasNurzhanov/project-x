"""Two devices on one account.

`User.push_token` held exactly one, so signing in on a second phone silently
took the first's notifications away and told nobody. These pin both halves of
the fix: push reaches every device, and a device the account has not been seen
on before announces itself to the ones that were already there.

Run with:  python manage.py test users.test_devices --settings=core.test_settings
"""
from datetime import date

from django.test import TestCase
from rest_framework.test import APIClient

from notifications.models import Notification
from notifications.push import push_to_users
from users.models import Device, User

DEVICES = '/api/users/devices/'
LOGOUT = '/api/users/logout/'


def make_user(name):
    return User.objects.create_user(
        username=name, email=f'{name}@example.com', password='pw',
        date_of_birth=date(1995, 1, 1),
    )


class DeviceRegistrationTests(TestCase):
    def setUp(self):
        self.user = make_user('owner')
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def register(self, install_id, **extra):
        return self.client.post(
            DEVICES, {'install_id': install_id, **extra}, format='json',
        )

    def alerts(self):
        return Notification.objects.filter(
            recipient=self.user, notification_type=Notification.Type.NEW_DEVICE,
        )

    def test_first_device_is_recorded(self):
        res = self.register('phone', push_token='tok-a', name='Pixel 8', platform='android')
        self.assertEqual(res.status_code, 201)
        device = Device.objects.get(user=self.user)
        self.assertEqual(device.install_id, 'phone')
        self.assertEqual(device.push_token, 'tok-a')
        self.assertEqual(device.platform, Device.Platform.ANDROID)

    def test_first_device_raises_no_alert(self):
        """Nobody to warn, and warning someone about their only phone is noise."""
        self.register('phone', push_token='tok-a')
        self.assertEqual(self.alerts().count(), 0)

    def test_second_device_alerts_the_account(self):
        self.register('phone', push_token='tok-a', name='Pixel 8')
        self.register('tablet', push_token='tok-b', name='iPad')

        alert = self.alerts().get()
        self.assertIn('iPad', alert.message)
        # About the account, not about a person — so there is nobody to open.
        self.assertIsNone(alert.sender)

    def test_re_registering_a_known_device_is_silent(self):
        """The token is reissued on Expo's schedule. If that counted as a new
        device, every reissue would fire a security warning at its owner."""
        self.register('phone', push_token='tok-a')
        self.register('tablet', push_token='tok-b')
        self.assertEqual(self.alerts().count(), 1)

        res = self.register('phone', push_token='tok-a-renewed')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.alerts().count(), 1)
        self.assertEqual(
            Device.objects.get(user=self.user, install_id='phone').push_token,
            'tok-a-renewed',
        )

    def test_an_empty_token_does_not_clear_a_working_one(self):
        """Push permission simply may not have been granted this launch."""
        self.register('phone', push_token='tok-a')
        self.register('phone')
        self.assertEqual(
            Device.objects.get(user=self.user, install_id='phone').push_token, 'tok-a',
        )

    def test_install_id_is_required(self):
        self.assertEqual(self.client.post(DEVICES, {}, format='json').status_code, 400)

    def test_the_same_install_on_two_accounts_is_two_devices(self):
        """One phone, two accounts — each account tracks its own sign-in."""
        other = make_user('other')
        self.register('phone', push_token='tok-a')

        self.client.force_authenticate(other)
        self.register('phone', push_token='tok-a')

        self.assertEqual(Device.objects.filter(install_id='phone').count(), 2)


class DevicePushFanOutTests(TestCase):
    def setUp(self):
        self.user = make_user('owner')

    def test_push_reaches_every_device(self):
        """The regression: one account, two phones, both notified."""
        Device.objects.create(user=self.user, install_id='a', push_token='tok-a')
        Device.objects.create(user=self.user, install_id='b', push_token='tok-b')

        self.assertEqual(push_to_users([self.user], 'hi', 'there'), 2)

    def test_a_device_without_a_token_is_skipped(self):
        Device.objects.create(user=self.user, install_id='a', push_token='tok-a')
        Device.objects.create(user=self.user, install_id='b', push_token='')

        self.assertEqual(push_to_users([self.user], 'hi', 'there'), 1)

    def test_one_token_on_two_accounts_is_pushed_once(self):
        """Two accounts signed in on one phone share a token, and a club
        message reaching both must not arrive twice."""
        other = make_user('other')
        Device.objects.create(user=self.user, install_id='a', push_token='shared')
        Device.objects.create(user=other, install_id='a', push_token='shared')

        self.assertEqual(push_to_users([self.user, other], 'hi', 'there'), 1)

    def test_falls_back_to_the_legacy_column(self):
        """A client too old to register a device still gets its notifications."""
        self.user.push_token = 'legacy'
        self.user.save(update_fields=['push_token'])

        self.assertEqual(push_to_users([self.user], 'hi', 'there'), 1)

    def test_devices_win_over_the_legacy_column(self):
        """Not both: the column is the same phone's older registration, and
        counting it too would push twice to one device."""
        self.user.push_token = 'legacy'
        self.user.save(update_fields=['push_token'])
        Device.objects.create(user=self.user, install_id='a', push_token='tok-a')

        self.assertEqual(push_to_users([self.user], 'hi', 'there'), 1)


class SignOutTests(TestCase):
    def setUp(self):
        self.user = make_user('owner')
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_signing_out_one_device_leaves_the_other_addressable(self):
        """The whole point of per-device tokens: leaving on the phone must not
        silence the tablet that is still signed in."""
        Device.objects.create(user=self.user, install_id='phone', push_token='tok-a')
        Device.objects.create(user=self.user, install_id='tablet', push_token='tok-b')

        self.client.post(LOGOUT, {'refresh': 'nonsense', 'install_id': 'phone'}, format='json')

        self.assertEqual(
            Device.objects.get(user=self.user, install_id='phone').push_token, '')
        self.assertEqual(
            Device.objects.get(user=self.user, install_id='tablet').push_token, 'tok-b')

    def test_the_row_survives_so_signing_back_in_is_not_announced(self):
        Device.objects.create(user=self.user, install_id='phone', push_token='tok-a')
        self.client.post(LOGOUT, {'refresh': 'nonsense', 'install_id': 'phone'}, format='json')

        self.assertTrue(
            Device.objects.filter(user=self.user, install_id='phone').exists())
