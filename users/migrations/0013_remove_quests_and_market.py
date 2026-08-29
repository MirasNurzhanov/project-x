"""Tear down Approx points, quests and the market scaffold.

The `quests` and `market` apps are gone from INSTALLED_APPS, so their own
migrations can never run again — nothing would ever drop their tables. This
migration does it from `users`, which is where the last surviving column
(`show_on_leaderboard`) lived anyway.

Everything here is defensive about what actually exists: a fresh database
(tests, a new environment) never created these tables, and re-running against a
partially cleaned database must not fail. Irreversible on purpose — the point
ledger is being deleted, and an empty table is not a rollback.
"""
from django.db import migrations

# Children first: without CASCADE, a parent with live foreign keys pointing at
# it cannot be dropped.
QUEST_TABLES = [
    'quests_pointevent',
    'quests_questproof',
    'quests_userquest',
    'quests_useraurapoints',
    'quests_attestationchallenge',
    'quests_attesteddevice',
    'quests_quest',
]

MODERATOR_GROUP = 'Moderator'


def drop_quest_tables(apps, schema_editor):
    connection = schema_editor.connection
    existing = set(connection.introspection.table_names())
    # CASCADE is Postgres-only syntax; SQLite would reject the statement even
    # when the drop itself is a no-op, so branch on the vendor rather than the
    # environment.
    suffix = ' CASCADE' if connection.vendor == 'postgresql' else ''
    for table in QUEST_TABLES:
        if table in existing:
            schema_editor.execute(
                f'DROP TABLE {schema_editor.quote_name(table)}{suffix}'
            )


def drop_quest_metadata(apps, schema_editor):
    """Django's own bookkeeping outlives a deleted app: the content types (and
    the permissions that cascade off them), the moderation group the quests
    migrations created, and the applied-migration rows that would collide with
    any future app reusing these labels."""
    ContentType = apps.get_model('contenttypes', 'ContentType')
    ContentType.objects.filter(app_label__in=('quests', 'market')).delete()

    Group = apps.get_model('auth', 'Group')
    Group.objects.filter(name=MODERATOR_GROUP).delete()

    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM django_migrations WHERE app IN ('quests', 'market')"
        )


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0012_user_last_seen_user_motion_class_and_more'),
        ('contenttypes', '0002_remove_content_type_name'),
        ('auth', '0012_alter_user_first_name_max_length'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='user',
            name='show_on_leaderboard',
        ),
        migrations.RunPython(drop_quest_tables, migrations.RunPython.noop),
        migrations.RunPython(drop_quest_metadata, migrations.RunPython.noop),
    ]
