from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('interests', '0003_seed_interests'),
    ]

    operations = [
        migrations.AddField(
            model_name='interest',
            name='needs_review',
            field=models.BooleanField(default=False),
        ),
    ]
