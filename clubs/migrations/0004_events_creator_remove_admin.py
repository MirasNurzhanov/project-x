import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def demote_admins(apps, schema_editor):
    ClubMember = apps.get_model("clubs", "ClubMember")
    ClubMember.objects.filter(role="admin").update(role="member")


def set_creators(apps, schema_editor):
    Club = apps.get_model("clubs", "Club")
    ClubMember = apps.get_model("clubs", "ClubMember")
    for club in Club.objects.all():
        owner = ClubMember.objects.filter(club=club, role="owner").order_by("joined_at").first()
        if owner:
            club.creator_id = owner.user_id
            club.save(update_fields=["creator_id"])


class Migration(migrations.Migration):
    dependencies = [
        ("clubs", "0003_rename_roles"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # 1. Convert any leftover admin rows to member
        migrations.RunPython(demote_admins, migrations.RunPython.noop),

        # 2. Drop admin from role choices
        migrations.AlterField(
            model_name="clubmember",
            name="role",
            field=models.CharField(
                choices=[("owner", "Owner"), ("member", "Member")],
                default="member",
                max_length=10,
            ),
        ),

        # 3. Add creator FK to Club
        migrations.AddField(
            model_name="club",
            name="creator",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="created_clubs",
                to=settings.AUTH_USER_MODEL,
            ),
        ),

        # 4. Backfill creator from existing owner memberships
        migrations.RunPython(set_creators, migrations.RunPython.noop),

        # 5. Create ClubEvent
        migrations.CreateModel(
            name="ClubEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("title", models.CharField(max_length=200)),
                ("description", models.TextField(blank=True)),
                ("location_name", models.CharField(blank=True, max_length=200)),
                ("latitude", models.DecimalField(blank=True, decimal_places=8, max_digits=11, null=True)),
                ("longitude", models.DecimalField(blank=True, decimal_places=8, max_digits=11, null=True)),
                ("event_datetime", models.DateTimeField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "club",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="events",
                        to="clubs.club",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="club_events",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["event_datetime"]},
        ),
    ]
