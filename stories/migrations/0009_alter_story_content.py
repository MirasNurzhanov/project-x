from django.db import migrations, models


class Migration(migrations.Migration):
    """Make a story's caption optional.

    `blank` is validation-only and `default` is applied by Django rather than
    by the column, so this alters no data and rewrites no rows - every existing
    story already has a caption, because until now it could not have been
    posted without one.

    What stops this from allowing an empty post is
    `StoryCreateSerializer.validate`: a story still needs a caption or a
    picture, which is a rule about the pair and cannot be expressed on either
    column alone.
    """

    dependencies = [
        ('stories', '0008_alter_storyreaction_reaction'),
    ]

    operations = [
        migrations.AlterField(
            model_name='story',
            name='content',
            field=models.TextField(blank=True, default='', max_length=280),
        ),
    ]
