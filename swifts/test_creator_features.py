"""The composer's feature set: capture settings, music, stickers, tags, sharing.

Four groups, and the ones worth reading are the two where a rule is not
obvious from the field list:

* `SpeedCeilingTests` — speed and the sixty-second maximum interact, because
  the ceiling applies to what a viewer *watches* rather than to how much source
  was kept. Every other duration test in this suite predates speed existing and
  passes unchanged only because 100% is the identity.

* `MentionNotificationTests` — a mention is notified when the Swift becomes
  watchable, not when it is created, and with AI moderation on those are
  different moments.
"""
from unittest.mock import patch

from django.urls import reverse
from rest_framework.test import APIClient

from friends.models import Message
from moderation.reasons import Moderation
from notifications.models import Notification

from . import capture, tagging
from .models import Hashtag, SwiftHashtag, SwiftMention, SwiftTrack, SwiftVideo
from .testing import (
    SwiftTestCase,
    block,
    connect,
    fake_video,
    make_swift,
    make_user,
)


def publish_payload(**extra):
    """The minimum a publish needs, plus whatever the test is about."""
    payload = {
        'video': fake_video(),
        'duration_ms': 10_000,
        'caption': '',
    }
    payload.update(extra)
    return payload


class CaptureSettingsTests(SwiftTestCase):
    """Speed, rotation and the colour effect, stored as parameters."""

    def setUp(self):
        super().setUp()
        self.author = make_user('capture_author')
        self.client = APIClient()
        self.client.force_authenticate(self.author)

    def test_settings_round_trip(self):
        response = self.client.post(reverse('swift-create'), publish_payload(
            speed_percent=200, rotation_deg=90, effect='warm', effect_intensity=60,
        ))
        self.assertEqual(response.status_code, 201, response.data)

        swift = SwiftVideo.objects.get(pk=response.data['id'])
        self.assertEqual(swift.speed_percent, 200)
        self.assertEqual(swift.rotation_deg, 90)
        self.assertEqual(swift.effect, 'warm')
        self.assertEqual(swift.effect_intensity, 60)
        # And the player is actually told, which is the whole point of storing
        # them — a parameter the feed does not serialize is a parameter that
        # does nothing.
        self.assertEqual(response.data['speed_percent'], 200)
        self.assertEqual(response.data['rotation_deg'], 90)
        self.assertEqual(response.data['effect'], 'warm')

    def test_defaults_are_the_identity(self):
        """A publish that says nothing about capture plays untouched."""
        response = self.client.post(reverse('swift-create'), publish_payload())
        self.assertEqual(response.status_code, 201, response.data)
        swift = SwiftVideo.objects.get(pk=response.data['id'])
        self.assertEqual(swift.speed_percent, 100)
        self.assertEqual(swift.rotation_deg, 0)
        self.assertEqual(swift.effect, '')

    def test_speed_outside_the_range_is_refused(self):
        for bad in (10, 500):
            with self.subTest(speed=bad):
                response = self.client.post(
                    reverse('swift-create'), publish_payload(speed_percent=bad))
                self.assertEqual(response.status_code, 400)
                self.assertIn('speed_percent', response.data)

    def test_rotation_must_be_a_quarter_turn(self):
        response = self.client.post(
            reverse('swift-create'), publish_payload(rotation_deg=45))
        self.assertEqual(response.status_code, 400)
        self.assertIn('rotation_deg', response.data)

    def test_unknown_effect_is_refused(self):
        """A typo'd key would play unfiltered for everyone, silently."""
        response = self.client.post(
            reverse('swift-create'), publish_payload(effect='vintagegrain'))
        self.assertEqual(response.status_code, 400)
        self.assertIn('effect', response.data)

    def test_every_shipped_effect_is_accepted(self):
        for key in capture.EFFECT_KEYS:
            with self.subTest(effect=key):
                response = self.client.post(
                    reverse('swift-create'), publish_payload(effect=key))
                self.assertEqual(response.status_code, 201, response.data)


