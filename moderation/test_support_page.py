"""The App Store's Support URL: /legal/support/."""
from django.test import TestCase, override_settings

from users.deletion import GRACE_DAYS


class SupportPageTests(TestCase):
    @override_settings(SAFETY_CONTACT_EMAIL='help@example.test')
    def test_it_is_public_and_says_how_to_reach_us(self):
        response = self.client.get('/legal/support/', secure=True)
        self.assertEqual(response.status_code, 200)
        page = response.content.decode()
        self.assertIn('mailto:help@example.test', page)
        self.assertIn('Delete account', page)
        self.assertIn(f'{GRACE_DAYS} days later', page)
        # A help page, not a versioned document.
        self.assertNotIn('Last updated', page)

    def test_every_legal_page_links_to_it(self):
        for path in ('/legal/terms/', '/legal/guidelines/', '/legal/privacy/'):
            with self.subTest(path=path):
                page = self.client.get(path, secure=True).content.decode()
                self.assertIn('href="/legal/support/"', page)
