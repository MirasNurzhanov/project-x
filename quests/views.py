from django.db.models import F, Q
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from friends.models import FriendRequest
from proximity.utils import haversine
from users.models import User
from .models import Quest, UserAuraPoints, UserQuest
from .serializers import QuestSerializer
from .utils import _is_reset_due

# Specific titles/descriptions for every seeded interest.
# Keys must match Interest.name exactly (case-sensitive).
_INTEREST_LABELS = {
    'Music':                 ('Jam with a music lover nearby',        'Meet a music-loving friend in person this week.'),
    'Guitar':                ('Strum with a guitarist nearby',        'Meet a guitarist friend in person this week.'),
    'DJing':                 ('Drop beats with a DJ nearby',          'Meet a DJ friend in person this week.'),
    'Singing':               ('Sing with a fellow vocalist nearby',   'Meet a singing friend in person this week.'),
    'Football':              ('Meet a football fan nearby',           'Meet a football-loving friend in person this week.'),
    'Basketball':            ('Shoot hoops together nearby',          'Meet a basketball friend in person this week.'),
    'Tennis':                ('Hit the court with a tennis fan',      'Meet a tennis friend in person this week.'),
    'Swimming':              ('Swim with a swimming buddy nearby',    'Meet a swimming friend in person this week.'),
    'Cycling':               ('Ride with a cycling friend nearby',    'Meet a cycling friend in person this week.'),
    'Skateboarding':         ('Skate with a fellow skater nearby',    'Meet a skateboarding friend in person this week.'),
    'Gaming':                ('Game with a gaming friend nearby',     'Meet a gaming friend in person this week.'),
    'Esports':               ('Watch esports with a fan nearby',      'Meet an esports fan in person this week.'),
    'Board Games':           ('Play board games together nearby',     'Meet a board-games friend in person this week.'),
    'Drawing':               ('Draw with a fellow artist nearby',     'Meet a drawing friend in person this week.'),
    'Painting':              ('Paint with a fellow artist nearby',    'Meet a painting friend in person this week.'),
    'Sculpting':             ('Sculpt with a friend nearby',          'Meet a sculpting friend in person this week.'),
    'Street Art':            ('Create street art together nearby',    'Meet a street-art friend in person this week.'),
    'Coding':                ('Hack with a fellow coder nearby',      'Meet a coding friend in person this week.'),
    'AI & Machine Learning': ('Geek out over AI together nearby',     'Meet an AI-enthusiast friend in person this week.'),
    'Robotics':              ('Build robots with a friend nearby',    'Meet a robotics friend in person this week.'),
    '3D Printing':           ('3D print something together nearby',   'Meet a 3D-printing friend in person this week.'),
    'Travel':                ('Explore with a travel buddy nearby',   'Meet a travel-loving friend in person this week.'),
    'Hiking':                ('Hit the trail with a hiker nearby',    'Meet a hiking friend in person this week.'),
    'Backpacking':           ('Backpack with an adventurer nearby',   'Meet a backpacking friend in person this week.'),
    'Road Trips':            ('Plan a road trip with a friend',       'Meet a road-trip friend in person this week.'),
    'Cooking':               ('Cook a meal with a foodie nearby',     'Meet a cooking friend in person this week.'),
    'Baking':                ('Bake something with a friend nearby',  'Meet a baking friend in person this week.'),
    'Coffee':                ('Grab a coffee with a coffee fan',      'Meet a coffee-loving friend in person this week.'),
    'Street Food':           ('Explore street food together nearby',  'Meet a street-food friend in person this week.'),
    'Gym & Weightlifting':   ('Train with a gym buddy nearby',        'Meet a gym friend in person this week.'),
    'Yoga':                  ('Flow through yoga together nearby',    'Meet a yoga friend in person this week.'),
    'Running':               ('Run with a running buddy nearby',      'Meet a running friend in person this week.'),
    'Martial Arts':          ('Train martial arts together nearby',   'Meet a martial-arts friend in person this week.'),
    'Photography':           ('Shoot photos with a photographer',     'Meet a photography friend in person this week.'),
    'Videography':           ('Film something together nearby',       'Meet a videography friend in person this week.'),
    'Film Photography':      ('Shoot film together nearby',           'Meet a film-photography friend in person this week.'),
    'Movies':                ('Watch a movie together nearby',        'Meet a movie-loving friend in person this week.'),
    'Anime':                 ('Watch anime together nearby',          'Meet an anime fan in person this week.'),
    'Documentaries':         ('Watch a documentary together nearby',  'Meet a documentary fan in person this week.'),
}


def _quest_labels_for(interest_name):
    if interest_name in _INTEREST_LABELS:
        return _INTEREST_LABELS[interest_name]
    return (
        f'Meet a {interest_name} fan nearby',
        f'Meet a friend who loves {interest_name} in person this week.',
    )


class QuestListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user

        # Regular quests (not interest-linked)
        regular_quests = list(Quest.objects.filter(interest__isnull=True))

        # One quest per user interest, created lazily on first access
        interest_quests = []
        for interest in user.interests.all():
            title, description = _quest_labels_for(interest.name)
            quest, _ = Quest.objects.get_or_create(
                condition_type=Quest.ConditionType.MEET_FRIEND_IN_PERSON,
                interest=interest,
                defaults={
                    'title': title,
                    'description': description,
                    'reward_points': 150,
                    'quest_type': Quest.QuestType.WEEKLY,
                },
            )
            interest_quests.append(quest)

        all_quests = regular_quests + interest_quests
        user_quests = {
            uq.quest_id: uq
            for uq in UserQuest.objects.filter(user=user, quest__in=all_quests)
        }
        aura = UserAuraPoints.objects.filter(user=user).first()
        serializer = QuestSerializer(all_quests, many=True, context={'user_quests': user_quests})
        return Response({
            'total_points': aura.total_points if aura else 0,
            'quests': serializer.data,
        })


class CompleteQuestView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        try:
            quest = Quest.objects.get(pk=pk)
        except Quest.DoesNotExist:
            return Response({'detail': 'Quest not found.'}, status=status.HTTP_404_NOT_FOUND)

        user_quest, _ = UserQuest.objects.get_or_create(user=request.user, quest=quest)

        if user_quest.completed and not _is_reset_due(user_quest):
            return Response(
                {'detail': 'Quest already completed for this period.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        now = timezone.now()
        user_quest.completed = True
        user_quest.completed_at = now
        user_quest.reset_date = timezone.localdate()
        user_quest.save(update_fields=['completed', 'completed_at', 'reset_date'])

        aura, _ = UserAuraPoints.objects.get_or_create(user=request.user)
        UserAuraPoints.objects.filter(pk=aura.pk).update(total_points=F('total_points') + quest.reward_points)
        aura.refresh_from_db()

        return Response({
            'detail': 'Quest completed.',
            'reward_points': quest.reward_points,
            'total_points': aura.total_points,
        }, status=status.HTTP_200_OK)


MEETUP_RADIUS_KM = 0.1  # 100 m


def _complete_quest_for_user(user, quest, now):
    user_quest, _ = UserQuest.objects.get_or_create(user=user, quest=quest)
    if user_quest.completed and not _is_reset_due(user_quest):
        return False
    user_quest.completed = True
    user_quest.completed_at = now
    user_quest.reset_date = timezone.localdate()
    user_quest.save(update_fields=['completed', 'completed_at', 'reset_date'])
    aura, _ = UserAuraPoints.objects.get_or_create(user=user)
    UserAuraPoints.objects.filter(pk=aura.pk).update(total_points=F('total_points') + quest.reward_points)
    return True


class VerifyMeetupView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        try:
            quest = Quest.objects.get(pk=pk)
        except Quest.DoesNotExist:
            return Response({'detail': 'Quest not found.'}, status=status.HTTP_404_NOT_FOUND)

        if quest.condition_type != Quest.ConditionType.MEET_FRIEND_IN_PERSON:
            return Response(
                {'detail': 'This quest does not support meetup verification.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        friend_id = request.data.get('friend_id')
        if not friend_id:
            return Response({'detail': 'friend_id is required.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            friend = User.objects.get(pk=friend_id)
        except User.DoesNotExist:
            return Response({'detail': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)

        is_friends = FriendRequest.objects.filter(
            status=FriendRequest.Status.ACCEPTED,
        ).filter(
            Q(sender=request.user, receiver=friend) | Q(sender=friend, receiver=request.user)
        ).exists()
        if not is_friends:
            return Response(
                {'detail': 'You are not friends with this user.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        user = request.user
        if None in (user.latitude, user.longitude, friend.latitude, friend.longitude):
            return Response(
                {'detail': 'Both users must have their location set.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        dist_km = haversine(
            float(user.latitude), float(user.longitude),
            float(friend.latitude), float(friend.longitude),
        )
        if dist_km > MEETUP_RADIUS_KM:
            return Response(
                {'detail': f'Too far apart ({round(dist_km * 1000)}m). Must be within 100m.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        now = timezone.now()
        completed_for_requester = _complete_quest_for_user(user, quest, now)
        completed_for_friend = _complete_quest_for_user(friend, quest, now)

        return Response({
            'detail': 'Meetup verified.',
            'quest': quest.title,
            'reward_points': quest.reward_points,
            'distance_m': round(dist_km * 1000),
            'completed_for_you': completed_for_requester,
            'completed_for_friend': completed_for_friend,
        }, status=status.HTTP_200_OK)
