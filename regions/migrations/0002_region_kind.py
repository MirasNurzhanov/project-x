"""Size a region so "my city" and "my district" can tell each other apart.

Rows cached before this field existed default to 'other', which matches
neither lookup — they would sit in the cache being silently skipped, and every
request for that place would go upstream again forever. `addresstype` was never
stored, so the backfill uses admin_level, the one signal we did keep. Rows
without even that stay 'other' and are simply refetched once on demand.
"""
from django.db import migrations, models


def classify_existing(apps, schema_editor):
    Region = apps.get_model('regions', 'Region')
    # 6-8 is a whole settlement, 9+ a subdivision of one — the same split
    # regions.nominatim._classify falls back to.
    Region.objects.filter(admin_level__gte=6, admin_level__lte=8).update(kind='city')
    Region.objects.filter(admin_level__gte=9).update(kind='district')


class Migration(migrations.Migration):

    dependencies = [
        ('regions', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='region',
            name='kind',
            field=models.CharField(choices=[('city', 'City or town'), ('district', 'District within a city'), ('other', 'Other')], db_index=True, default='other', max_length=10),
        ),
        migrations.RunPython(classify_existing, migrations.RunPython.noop),
    ]
