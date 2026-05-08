from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('friends', '0002_blockeduser'),
    ]

    operations = [
        migrations.AddField(
            model_name='message',
            name='deleted_for_sender',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='message',
            name='deleted_for_receiver',
            field=models.BooleanField(default=False),
        ),
    ]