class SpeedCeilingTests(SwiftTestCase):
    """Speed and the sixty-second maximum.

    The ceiling is on played length, so the same trim is legal or not depending
    on the speed applied to it. These are the cases that would silently publish
    a two-minute Swift if the conversion were missing.
    """

    def setUp(self):
        super().setUp()
        self.author = make_user('speed_author')
        self.client = APIClient()
        self.client.force_authenticate(self.author)

    def test_played_and_recorded_are_inverses(self):
        for speed in capture.SPEED_CHOICES:
            with self.subTest(speed=speed):
                self.assertEqual(
                    capture.recorded_ms(capture.played_ms(45_000, speed), speed),
                    45_000,
                )

    def test_long_trim_is_legal_when_sped_up(self):
        """90 seconds of source at 150% is a 60-second Swift."""
        response = self.client.post(reverse('swift-create'), publish_payload(
            duration_ms=60_000, trim_start_ms=0, trim_end_ms=90_000, speed_percent=150,
        ))
        self.assertEqual(response.status_code, 201, response.data)

    def test_short_trim_is_refused_when_slowed_down(self):
        """45 seconds of source at 50% is a 90-second Swift, and is not."""
        response = self.client.post(reverse('swift-create'), publish_payload(
            duration_ms=60_000, trim_start_ms=0, trim_end_ms=45_000, speed_percent=50,
        ))
        self.assertEqual(response.status_code, 400)
        self.assertIn('trim_end_ms', response.data)

    def test_ceiling_still_holds_at_normal_speed(self):
        """The pre-existing rule, unchanged: 100% is the identity."""
        response = self.client.post(reverse('swift-create'), publish_payload(
            duration_ms=60_000, trim_start_ms=0, trim_end_ms=61_000,
        ))
        self.assertEqual(response.status_code, 400)
        self.assertIn('trim_end_ms', response.data)

    def test_duration_ms_ceiling_is_independent_of_speed(self):
        """`duration_ms` is already the played length, so it is capped flat."""
        response = self.client.post(
            reverse('swift-create'), publish_payload(duration_ms=61_000))
        self.assertEqual(response.status_code, 400)
        self.assertIn('duration_ms', response.data)


class StickerTests(SwiftTestCase):
    """`stickers` is a schemaless JSON column, so the validator is the schema."""

    def setUp(self):
        super().setUp()
        self.author = make_user('sticker_author')
        self.client = APIClient()
        self.client.force_authenticate(self.author)

    def _publish_multipart(self, **extra):
        return self.client.post(reverse('swift-create'), publish_payload(**extra))

    def test_stickers_survive_a_multipart_publish(self):
        """The real path: a publish is multipart, so `stickers` arrives as text.

        Worth its own test rather than trusting the framework. A JSON column
        fed from a form field is one of the few places DRF has to *parse*
        rather than pass through, and if it did not, the column would store the
        literal string — which serializes back out as a string, and every
        client drawing `stickers.map(...)` would iterate its characters.
        """
        import json

        placed = [{'emoji': '🔥', 'x': 0.25, 'y': 0.75, 'scale': 1.5, 'rotation': 30.0}]
        response = self.client.post(
            reverse('swift-create'), publish_payload(stickers=json.dumps(placed)))
        self.assertEqual(response.status_code, 201, response.data)

        swift = SwiftVideo.objects.get(pk=response.data['id'])
        self.assertIsInstance(swift.stickers, list)
        self.assertEqual(swift.stickers, placed)
        self.assertEqual(response.data['stickers'][0]['emoji'], '🔥')

    def test_a_malformed_sticker_payload_is_refused(self):
        response = self.client.post(
            reverse('swift-create'), publish_payload(stickers='not json at all'))
        self.assertEqual(response.status_code, 400)
        self.assertIn('stickers', response.data)

    def test_defaults_to_no_stickers(self):
        response = self._publish_multipart()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(SwiftVideo.objects.get(pk=response.data['id']).stickers, [])

    def test_unknown_keys_are_dropped(self):
        """Anything the validator lets past is stored verbatim and served."""
        from .serializers import SwiftCreateSerializer

        serializer = SwiftCreateSerializer()
        cleaned = serializer.validate_stickers([{
            'emoji': '🔥', 'x': 0.2, 'y': 0.3, 'scale': 2.0, 'rotation': 15.0,
            'onTap': 'javascript:alert(1)', 'href': 'https://example.test',
        }])
        self.assertEqual(
            set(cleaned[0]), {'emoji', 'x', 'y', 'scale', 'rotation'})

    def test_out_of_range_positions_are_clamped_not_refused(self):
        from .serializers import SwiftCreateSerializer

        cleaned = SwiftCreateSerializer().validate_stickers([
            {'emoji': '🎈', 'x': 1.4, 'y': -0.2, 'scale': 99, 'rotation': 900},
        ])
        self.assertEqual(cleaned[0]['x'], 1.0)
        self.assertEqual(cleaned[0]['y'], 0.0)
        self.assertEqual(cleaned[0]['scale'], 5.0)
        self.assertEqual(cleaned[0]['rotation'], 180.0)

    def test_non_numeric_position_falls_back(self):
        from .serializers import SwiftCreateSerializer

        cleaned = SwiftCreateSerializer().validate_stickers([
            {'emoji': '🎈', 'x': 'left', 'y': None, 'scale': float('nan')},
        ])
        self.assertEqual(cleaned[0]['x'], 0.5)
        self.assertEqual(cleaned[0]['y'], 0.5)
        self.assertEqual(cleaned[0]['scale'], 1.0)

    def test_too_many_stickers_is_refused(self):
        from rest_framework import serializers as drf
        from .serializers import SwiftCreateSerializer

        too_many = [{'emoji': '🎈'}] * (capture.MAX_STICKERS + 1)
        with self.assertRaises(drf.ValidationError):
            SwiftCreateSerializer().validate_stickers(too_many)

    def test_text_smuggled_as_an_emoji_is_refused(self):
        from rest_framework import serializers as drf
        from .serializers import SwiftCreateSerializer

        with self.assertRaises(drf.ValidationError):
            SwiftCreateSerializer().validate_stickers([{'emoji': 'x' * 400}])

    def test_a_sticker_needs_an_emoji(self):
        from rest_framework import serializers as drf
        from .serializers import SwiftCreateSerializer

        with self.assertRaises(drf.ValidationError):
            SwiftCreateSerializer().validate_stickers([{'x': 0.5}])


