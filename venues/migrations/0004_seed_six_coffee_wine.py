from django.db import migrations


def seed_six(apps, schema_editor):
    # User-confirmed branch: Abay Avenue 17, not Tulebayev 107.
    # Address: https://six-coffee-wine.kamiqr.com/menu/basic
    # Pin: https://yandex.com/maps/org/six_coffee_wine/12299495096/
    apps.get_model('venues', 'Venue').objects.using(schema_editor.connection.alias).get_or_create(
        slug='six-coffee-wine-abay',
        defaults={
            'name': 'Six Coffee+Wine',
            'address': 'Abay Avenue, 17, Almaty',
            'category': 'Restaurant & cafe',
            'description': 'Good coffee, good company. Gather your club for a catch-up on Abay and add a meeting point to your proposal.',
            'latitude': '43.2432600',
            'longitude': '76.9537860',
            'is_active': True,
        },
    )


class Migration(migrations.Migration):
    dependencies = [('venues', '0003_seed_esentai_mall')]
    operations = [migrations.RunPython(seed_six, migrations.RunPython.noop)]
