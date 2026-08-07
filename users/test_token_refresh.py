"""POST /api/users/token/refresh/ must never 500.

TokenRefreshSerializer.validate() (simplejwt 5.5.1) looks the token's user up
with a bare `User.objects.get()` — a token outliving its account crashes
uncaught instead of producing the clean 401 the surrounding code clearly
intends. SafeTokenRefreshView exists to close that gap.
"""
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from users.models import User


class TokenRefreshForDeletedUserTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_a_token_for_a_deleted_user_is_a_clean_401_not_a_500(self):
        user = User.objects.create_user(username='temp', email='temp@example.com', password='pw')
        refresh = str(RefreshToken.for_user(user))
        user.delete()

        response = self.client.post('/api/users/token/refresh/', {'refresh': refresh}, format='json')
        self.assertEqual(response.status_code, 401)

    def test_a_token_for_an_existing_user_still_refreshes_normally(self):
        user = User.objects.create_user(username='real', email='real@example.com', password='pw')
        refresh = str(RefreshToken.for_user(user))

        response = self.client.post('/api/users/token/refresh/', {'refresh': refresh}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertIn('access', response.json())
