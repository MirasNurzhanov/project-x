from django.db import migrations


def seed_dostyk(apps, schema_editor):
    # Venue-owned address and pin, verified 2026-09-15:
    # https://dostykplaza.kz/en/contacts/ (contacts-map data-lat/data-lng).
    # No Google Places content, logo, partner claim or booking integration.
    apps.get_model('venues', 'Venue').objects.using(schema_editor.connection.alias).get_or_create(
        slug='dostyk-plaza',
        defaults={
            'name': 'Dostyk Plaza',
            'address': 'Samal-2, 111, Almaty',
            'category': 'Shopping mall',
            'description': 'Choose a time, pick your club, and meet here. Add a meeting point so everyone knows where to find you.',
            'latitude': '43.2331390',
            'longitude': '76.9565620',
            'is_active': True,
        },
    )


class Migration(migrations.Migration):
    dependencies = [('venues', '0001_initial')]
    # Do not remove a potentially edited venue or detach its meetups on rollback.
    operations = [migrations.RunPython(seed_dostyk, migrations.RunPython.noop)]
