"""Give the pre-existing clubs a city, so `global` stops being permanent.

    manage.py scope_legacy_clubs              # report only, changes nothing
    manage.py scope_legacy_clubs --apply
    manage.py scope_legacy_clubs --apply --club 12 --club 15

Clubs created before location visibility carry `visibility_level='global'` and
no place at all, because they never had one to record — see migration 0009.
That is safe but permanent, and a growing set of unscoped clubs is exactly the
hole the feature was meant to close.

This closes it where it can be closed honestly: a legacy club is scoped to
**its creator's city**, at city level — the widest of the three, so nobody who
can see the club today and is still in that city loses it. Nothing narrower is
inferred. A district is a claim about where a club meets, and the creator's
current position a year later is not evidence for it.

Three things it will not do, each of which would be a guess:

  * scope a club whose creator has left, or was never placed;
  * scope a club whose creator is now in a different city from most of its
    members — it reports those and skips them, because the creator moving is
    not the club moving;
  * touch a club that already has a place.

Deliberately a command rather than a data migration. It needs `UserPlace` rows,
which only exist once people have used the app since the deploy, and it makes a
product decision per club that somebody should be able to read the report of
before it happens.
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from clubs.models import Club, ClubMember
from clubs.visibility import scope_fields
from regions.models import UserPlace


class Command(BaseCommand):
    help = "Scope legacy 'global' clubs to their creator's city."

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='Write the changes. Without this the command only reports.',
        )
        parser.add_argument(
            '--club', type=int, action='append', dest='clubs', default=[],
            help='Limit to these club ids. Repeatable.',
        )
        parser.add_argument(
            '--majority', type=float, default=0.5,
            help=(
                'Fraction of placed members who must share the creator’s city '
                'for the club to be scoped. 0 disables the check.'
            ),
        )

    def handle(self, *args, **options):
        apply_changes = options['apply']
        threshold = options['majority']

        clubs = Club.objects.filter(
            visibility_level=Club.Visibility.GLOBAL,
        ).select_related('creator')
        if options['clubs']:
            clubs = clubs.filter(pk__in=options['clubs'])

        scoped = skipped = 0
        for club in clubs.order_by('pk'):
            place = self._creator_place(club)
            if place is None:
                skipped += 1
                self.stdout.write(
                    f'  skip  #{club.pk} {club.name!r}: '
                    f'creator has no resolved location'
                )
                continue

            agreement, placed = self._member_agreement(club, place.city_id)
            if threshold and placed and agreement < threshold:
                skipped += 1
                self.stdout.write(self.style.WARNING(
                    f'  skip  #{club.pk} {club.name!r}: only '
                    f'{agreement:.0%} of {placed} placed members are in '
                    f'{place.city_name} — the creator may have moved'
                ))
                continue

            scoped += 1
            self.stdout.write(self.style.SUCCESS(
                f'  scope #{club.pk} {club.name!r} → {place.city_name} (city)'
            ))
            if apply_changes:
                with transaction.atomic():
                    for field, value in scope_fields(Club.Visibility.CITY, place).items():
                        setattr(club, field, value)
                    club.save(update_fields=list(
                        scope_fields(Club.Visibility.CITY, place).keys()
                    ))

        verb = 'Scoped' if apply_changes else 'Would scope'
        self.stdout.write('')
        self.stdout.write(f'{verb} {scoped} club(s); skipped {skipped}.')
        if scoped and not apply_changes:
            self.stdout.write('Re-run with --apply to write these changes.')

    # ── helpers ───────────────────────────────────────────────────────────────
    def _creator_place(self, club):
        """The creator's place, if they still exist and we can place them."""
        if club.creator_id is None:
            return None
        place = UserPlace.objects.filter(
            user_id=club.creator_id,
        ).select_related('city', 'region', 'district').first()
        if place is None or not place.resolved or place.city_id is None:
            return None
        return place

    def _member_agreement(self, club, city_id):
        """What share of the club's other placed members are in `city_id`.

        The one check that can catch a creator who has emigrated, which is why
        the creator's own place is excluded: this is asking whether the club
        agrees with them, and a vote they cast themselves cannot answer that.
        In a two-person club it would decide it.

        Members without a resolved place are not counted either way — they are
        silent, not dissenting.
        """
        member_ids = ClubMember.objects.filter(club=club).exclude(
            user_id=club.creator_id,
        ).values_list('user_id', flat=True)
        cities = Counter(
            UserPlace.objects.filter(
                user_id__in=list(member_ids), city_id__isnull=False,
            ).values_list('city_id', flat=True)
        )
        placed = sum(cities.values())
        if not placed:
            return 1.0, 0
        return cities.get(city_id, 0) / placed, placed