class MusicTests(SwiftTestCase):
    """Attaching a catalogue track, which is a playback parameter now."""

    def setUp(self):
        super().setUp()
        self.author = make_user('music_author')
        self.client = APIClient()
        self.client.force_authenticate(self.author)
        self.track = SwiftTrack.objects.create(
            title='Almaty Nights', artist='Kaz Collective', genre='house',
            audio_url='https://cdn.example.test/t/1.mp3',
            artwork_url='https://cdn.example.test/t/1.jpg',
            duration_ms=180_000, source=SwiftTrack.Source.OPEN,
            licence='CC BY 4.0', is_active=True,
        )

    def test_track_attaches_and_credit_is_flattened(self):
        response = self.client.post(reverse('swift-create'), publish_payload(
            track=self.track.pk, music_volume=40, music_start_ms=30_000,
            uses_original_audio=False,
        ))
        self.assertEqual(response.status_code, 201, response.data)

        swift = SwiftVideo.objects.get(pk=response.data['id'])
        self.assertEqual(swift.track_id, self.track.pk)
        self.assertEqual(swift.music_volume, 40)
        self.assertEqual(swift.music_start_ms, 30_000)
        # The attribution is copied, not looked up — see `SwiftVideo.track`.
        self.assertEqual(swift.audio_title, 'Almaty Nights')
        self.assertEqual(swift.audio_artist, 'Kaz Collective')

    def test_payload_carries_what_the_player_needs(self):
        response = self.client.post(reverse('swift-create'), publish_payload(
            track=self.track.pk, music_volume=40, music_start_ms=30_000,
        ))
        self.assertEqual(response.data['track']['audio_url'],
                         'https://cdn.example.test/t/1.mp3')
        self.assertEqual(response.data['music_volume'], 40)
        self.assertEqual(response.data['music_start_ms'], 30_000)

    def test_inactive_track_cannot_be_attached(self):
        """A track is unvetted until somebody publishes it."""
        self.track.is_active = False
        self.track.save(update_fields=['is_active'])
        response = self.client.post(
            reverse('swift-create'), publish_payload(track=self.track.pk))
        self.assertEqual(response.status_code, 400)
        self.assertIn('track', response.data)

    def test_credit_survives_a_withdrawn_track(self):
        response = self.client.post(reverse('swift-create'), publish_payload(
            track=self.track.pk, music_volume=40,
        ))
        swift_id = response.data['id']
        self.track.delete()

        detail = self.client.get(reverse('swift-detail', args=[swift_id]))
        self.assertEqual(detail.data['track']['title'], 'Almaty Nights')
        # Nothing left to play, but the credit stands.
        self.assertEqual(detail.data['track']['audio_url'], '')

    def test_offset_without_a_track_is_refused(self):
        response = self.client.post(
            reverse('swift-create'), publish_payload(music_start_ms=5_000))
        self.assertEqual(response.status_code, 400)
        self.assertIn('music_start_ms', response.data)


