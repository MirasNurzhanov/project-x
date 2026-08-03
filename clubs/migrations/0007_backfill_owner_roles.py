from django.db import migrations


def backfill_owner_roles(apps, schema_editor):
    """Set role='owner' for any club creator whose membership row was saved
    with the default 'member' role — happens when a deployment gap meant the
    creation endpoint ran before role=OWNER was being passed explicitly."""
    Club = apps.get_model("clubs", "Club")
    ClubMember = apps.get_model("clubs", "ClubMember")
    for club in Club.objects.filter(creator__isnull=False):
        ClubMember.objects.filter(
            club=club, user=club.creator, role="member"
        ).update(role="owner")


class Migration(migrations.Migration):
    dependencies = [
        ("clubs", "0006_clubevent_approved_at_clubevent_approved_by_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_owner_roles, migrations.RunPython.noop),
    ]
