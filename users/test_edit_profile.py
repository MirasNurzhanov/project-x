"""PATCH /api/users/profile/ — changing your own username.

EditProfileSerializer used to omit `username` from its fields entirely, so a
PATCH that included it was accepted (DRF silently drops unknown fields on a
ModelSerializer) but never actually changed anything — the request looked
like it worked and the profile screen just kept showing the old name.
"""
from django.test import TestCase
from rest_framework.test import APIClient

from users.models import User


class UsernameChangeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='original', email='original@example.com', password='pw-123456')
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def patch(self, **body):
        return self.client.patch('/api/users/profile/', body, format='json')

    def test_username_actually_changes(self):
        res = self.patch(username='newname')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['username'], 'newname')
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, 'newname')

    def test_saving_your_own_unchanged_username_is_not_a_conflict(self):
        res = self.patch(username='original', bio='hello')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['username'], 'original')

    def test_refuses_a_name_already_taken(self):
        User.objects.create_user(
            username='taken', email='taken@example.com', password='pw-123456')
        res = self.patch(username='taken')
        self.assertEqual(res.status_code, 400)
        self.assertIn('username', res.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, 'original')

    def test_refuses_a_name_taken_in_a_different_case(self):
        # Matches socialauth's _unique_username, and how a handle is matched
        # everywhere else — two accounts that differ only by case are the same
        # collision to everyone but the database's own unique constraint.
        User.objects.create_user(
            username='CoolPerson', email='cool@example.com', password='pw-123456')
        res = self.patch(username='coolperson')
        self.assertEqual(res.status_code, 400)
        self.assertIn('username', res.data)

    def test_other_fields_still_save_without_a_username_in_the_body(self):
        res = self.patch(bio='just a bio update')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['bio'], 'just a bio update')
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, 'original')