class HashtagTests(SwiftTestCase):
    def setUp(self):
        super().setUp()
        self.author = make_user('tag_author')
        self.client = APIClient()
        self.client.force_authenticate(self.author)

    def test_tags_are_parsed_out_of_the_caption(self):
        response = self.client.post(reverse('swift-create'), publish_payload(
            caption='sunset at #KokTobe with #friends'))
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            [t['name'] for t in response.data['hashtags']], ['koktobe', 'friends'])

    def test_case_variants_are_one_tag(self):
        for caption in ('#Astana', '#ASTANA', '#astana'):
            self.client.post(
                reverse('swift-create'), publish_payload(caption=caption))
        self.assertEqual(Hashtag.objects.filter(name='astana').count(), 1)
        self.assertEqual(Hashtag.objects.get(name='astana').swift_count, 3)
        # The first spelling is what gets shown back.
        self.assertEqual(Hashtag.objects.get(name='astana').display, 'Astana')

    def test_client_list_is_merged_with_the_caption(self):
        response = self.client.post(
            reverse('swift-create'),
            publish_payload(caption='at #koktobe', hashtags=['sunset', '#koktobe']),
        )
        self.assertEqual(response.status_code, 201, response.data)
        # Caption first, client's addition after, and no duplicate.
        self.assertEqual(
            [t['name'] for t in response.data['hashtags']], ['koktobe', 'sunset'])

    def test_digits_only_is_not_a_tag(self):
        response = self.client.post(
            reverse('swift-create'), publish_payload(caption='in #2024 I went'))
        self.assertEqual(response.data['hashtags'], [])

    def test_mid_word_hash_is_not_a_tag(self):
        response = self.client.post(
            reverse('swift-create'), publish_payload(caption='ab#cd'))
        self.assertEqual(response.data['hashtags'], [])

    def test_tag_cap(self):
        caption = ' '.join(f'#tag{i}' for i in range(tagging.MAX_HASHTAGS + 5))
        response = self.client.post(
            reverse('swift-create'), publish_payload(caption=caption))
        self.assertEqual(len(response.data['hashtags']), tagging.MAX_HASHTAGS)

    def test_blocked_tag_is_skipped_without_failing_the_publish(self):
        Hashtag.objects.create(name='banned', display='banned', is_blocked=True)
        response = self.client.post(
            reverse('swift-create'), publish_payload(caption='#banned and #fine'))
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual([t['name'] for t in response.data['hashtags']], ['fine'])

    def test_count_is_given_back_on_delete(self):
        response = self.client.post(
            reverse('swift-create'), publish_payload(caption='#koktobe'))
        swift_id = response.data['id']
        self.assertEqual(Hashtag.objects.get(name='koktobe').swift_count, 1)

        self.client.delete(reverse('swift-detail', args=[swift_id]))
        self.assertEqual(Hashtag.objects.get(name='koktobe').swift_count, 0)
        # The link row stays: the tag was part of what was posted.
        self.assertTrue(SwiftHashtag.objects.filter(swift_id=swift_id).exists())

    def test_count_never_goes_negative(self):
        tag = Hashtag.objects.create(name='drifted', display='drifted', swift_count=0)
        swift = make_swift(self.author)
        SwiftHashtag.objects.create(swift=swift, hashtag=tag)
        tagging.release(swift)
        self.assertEqual(Hashtag.objects.get(pk=tag.pk).swift_count, 0)

    def test_suggestions_are_trending_when_nothing_is_typed(self):
        Hashtag.objects.create(name='busy', display='busy', swift_count=40)
        Hashtag.objects.create(name='quiet', display='quiet', swift_count=1)
        Hashtag.objects.create(name='unused', display='unused', swift_count=0)
        Hashtag.objects.create(name='hidden', display='hidden',
                               swift_count=99, is_blocked=True)

        response = self.client.get(reverse('swift-hashtags'))
        names = [t['name'] for t in response.data['results']]
        self.assertEqual(names, ['busy', 'quiet'])

    def test_suggestions_match_a_prefix(self):
        Hashtag.objects.create(name='astana', display='astana', swift_count=5)
        Hashtag.objects.create(name='breakfast', display='breakfast', swift_count=90)

        response = self.client.get(reverse('swift-hashtags'), {'q': 'ast'})
        self.assertEqual([t['name'] for t in response.data['results']], ['astana'])


