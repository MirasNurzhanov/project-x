from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('stories', '0006_alter_story_latitude_alter_story_longitude'),
    ]

    operations = [
        migrations.AddField(
            model_name='story',
            name='is_starred',
            field=models.BooleanField(default=False),
        ),
        migrations.DeleteModel(
            name='SavedStory',
        ),
    ]
