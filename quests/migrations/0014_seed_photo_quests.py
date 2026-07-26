"""Retire the unverifiable quests and seed the photo-proof catalogue.

Retired: the event-driven quests (send_hi, send_message, update_location,
make_friend, have_encounter, select_interests) and the GPS meetup quests.
None of them required evidence a moderator could check, which is exactly what
the photo-proof system replaces.

Deleting a Quest cascades to its UserQuest rows but only NULLs
PointEvent.quest, so the ledger — and every point balance derived from it —
survives intact and stays auditable.
"""
from django.db import migrations

from quests.sync import sync_quests

RETIRED_CONDITION_TYPES = [
    'send_hi',
    'send_message',
    'update_location',
    'make_friend',
    'have_encounter',
    'select_interests',
    'meet_friend_in_person',
]


def seed(apps, schema_editor):
    Quest = apps.get_model('quests', 'Quest')
    Interest = apps.get_model('interests', 'Interest')

    Quest.objects.filter(condition_type__in=RETIRED_CONDITION_TYPES).delete()
    sync_quests(Quest, Interest)


def unseed(apps, schema_editor):
    # Only the generated rows are removed; the retired quests are not
    # recreated, since their conditions no longer exist in the app.
    Quest = apps.get_model('quests', 'Quest')
    Quest.objects.exclude(template_key='').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('quests', '0013_quest_template_key_quest_template_params_and_more'),
        ('interests', '0004_interest_needs_review'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