class MentionTests(SwiftTestCase):
    def setUp(self):
        super().setUp()
        self.author = make_user('mention_author')
        self.friend = make_user('aisha')
        self.client = APIClient()
        self.client.force_authenticate(self.author)

    def test_mention_resolves_and_notifies(self):
        response = self.client.post(
            reverse('swift-create'), publish_payload(caption='with @aisha'))
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            response.data['mentions'],
            [{'user_id': self.friend.pk, 'username': 'aisha'}],
        )
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.friend, sender=self.author,
                notification_type=Notification.Type.SWIFT_MENTION,
            ).exists()
        )

    def test_mention_matches_case_insensitively(self):
        response = self.client.post(
            reverse('swift-create'), publish_payload(caption='hey @Aisha'))
        self.assertEqual(len(response.data['mentions']), 1)
        # The row keeps the real username, not the way it was typed.
        self.assertEqual(response.data['mentions'][0]['username'], 'aisha')

    def test_unknown_handle_is_left_as_text(self):
        response = self.client.post(
            reverse('swift-create'), publish_payload(caption='hi @nobody_here'))
        self.assertEqual(response.data['mentions'], [])

    def test_author_cannot_mention_themselves(self):
        response = self.client.post(
            reverse('swift-create'),
            publish_payload(caption='me: @mention_author'))
        self.assertEqual(response.data['mentions'], [])

    def test_blocked_author_cannot_reach_someone(self):
        """A mention is a way to reach somebody who said not to be."""
        block(self.friend, self.author)
        response = self.client.post(
            reverse('swift-create'), publish_payload(caption='@aisha look'))
        self.assertEqual(response.data['mentions'], [])
        self.assertFalse(Notification.objects.filter(recipient=self.friend).exists())

    def test_client_list_is_merged(self):
        other = make_user('dias')
        response = self.client.post(
            reverse('swift-create'),
            publish_payload(caption='with @aisha', mentions=['dias']),
        )
        self.assertEqual(
            [m['username'] for m in response.data['mentions']], ['aisha', 'dias'])
        self.assertTrue(Notification.objects.filter(recipient=other).exists())

    def test_mention_cap(self):
        for i in range(tagging.MAX_MENTIONS + 3):
            make_user(f'person{i}')
        caption = ' '.join(f'@person{i}' for i in range(tagging.MAX_MENTIONS + 3))
        response = self.client.post(
            reverse('swift-create'), publish_payload(caption=caption))
        self.assertEqual(len(response.data['mentions']), tagging.MAX_MENTIONS)


