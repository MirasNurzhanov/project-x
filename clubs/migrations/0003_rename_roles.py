from django.db import migrations, models


def rename_roles(apps, schema_editor):
    ClubMember = apps.get_model("clubs", "ClubMember")
    ClubMember.objects.filter(role="president").update(role="owner")
    ClubMember.objects.filter(role="vice").update(role="admin")


class Migration(migrations.Migration):
    dependencies = [
        ("clubs", "0002_alter_club_name"),
    ]

    operations = [
        migrations.RunPython(rename_roles, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="clubmember",
            name="role",
            field=models.CharField(
                choices=[("owner", "Owner"), ("admin", "Admin"), ("member", "Member")],
                default="member",
                max_length=10,
            ),
        ),
    ]
