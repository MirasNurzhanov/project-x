"""The interest seeder runs on every deploy, from the Procfile release step.

That placement is the reason these exist: a command that raises there does not
just skip the seed, it fails the release and the deploy never lands. So the
cases worth pinning are the boring ones — running twice, running against a
missing file, running when there is nothing to do.
"""
import json
import os
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from .models import Interest


def seed(**kwargs):
    out = StringIO()
    call_command('seed_interests', stdout=out, stderr=StringIO(), **kwargs)
    return out.getvalue()


class SeedInterestsTests(TestCase):
    def test_creates_the_catalogue(self):
        seed()
        self.assertGreater(Interest.objects.count(), 600)

    def test_every_seeded_interest_has_an_emoji(self):
        """A blank icon renders an empty chip, and the app has no second source
        for one now that the free-text catalogue is gone."""
        seed()
        self.assertEqual(
            list(Interest.objects.filter(icon='').values_list('name', flat=True)), [],
        )

    def test_running_twice_creates_nothing_the_second_time(self):
        seed()
        first = Interest.objects.count()
        output = seed()
        self.assertEqual(Interest.objects.count(), first)
        self.assertIn('0 created', output)

    def test_existing_names_are_matched_case_insensitively(self):
        """Otherwise a re-run would duplicate every interest whose casing in the
        database differs from the JSON."""
        Interest.objects.create(name='kAbAdDi', icon='X')
        seed()
        self.assertEqual(Interest.objects.filter(name__iexact='kabaddi').count(), 1)

    def test_nothing_is_ever_deleted(self):
        """People have already selected these. A name dropped from the JSON must
        not vanish from their profiles."""
        Interest.objects.create(name='Retired Hobby', icon='🕰️')
        seed()
        self.assertTrue(Interest.objects.filter(name='Retired Hobby').exists())

    def test_a_missing_seed_file_does_not_fail_the_release(self):
        with patch('os.path.exists', return_value=False):
            seed()  # must not raise

    def test_dry_run_writes_nothing(self):
        before = Interest.objects.count()
        output = seed(dry_run=True)
        self.assertIn('Would create', output)
        self.assertEqual(Interest.objects.count(), before)


class SeedFileTests(TestCase):
    """The JSON itself, checked here because the app cannot render a malformed
    entry and nothing else in the backend reads this file."""

    def setUp(self):
        from django.conf import settings
        path = os.path.join(settings.BASE_DIR, 'interests_seed.json')
        with open(path, encoding='utf-8') as f:
            self.catalog = json.load(f)
        self.rows = [i for items in self.catalog.values() for i in items]

    def test_every_row_has_a_name_and_an_icon(self):
        bad = [r for r in self.rows if not r.get('name') or not r.get('icon')]
        self.assertEqual(bad, [])

    def test_no_duplicate_names_across_categories(self):
        seen = {}
        for r in self.rows:
            key = r['name'].strip().lower()
            seen[key] = seen.get(key, 0) + 1
        self.assertEqual([n for n, c in seen.items() if c > 1], [])