class MentionNotificationTimingTests(SwiftTestCase):
    """A mention is announced when the Swift becomes watchable, not before.

    With AI moderation on, a freshly posted Swift is PENDING and nobody but the
    author can open it. An alert sent then would lead to a 404.
    """

    def setUp(self):
        super().setUp()
        self.author = make_user('pending_author')
        self.friend = make_user('bekzat')
        self.client = APIClient()
        self.client.force_authenticate(self.author)

    def test_pending_swift_stores_the_mention_but_says_nothing(self):
        with patch('swifts.ai_moderation.enabled', return_value=True), \
             patch('swifts.ai_moderation.enqueue', return_value=None):
            response = self.client.post(
                reverse('swift-create'), publish_payload(caption='@bekzat'))

        self.assertEqual(response.status_code, 201, response.data)
        swift = SwiftVideo.objects.get(pk=response.data['id'])
        self.assertEqual(swift.moderation, Moderation.PENDING)

        mention = SwiftMention.objects.get(swift=swift)
        self.assertFalse(mention.notified)
        self.assertFalse(Notification.objects.filter(recipient=self.friend).exists())

    def test_approval_sends_the_held_alert(self):
        with patch('swifts.ai_moderation.enabled', return_value=True), \
             patch('swifts.ai_moderation.enqueue', return_value=None):
            response = self.client.post(
                reverse('swift-create'), publish_payload(caption='@bekzat'))
        swift = SwiftVideo.objects.get(pk=response.data['id'])

        from . import ai_moderation
        ai_moderation._approve(swift)

        self.assertTrue(SwiftMention.objects.get(swift=swift).notified)
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.friend,
                notification_type=Notification.Type.SWIFT_MENTION,
            ).exists()
        )

    def test_notifying_twice_sends_one_alert(self):
        response = self.client.post(
            reverse('swift-create'), publish_payload(caption='@bekzat'))
        swift = SwiftVideo.objects.get(pk=response.data['id'])

        tagging.notify_pending(swift)
        tagging.notify_pending(swift)

        self.assertEqual(
            Notification.objects.filter(
                recipient=self.friend,
                notification_type=Notification.Type.SWIFT_MENTION,
            ).count(),
            1,
        )


class SendSwiftTests(SwiftTestCase):
    """Forwarding a Swift into somebody's DMs."""

    def setUp(self):
        super().setUp()
        self.author = make_user('sharer')
        self.friend = make_user('recipient')
        self.stranger = make_user('stranger')
        connect(self.author, self.friend)
        self.swift = make_swift(self.author)
        self.client = APIClient()
        self.client.force_authenticate(self.author)

    def _send(self, *users):
        return self.client.post(
            reverse('swift-send', args=[self.swift.pk]),
            {'user_ids': [u.pk for u in users]},
            content_type='application/json',
        )

    def test_send_creates_a_message_carrying_the_swift(self):
        response = self._send(self.friend)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(len(response.data['sent']), 1)

        message = Message.objects.get(sender=self.author, receiver=self.friend)
        self.assertEqual(message.swift_id, self.swift.pk)

    def test_recipient_sees_the_card(self):
        self._send(self.friend)
        self.client = APIClient()
        self.client.force_authenticate(self.friend)
        response = self.client.get(reverse('messages', args=[self.author.pk]))
        card = response.data['results'][-1]['swift']
        self.assertEqual(card['id'], self.swift.pk)
        self.assertEqual(card['author']['username'], 'sharer')

    def test_card_stops_rendering_once_the_swift_is_gone(self):
        """Audience is re-checked at read time, not frozen at send time."""
        self._send(self.friend)
        self.swift.visibility = SwiftVideo.Visibility.PRIVATE
        self.swift.save(update_fields=['visibility'])

        self.client = APIClient()
        self.client.force_authenticate(self.friend)
        response = self.client.get(reverse('messages', args=[self.author.pk]))
        self.assertIsNone(response.data['results'][-1]['swift'])

    def test_cannot_send_to_someone_you_are_not_connected_to(self):
        response = self._send(self.stranger)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(response.data['sent']), 0)
        self.assertEqual(len(response.data['refused']), 1)

    def test_cannot_forward_a_swift_the_recipient_could_not_watch(self):
        """A friends-only Swift must not reach a non-friend of its author."""
        other_author = make_user('other_author')
        private_swift = make_swift(
            other_author, visibility=SwiftVideo.Visibility.FRIENDS)
        # The sender can watch it, the recipient cannot.
        from .testing import befriend
        befriend(other_author, self.author)

        response = self.client.post(
            reverse('swift-send', args=[private_swift.pk]),
            {'user_ids': [self.friend.pk]},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Message.objects.filter(swift=private_swift).exists())

    def test_partial_success_is_reported_as_one(self):
        second = make_user('second_friend')
        connect(self.author, second)
        response = self.client.post(
            reverse('swift-send', args=[self.swift.pk]),
            {'user_ids': [self.friend.pk, self.stranger.pk, second.pk]},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            {row['user_id'] for row in response.data['sent']},
            {self.friend.pk, second.pk},
        )
        self.assertEqual(
            [row['user_id'] for row in response.data['refused']], [self.stranger.pk])

    def test_the_same_person_twice_is_one_send(self):
        response = self.client.post(
            reverse('swift-send', args=[self.swift.pk]),
            {'user_ids': [self.friend.pk, self.friend.pk]},
            content_type='application/json',
        )
        self.assertEqual(len(response.data['sent']), 1)
        self.assertEqual(Message.objects.filter(swift=self.swift).count(), 1)

    def test_sending_a_swift_you_cannot_watch_is_a_404(self):
        hidden = make_swift(
            make_user('hidden_author'), visibility=SwiftVideo.Visibility.PRIVATE)
        response = self.client.post(
            reverse('swift-send', args=[hidden.pk]),
            {'user_ids': [self.friend.pk]},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 404)

    def test_empty_recipient_list_is_refused(self):
        response = self.client.post(
            reverse('swift-send', args=[self.swift.pk]),
            {'user_ids': []},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)

    def test_too_many_recipients_is_refused(self):
        from .views import SwiftSendView

        response = self.client.post(
            reverse('swift-send', args=[self.swift.pk]),
            {'user_ids': list(range(SwiftSendView.MAX_RECIPIENTS + 1))},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)


