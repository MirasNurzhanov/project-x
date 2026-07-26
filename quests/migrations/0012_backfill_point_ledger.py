"""Seed the ledger with everyone's existing balance.

Before the ledger, total_points was a free-floating integer. Rather than
zeroing it (users would lose visible points) or trusting it forever, each
non-zero balance becomes one `legacy_balance` event with period_start=NULL:
it still counts toward the all-time total, but it can never appear in a
weekly leaderboard, which from now on only reflects approved photo proofs.
"""
from django.db import migrations


def backfill(apps, schema_editor):
    UserAuraPoints = apps.get_model('quests', 'UserAuraPoints')
    PointEvent = apps.get_model('quests', 'PointEvent')

    existing_user_ids = set(
        PointEvent.objects.filter(reason='legacy_balance').values_list('user_id', flat=True)
    )

    events = [
        PointEvent(
            user_id=row.user_id,
            quest=None,
            proof=None,
            points=row.total_points,
            reason='legacy_balance',
            period_start=None,
        )
        for row in UserAuraPoints.objects.exclude(total_points=0)
        if row.user_id not in existing_user_ids
    ]
    PointEvent.objects.bulk_create(events, batch_size=500)


def unbackfill(apps, schema_editor):
    PointEvent = apps.get_model('quests', 'PointEvent')
    PointEvent.objects.filter(reason='legacy_balance').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('quests', '0011_pointevent'),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
