from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class AddUnreadIndex(AddIndexConcurrently):
    """Build online in production; retain SQLite support for local tests."""

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor == 'postgresql':
            return super().database_forwards(app_label, schema_editor, from_state, to_state)
        return migrations.AddIndex.database_forwards(
            self, app_label, schema_editor, from_state, to_state,
        )

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor == 'postgresql':
            return super().database_backwards(app_label, schema_editor, from_state, to_state)
        return migrations.AddIndex.database_backwards(
            self, app_label, schema_editor, from_state, to_state,
        )


class Migration(migrations.Migration):
    atomic = False
    dependencies = [('notifications', '0007_alter_notification_notification_type')]
    operations = [
        AddUnreadIndex(
            model_name='notification',
            index=models.Index(
                fields=['recipient'], condition=models.Q(is_read=False),
                name='notification_unread_idx',
            ),
        ),
    ]
