"""Owners rename a club and change its emblem from Manage; nobody else can."""
from io import BytesIO

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from PIL import Image
from rest_framework.test import APIClient

from users.models import User
from .models import Club, ClubMember


def png(name='emblem.png'):
    output = BytesIO()
    Image.new('RGB', (4, 4), 'teal').save(output, format='PNG')
    return SimpleUploadedFile(name, output.getvalue(), content_type='image/png')


class ClubEditTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.creator = User.objects.create_user(username='creator', email='creator@example.test')
        self.co_owner = User.objects.create_user(username='co', email='co@example.test')
        self.member = User.objects.create_user(username='member', email='member@example.test')
        self.club = Club.objects.create(name='Runners', creator=self.creator)
        ClubMember.objects.create(club=self.club, user=self.creator, role=ClubMember.Role.OWNER)
        ClubMember.objects.create(club=self.club, user=self.co_owner, role=ClubMember.Role.OWNER)
        ClubMember.objects.create(club=self.club, user=self.member)
        Club.objects.create(name='Climbers', creator=self.member)

    def edit(self, user, data, fmt='json'):
        client = APIClient()
        client.force_authenticate(user)
        return client.patch(f'/api/clubs/{self.club.pk}/', data, format=fmt)

    def test_an_owner_renames_the_club(self):
        response = self.edit(self.co_owner, {'name': '  Morning Runners '})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['name'], 'Morning Runners')
        self.club.refresh_from_db()
        self.assertEqual(self.club.name, 'Morning Runners')

    def test_names_stay_unique_and_present(self):
        self.assertEqual(self.edit(self.creator, {'name': 'climbers'}).status_code, 400)
        self.assertEqual(self.edit(self.creator, {'name': '   '}).status_code, 400)
        self.assertEqual(self.edit(self.creator, {'name': 'x' * 101}).status_code, 400)
        # Its own name, in a different case, is not a clash.
        self.assertEqual(self.edit(self.creator, {'name': 'RUNNERS'}).status_code, 200)

    def test_an_owner_changes_the_emblem(self):
        response = self.edit(self.creator, {'emblem': png()}, fmt='multipart')
        self.assertEqual(response.status_code, 200, response.content)
        self.club.refresh_from_db()
        self.assertTrue(self.club.emblem.name)
        self.assertIsNotNone(response.json()['emblem'])

    def test_a_file_that_is_not_an_image_is_refused(self):
        bogus = SimpleUploadedFile('emblem.png', b'not an image', content_type='image/png')
        self.assertEqual(self.edit(self.creator, {'emblem': bogus}, fmt='multipart').status_code, 400)

    def test_members_cannot_change_the_club(self):
        self.assertEqual(self.edit(self.member, {'name': 'Mine now'}).status_code, 403)
        self.club.refresh_from_db()
        self.assertEqual(self.club.name, 'Runners')

    def test_an_empty_edit_says_so(self):
        self.assertEqual(self.edit(self.creator, {}).status_code, 400)
