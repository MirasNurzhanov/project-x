"""Delete a club by name, safely.

    manage.py delete_club dubo                  # report only, changes nothing
    manage.py delete_club dubo --apply
    manage.py delete_club --id 42 --apply

Matches case-insensitively on `Club.name`, which is enforced unique — so a
name always names at most one club. `--id` is for the rare case a club's name
has already changed since whoever asked for this looked at it.

Report-first by default, like `scope_legacy_clubs`: this is destructive and
the blast radius is easy to underestimate from the name alone — deleting a
club cascades to its memberships, its messages, and its events (see
clubs.models, every FK back to Club is on_delete=CASCADE). The report exists
so that cascade is a number someone reads before it happens, not after.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from clubs.models import Club, ClubEvent, ClubMember, ClubMessage


class Command(BaseCommand):
    help = "Delete a club by name (or id), after reporting what it cascades to."

    def add_arguments(self, parser):
        parser.add_argument(
            'name', nargs='?', default=None,
            help='Club name, matched case-insensitively.',
        )
        parser.add_argument(
            '--id', type=int, default=None, dest='club_id',
            help='Match by id instead of name.',
        )
        parser.add_argument(
            '--apply', action='store_true',
            help='Delete it. Without this the command only reports.',
        )

    def handle(self, *args, **options):
        name = options['name']
        club_id = options['club_id']
        if not name and not club_id:
            raise CommandError('Give a club name or --id.')

        qs = Club.objects.select_related('creator')
        club = (
            qs.filter(pk=club_id).first() if club_id
            else qs.filter(name__iexact=name).first()
        )
        if club is None:
            raise CommandError(
                f'No club matches {"id " + str(club_id) if club_id else name!r}.'
            )

        member_count = ClubMember.objects.filter(club=club).count()
        message_count = ClubMessage.objects.filter(club=club).count()
        event_count = ClubEvent.objects.filter(club=club).count()

        self.stdout.write(f'Club #{club.pk}: {club.name!r}')
        self.stdout.write(f'  creator:  {club.creator or "(none)"}')
        self.stdout.write(f'  scope:    {club.visibility_level} - {club.visibility_place_name or "(unscoped)"}')
        self.stdout.write(f'  created:  {club.created_at:%Y-%m-%d}')
        self.stdout.write('')
        self.stdout.write('Deleting it cascades to:')
        self.stdout.write(f'  {member_count} membership(s)')
        self.stdout.write(f'  {message_count} message(s)')
        self.stdout.write(f'  {event_count} event(s)')
        self.stdout.write('')

        if not options['apply']:
            self.stdout.write(self.style.WARNING(
                'Nothing deleted. Re-run with --apply to actually delete this club.'
            ))
            return

        club_id, club_name = club.pk, club.name
        with transaction.atomic():
            club.delete()

        self.stdout.write(self.style.SUCCESS(f'Deleted club #{club_id} {club_name!r}'))
