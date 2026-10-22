"""Create (or refresh) the App Review demo account and the people around it.

    python manage.py seed_review_demo --email review@example.com --password '…'
    python manage.py seed_review_demo --email … --password … --swifts-dir ./demo_swifts --zone-song-id 1440818839
    python manage.py seed_review_demo --remove

Safe to run again: everything is looked up before it is created, and the
reviewer's password is reset to the one given. Every account it creates is
`is_review_demo`, which keeps the whole demo out of real users' app — see
`users.review_demo`. `--remove` deletes demo accounts and demo clubs and nothing
else.

`--swifts-dir` takes video files (.mp4/.mov), shared round-robin between the
cast; a same-named .jpg/.png beside a video becomes its poster. Files go to the
configured storage, so on production they are uploaded to R2.
"""
import io
import uuid
from datetime import date, timedelta
from pathlib import Path

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from clubs.models import Club, ClubEvent, ClubMember, ClubMessage
from friends.models import FriendRequest, HiRequest, Message
from interests.models import Interest
from stories.models import Story
from swifts.models import SwiftVideo
from users import applemusic
from users.models import User
from users.review_demo import CAST, REVIEWER_USERNAME

#: Stable ids for the seeded Swifts, so a re-run finds the ones it made.
SWIFT_NAMESPACE = uuid.UUID('6f1d4c1e-9a52-4d8e-9c3b-0f2a7e5d1b44')

VIDEO_SUFFIXES = {'.mp4', '.mov', '.m4v'}
POSTER_SUFFIXES = ('.jpg', '.jpeg', '.png')

AVATAR_COLOURS = ('#E8714A', '#4A90E2', '#7B61FF', '#2BAE66', '#F5A623', '#D0021B', '#50E3C2', '#9013FE')

OWN_CLUB = 'Approx Demo Runners'
MEMBER_CLUB = 'Approx Demo Board Games'

DIRECT_MESSAGES = {
    'demo_amina': [
        ('them', 'Hey! Are you coming to the run on Saturday?'),
        ('me', 'Yes, I will be there 🙌'),
        ('them', 'Great, walk past my place on the way and you will hear my zone track 🎵'),
    ],
    'demo_daniyar': [
        ('them', 'Board games tonight?'),
        ('me', 'Count me in. Which one?'),
        ('them', 'Catan, then we will see.'),
    ],
}

SWIFT_CAPTIONS = (
    'Morning light ☀️', 'Weekend vibes', 'New spot in town', 'Run club recap 🏃', 'Sunset walk',
)


