from django.db import migrations

MEETUP_QUESTS = [
    {
        'title': 'Meet a fitness buddy',
        'description': 'Meet a friend with fitness or gym interests in person this week.',
        'reward_points': 100,
        'quest_type': 'weekly',
        'condition_type': 'meet_friend_in_person',
    },
    {
        'title': 'Coffee date quest',
        'description': 'Catch up with a coffee-loving friend face to face this week.',
        'reward_points': 100,
        'quest_type': 'weekly',
        'condition_type': 'meet_friend_in_person',
    },
    {
        'title': 'Gaming session',
        'description': 'Get together with a gaming friend for an in-person session this week.',
        'reward_points': 100,
        'quest_type': 'weekly',
        'condition_type': 'meet_friend_in_person',
    },
    {
        'title': 'Sports meetup',
        'description': 'Meet up with a sports-loving friend in the real world this week.',
        'reward_points': 100,
        'quest_type': 'weekly',
        'condition_type': 'meet_friend_in_person',
    },
    {
        'title': 'Creative hangout',
        'description': 'Spend time in person with a friend who shares your love of art or music.',
        'reward_points': 100,
        'quest_type': 'weekly',
        'condition_type': 'meet_friend_in_person',
    },
]


def create_meetup_quests(apps, schema_editor):
    Quest = apps.get_model('quests', 'Quest')
    Quest.objects.bulk_create([Quest(**q) for q in MEETUP_QUESTS])


def delete_meetup_quests(apps, schema_editor):
    Quest = apps.get_model('quests', 'Quest')
    Quest.objects.filter(title__in=[q['title'] for q in MEETUP_QUESTS]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('quests', '0003_quest_meet_friend_in_person'),
    ]

    operations = [
        migrations.RunPython(create_meetup_quests, delete_meetup_quests),
    ]