class RetagOnEditTests(SwiftTestCase):
    """A caption edit is half an edit to the Swift's tags, so it re-reads them.

    Without this, adding `#astana` to a published caption would show the tag in
    the text and attach it to nothing, and removing one would leave the Swift
    listed under a tag it no longer carries.
    """

    def setUp(self):
        super().setUp()
        self.author = make_user('editor')
        self.named = make_user('kamila')
        self.client = APIClient()
        self.client.force_authenticate(self.author)
        response = self.client.post(
            reverse('swift-create'), publish_payload(caption='first #alpha'))
        self.swift_id = response.data['id']
        self.url = reverse('swift-detail', args=[self.swift_id])

    def test_a_new_caption_tag_is_attached(self):
        response = self.client.patch(self.url, {'caption': 'now #alpha and #beta'},
                                     content_type='application/json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            [t['name'] for t in response.data['hashtags']], ['alpha', 'beta'])
        self.assertEqual(Hashtag.objects.get(name='beta').swift_count, 1)

    def test_a_removed_caption_tag_is_detached_and_gives_back_its_count(self):
        self.client.patch(self.url, {'caption': 'no tags now'},
                          content_type='application/json')
        self.assertEqual(
            SwiftHashtag.objects.filter(swift_id=self.swift_id).count(), 0)
        self.assertEqual(Hashtag.objects.get(name='alpha').swift_count, 0)

    def test_an_unchanged_tag_keeps_its_count_rather_than_churning(self):
        self.client.patch(self.url, {'caption': 'reworded, still #alpha'},
                          content_type='application/json')
        self.assertEqual(Hashtag.objects.get(name='alpha').swift_count, 1)

    def test_editing_something_else_leaves_tags_alone(self):
        response = self.client.patch(self.url, {'allow_comments': False},
                                     content_type='application/json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([t['name'] for t in response.data['hashtags']], ['alpha'])
        self.assertEqual(Hashtag.objects.get(name='alpha').swift_count, 1)

    def test_a_new_mention_is_resolved_and_notified(self):
        response = self.client.patch(self.url, {'caption': 'thanks @kamila'},
                                     content_type='application/json')
        self.assertEqual(
            [m['username'] for m in response.data['mentions']], ['kamila'])
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.named,
                notification_type=Notification.Type.SWIFT_MENTION,
            ).exists()
        )

    def test_a_removed_mention_loses_its_row_but_keeps_its_alert(self):
        self.client.patch(self.url, {'caption': 'thanks @kamila'},
                          content_type='application/json')
        self.client.patch(self.url, {'caption': 'never mind'},
                          content_type='application/json')

        self.assertFalse(SwiftMention.objects.filter(swift_id=self.swift_id).exists())
        # The alert already happened and cannot be unsent — the row is a claim
        # about what the Swift says now, the alert a fact about what it said.
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.named,
                notification_type=Notification.Type.SWIFT_MENTION,
            ).exists()
        )

    def test_re_adding_a_mention_does_not_notify_twice(self):
        for caption in ('hi @kamila', 'never mind', 'hi again @kamila'):
            self.client.patch(self.url, {'caption': caption},
                              content_type='application/json')
        self.assertEqual(
            Notification.objects.filter(
                recipient=self.named,
                notification_type=Notification.Type.SWIFT_MENTION,
            ).count(),
            1,
        )

    def test_the_tag_field_can_add_without_touching_the_caption(self):
        response = self.client.patch(
            self.url, {'hashtags': ['gamma']}, content_type='application/json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            sorted(t['name'] for t in response.data['hashtags']), ['alpha', 'gamma'])


class HashtagFeedTests(SwiftTestCase):
    """A tag's own page, which is what makes a hashtag more than decoration."""

    def setUp(self):
        super().setUp()
        self.author = make_user('tagged_author')
        self.viewer = make_user('tag_viewer')
        self.client = APIClient()
        self.client.force_authenticate(self.author)
        self.client.post(reverse('swift-create'), publish_payload(caption='one #almaty'))
        self.client.post(reverse('swift-create'), publish_payload(caption='two #almaty'))
        self.client.post(reverse('swift-create'), publish_payload(caption='three #astana'))

    def _feed(self, tag, client=None):
        return (client or self.client).get(reverse('swift-hashtag-feed', args=[tag]))

    def test_lists_every_swift_under_the_tag(self):
        response = self._feed('almaty')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['count'], 2)
        self.assertEqual(len(response.data['results']), 2)

    def test_carries_the_tag_as_it_was_written(self):
        self.client.post(reverse('swift-create'), publish_payload(caption='#KokTobe'))
        response = self._feed('koktobe')
        self.assertEqual(response.data['tag'], {'name': 'koktobe', 'display': 'KokTobe'})

    def test_matches_case_insensitively(self):
        self.assertEqual(self._feed('ALMATY').data['count'], 2)

    def test_newest_first(self):
        captions = [row['caption'] for row in self._feed('almaty').data['results']]
        self.assertEqual(captions, ['two #almaty', 'one #almaty'])

    def test_an_unknown_tag_is_a_404(self):
        self.assertEqual(self._feed('nothinghere').status_code, 404)

    def test_a_blocked_tag_is_a_404_rather_than_an_empty_list(self):
        Hashtag.objects.filter(name='almaty').update(is_blocked=True)
        self.assertEqual(self._feed('almaty').status_code, 404)

    def test_something_that_is_not_a_tag_is_a_404(self):
        self.assertEqual(self._feed('2024').status_code, 404)

    def test_the_audience_gate_is_the_feed_s_own(self):
        """A friends-only Swift is under the tag only for people who may watch it.

        And the page itself still answers 200 with nothing in it, rather than
        404. The tag genuinely exists — saying otherwise would be a different
        claim from "there is nothing here for you", and the honest one is the
        empty list. Nothing leaks either way: the count is the viewer's own,
        which `test_the_count_is_what_this_viewer_can_see` pins.
        """
        SwiftVideo.objects.filter(author=self.author).update(
            visibility=SwiftVideo.Visibility.FRIENDS)

        stranger = APIClient()
        stranger.force_authenticate(self.viewer)
        response = self._feed('almaty', stranger)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['count'], 0)
        self.assertEqual(response.data['results'], [])

    def test_the_count_is_what_this_viewer_can_see(self):
        """Not `Hashtag.swift_count`, which would leak how much is withheld."""
        # One of the two goes friends-only; the viewer is not a friend.
        hidden = SwiftVideo.objects.filter(author=self.author, caption='two #almaty')
        hidden.update(visibility=SwiftVideo.Visibility.FRIENDS)

        stranger = APIClient()
        stranger.force_authenticate(self.viewer)
        response = self._feed('almaty', stranger)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['count'], 1)
        # The denormalised counter still says two — which is exactly why the
        # response does not publish it.
        self.assertEqual(Hashtag.objects.get(name='almaty').swift_count, 2)

    def test_pages(self):
        response = self.client.get(
            reverse('swift-hashtag-feed', args=['almaty']), {'limit': 1})
        self.assertEqual(response.data['count'], 2)
        self.assertEqual(len(response.data['results']), 1)