class Command(BaseCommand):
    help = 'Create or refresh the App Review demo account and its demo people.'

    def add_arguments(self, parser):
        parser.add_argument('--email', help="The reviewer's sign-in email.")
        parser.add_argument('--password', help="The reviewer's password.")
        parser.add_argument('--swifts-dir', help='Folder of demo Swift videos.')
        parser.add_argument('--swift-duration-ms', type=int, default=10_000,
                            help='Duration recorded for every seeded Swift (default 10000).')
        parser.add_argument('--zone-song-id', help="Apple Music song id for Amina's zone music.")
        parser.add_argument('--remove', action='store_true',
                            help='Delete every demo account and demo club, then stop.')

    def handle(self, *args, **options):
        if options['remove']:
            self._remove()
            return
        if not options['email'] or not options['password']:
            raise CommandError('--email and --password are required.')
        if len(options['password']) < 8:
            raise CommandError('Use a password of at least 8 characters.')

        videos = self._videos(options['swifts_dir'])
        song = self._song(options['zone_song_id'])

        with transaction.atomic():
            reviewer = self._reviewer(options['email'], options['password'])
            interests = self._interests()
            reviewer.interests.add(*interests)
            cast = {member.username: self._member(member, i, interests) for i, member in enumerate(CAST)}
            self._relationships(reviewer, cast)
            self._messages(reviewer, cast)
            self._clubs(reviewer, cast, interests)
            self._stories(cast)
            if song:
                self._zone_music(cast['demo_amina'], song)
            self._swifts(cast, videos, options['swift_duration_ms'])

        self.stdout.write(self.style.SUCCESS(
            f'Review demo ready: sign in as {reviewer.email}. The cast moves next to the '
            f'reviewer on its first location report.'
        ))

    # ── Accounts ──────────────────────────────────────────────────────────────
    def _reviewer(self, email, password):
        existing = User.objects.filter(email__iexact=email).first()
        if existing and not existing.is_review_demo:
            raise CommandError(f'{email} belongs to a real account; choose another email.')
        clash = User.objects.filter(username=REVIEWER_USERNAME, is_review_demo=False).exists()
        if clash:
            raise CommandError(f'The username {REVIEWER_USERNAME!r} belongs to a real account.')
        reviewer = existing or User.objects.filter(username=REVIEWER_USERNAME).first() or User(
            username=REVIEWER_USERNAME,
        )
        reviewer.email = email
        reviewer.first_name = 'App'
        reviewer.last_name = 'Review'
        reviewer.bio = 'Demo account for App Review.'
        reviewer.date_of_birth = date(1995, 5, 14)
        reviewer.nationality = reviewer.nationality or 'Kazakhstan'
        reviewer.profession = reviewer.profession or 'Designer'
        reviewer.is_review_demo = True
        reviewer.is_active = True
        reviewer.set_password(password)
        reviewer.save()
        self._avatar(reviewer, 0)
        return reviewer

    def _member(self, member, index, interests):
        user = User.objects.filter(username=member.username).first()
        if user and not user.is_review_demo:
            raise CommandError(f'The username {member.username!r} belongs to a real account.')
        if user is None:
            user = User(username=member.username, email=f'{member.username}@review-demo.invalid')
            user.set_unusable_password()
        user.first_name = member.first_name
        user.bio = member.bio
        user.date_of_birth = date(1996 + index % 5, 1 + index, 10 + index)
        user.is_review_demo = True
        # Nobody can search the cast up by name; the reviewer meets them through
        # the relationships below and the map.
        user.discoverable_by_username = False
        user.save()
        user.interests.add(*interests)
        self._avatar(user, index + 1)
        return user

    def _avatar(self, user, index):
        if user.profile_picture:
            return
        from PIL import Image, ImageDraw, ImageFont

        size = 512
        image = Image.new('RGB', (size, size), AVATAR_COLOURS[index % len(AVATAR_COLOURS)])
        draw = ImageDraw.Draw(image)
        letter = (user.first_name or user.username)[:1].upper()
        font = ImageFont.load_default(size=280)
        draw.text((size / 2, size / 2), letter, fill='white', font=font, anchor='mm')
        buffer = io.BytesIO()
        image.save(buffer, format='JPEG', quality=90)
        user.profile_picture.save(f'{user.username}.jpg', ContentFile(buffer.getvalue()), save=True)

    def _interests(self):
        wanted = ['Hiking', 'Running', 'Board Games', 'Photography']
        found = list(Interest.objects.filter(name__in=wanted))
        if len(found) < 3:
            found = list(Interest.objects.order_by('pk')[:4])
        if not found:
            raise CommandError('No interests exist yet; run seed_interests first.')
        return found

    # ── Who knows whom ────────────────────────────────────────────────────────
    def _relationships(self, reviewer, cast):
        for member in CAST:
            user = cast[member.username]
            if member.role == 'friend':
                self._pair(FriendRequest, user, reviewer, FriendRequest.Status.ACCEPTED)
            elif member.role == 'connected':
                self._pair(HiRequest, user, reviewer, HiRequest.Status.ACCEPTED)
            elif member.role == 'requester':
                self._pair(FriendRequest, user, reviewer, FriendRequest.Status.PENDING)
        # Mutual friends, and a Hi waiting for the reviewer to answer.
        self._pair(FriendRequest, cast['demo_amina'], cast['demo_daniyar'], FriendRequest.Status.ACCEPTED)
        self._pair(HiRequest, cast['demo_alina'], reviewer, HiRequest.Status.PENDING)

    @staticmethod
    def _pair(model, sender, receiver, status):
        if model.objects.filter(sender=receiver, receiver=sender).exists():
            return
        model.objects.update_or_create(sender=sender, receiver=receiver, defaults={'status': status})

    def _messages(self, reviewer, cast):
        for username, lines in DIRECT_MESSAGES.items():
            them = cast[username]
            if Message.objects.filter(sender__in=[them, reviewer], receiver__in=[them, reviewer]).exists():
                continue
            for who, text in lines:
                sender, receiver = (them, reviewer) if who == 'them' else (reviewer, them)
                Message.objects.create(sender=sender, receiver=receiver, content=text, is_read=who == 'me')

    # ── Clubs ─────────────────────────────────────────────────────────────────
    def _clubs(self, reviewer, cast, interests):
        # Left at the default scope (CITY, with no city recorded), which no
        # place is inside: nobody can discover these clubs. Members keep access
        # wherever they are, which is how the reviewer reaches both.
        own = self._club(
            OWN_CLUB, reviewer, interests[0],
            'Easy 5 km runs every Saturday morning. All paces welcome.',
            members=[cast['demo_amina'], cast['demo_daniyar'], cast['demo_timur']],
        )
        joined = self._club(
            MEMBER_CLUB, cast['demo_daniyar'], interests[-1],
            'Catan, Codenames and whatever someone brings.',
            members=[reviewer, cast['demo_amina'], cast['demo_sofia']],
        )
        if not own.messages.exists():
            for sender, text in (
                (cast['demo_amina'], 'Who is in for Saturday?'),
                (cast['demo_daniyar'], 'Me! Same place as last week?'),
                (reviewer, 'Yes, see you at 9.'),
            ):
                ClubMessage.objects.create(club=own, sender=sender, content=text)
        if not joined.messages.exists():
            ClubMessage.objects.create(club=joined, sender=cast['demo_daniyar'], content='Game night on Friday 🎲')
        if not own.events.exists():
            now = timezone.now()
            ClubEvent.objects.create(
                club=own, created_by=cast['demo_amina'], title='Saturday 5K',
                description='Meet at the park entrance.', location_name='City park',
                event_datetime=now + timedelta(days=2), expires_at=now + timedelta(days=2, hours=3),
                status=ClubEvent.Status.APPROVED, approved_by=reviewer, approved_at=now,
            )

    def _club(self, name, owner, interest, description, members):
        club = Club.objects.filter(name=name).first()
        if club and not (club.creator and club.creator.is_review_demo):
            raise CommandError(f'A real club is already called {name!r}.')
        if club is None:
            club = Club.objects.create(name=name, creator=owner, interest=interest, description=description)
        ClubMember.objects.update_or_create(user=owner, club=club, defaults={'role': ClubMember.Role.OWNER})
        for member in members:
            ClubMember.objects.get_or_create(user=member, club=club, defaults={'role': ClubMember.Role.MEMBER})
        return club

    # ── Content ───────────────────────────────────────────────────────────────
    def _stories(self, cast):
        now = timezone.now()
        for username, text in (('demo_amina', 'Found the best coffee in town ☕'),
                               ('demo_sofia', 'Golden hour 📸')):
            user = cast[username]
            if not user.stories.exists():
                Story.objects.create(user=user, content=text, expires_at=now + timedelta(hours=23))

    def _song(self, song_id):
        if not song_id:
            self.stdout.write(self.style.WARNING(
                'No --zone-song-id: Amina gets no zone music, so the zone music alert cannot be shown.'
            ))
            return None
        if not applemusic.is_configured():
            raise CommandError('Apple Music is not configured here; --zone-song-id needs it.')
        try:
            return applemusic.get_song(song_id)
        except applemusic.AppleMusicError as exc:
            raise CommandError(f'Apple Music refused song {song_id}: {exc}') from exc

    def _zone_music(self, owner, song):
        owner.zone_audio = None
        owner.zone_audio_moderation = 'ok'
        owner.zone_apple_id = song['id']
        owner.zone_apple_title = song['title'][:200]
        owner.zone_apple_artist = song['artist'][:200]
        owner.zone_apple_artwork = song['artwork'][:500]
        owner.zone_apple_preview_url = song['preview_url'][:500]
        owner.music_zone_active = True
        owner.save()

    def _videos(self, folder):
        if not folder:
            return []
        path = Path(folder)
        if not path.is_dir():
            raise CommandError(f'{folder} is not a folder.')
        videos = sorted(p for p in path.iterdir() if p.suffix.lower() in VIDEO_SUFFIXES)
        if not videos:
            raise CommandError(f'No .mp4/.mov files in {folder}.')
        return videos

    def _swifts(self, cast, videos, duration_ms):
        authors = [cast[m.username] for m in CAST if m.role in ('friend', 'connected', 'stranger')]
        for i, video in enumerate(videos):
            author = authors[i % len(authors)]
            key = uuid.uuid5(SWIFT_NAMESPACE, video.name)
            if SwiftVideo.objects.filter(author=author, idempotency_key=key).exists():
                continue
            swift = SwiftVideo(
                author=author, idempotency_key=key,
                caption=SWIFT_CAPTIONS[i % len(SWIFT_CAPTIONS)],
                duration_ms=duration_ms, width=1080, height=1920,
                status=SwiftVideo.Status.READY, moderation=SwiftVideo.Moderation.OK,
                visibility=SwiftVideo.Visibility.PUBLIC,
            )
            swift.video.save(f'demo_{video.name}', ContentFile(video.read_bytes()), save=False)
            poster = next((video.with_suffix(s) for s in POSTER_SUFFIXES if video.with_suffix(s).exists()), None)
            if poster:
                swift.thumbnail.save(f'demo_{poster.name}', ContentFile(poster.read_bytes()), save=False)
            swift.save()
            self.stdout.write(f'Swift {video.name} → {author.username}')

    # ── Removal ───────────────────────────────────────────────────────────────
    def _remove(self):
        with transaction.atomic():
            clubs = Club.objects.filter(name__in=[OWN_CLUB, MEMBER_CLUB], creator__is_review_demo=True)
            club_count = clubs.count()
            clubs.delete()
            users = User.objects.filter(is_review_demo=True)
            user_count = users.count()
            users.delete()
        self.stdout.write(self.style.SUCCESS(
            f'Removed {user_count} demo account(s) and {club_count} demo club(s).'
        ))
