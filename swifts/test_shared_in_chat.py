"""A Swift sent to a friend, as the chat reads it back.

The card used to reach the app only while the reader could still watch the
Swift, and an unwatchable one looked exactly like an empty message — the app
drew a blank bubble, and the chat list called it a photo.
"""
from django.utils import timezone
from rest_framework.test import APIClient

from .testing import SwiftTestCase, befriend, connect, make_swift, make_user


class SharedSwiftInChatTests(SwiftTestCase):
    def setUp(self):
        super().setUp()
        self.sender = make_user('sender')
        self.friend = make_user('friend')
        connect(self.sender, self.friend)
        befriend(self.sender, self.friend)
        self.swift = make_swift(self.sender)
        response = self.client_for(self.sender).post(
            f'/api/swifts/{self.swift.pk}/send/', {'user_ids': [self.friend.pk]}, format='json',
        )
        self.assertEqual(response.status_code, 201, response.content)

    @staticmethod
    def client_for(user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def last_message(self):
        response = self.client_for(self.friend).get(f'/api/friends/messages/{self.sender.pk}/')
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()['results'][-1]

    def test_the_card_arrives_with_what_it_needs_to_draw(self):
        message = self.last_message()
        self.assertEqual(message['swift']['id'], self.swift.pk)
        self.assertEqual(message['swift']['author']['username'], 'sender')
        self.assertFalse(message['swift_unavailable'])

    def test_a_swift_that_is_gone_says_so_instead_of_reading_as_empty(self):
        self.swift.deleted_at = timezone.now()
        self.swift.save(update_fields=['deleted_at'])
        message = self.last_message()
        self.assertIsNone(message['swift'])
        self.assertTrue(message['swift_unavailable'])

    def test_the_chat_list_calls_it_a_swift_not_a_photo(self):
        response = self.client_for(self.friend).get('/api/friends/list/')
        row = next(p for p in response.json()['friends'] if p['id'] == self.sender.pk)
        self.assertEqual(row['last_message'], 'Sent a Swift')

    def test_ordinary_messages_are_never_unavailable_swifts(self):
        self.client_for(self.friend).post(
            f'/api/friends/messages/{self.sender.pk}/', {'content': 'nice'}, format='json',
        )
        self.assertFalse(self.last_message()['swift_unavailable'])
