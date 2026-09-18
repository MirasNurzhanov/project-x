from django.db import migrations


def seed_esentai(apps, schema_editor):
    # Address: https://www.esentai.com/about/
    # Pin cross-check: https://yandex.com/maps/org/esentai_mall/1144117450/
    # The official page's JSON-LD coordinates point elsewhere; use the mall
    # building at Al-Farabi 77/8, not those generic city coordinates.
    apps.get_model('venues', 'Venue').objects.using(schema_editor.connection.alias).get_or_create(
        slug='esentai-mall',
        defaults={
            'name': 'Esentai Mall',
            'address': 'Al-Farabi Avenue, 77/8, Almaty',
            'category': 'Shopping mall',
            'description': 'Make time for your people. Bring your club together at Esentai and choose a meeting point everyone can find.',
            'latitude': '43.2185930',
            'longitude': '76.9280830',
            'is_active': True,
        },
    )


class Migration(migrations.Migration):
    dependencies = [('venues', '0002_seed_dostyk_plaza')]
    operations = [migrations.RunPython(seed_esentai, migrations.RunPython.noop)]
