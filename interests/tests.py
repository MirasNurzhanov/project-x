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

from . import categories
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

    def test_every_heading_maps_to_a_category(self):
        """The headings in the file are the groups the app shows.

        A heading nobody has mapped does not break the deploy — its interests
        just arrive ungrouped — which is exactly why it needs catching here
        instead: a silent regrouping of 60 interests into "More" is not
        something anyone would notice from the release log.
        """
        unmapped = [h for h in self.catalog if not categories.slug_for(h)]
        self.assertEqual(unmapped, [])

    def test_the_category_table_covers_the_whole_file(self):
        self.assertEqual(
            sorted(categories.slug_for(h) for h in self.catalog),
            sorted(categories.KEYS),
        )


class CategoryTests(TestCase):
    """Grouping, which is the difference between a picker and a wall of 681
    chips."""

    def test_seeding_files_every_interest_under_its_heading(self):
        seed()
        self.assertEqual(Interest.objects.filter(category='').count(), 0)
        self.assertGreater(Interest.objects.filter(category='sports').count(), 100)
        self.assertGreater(Interest.objects.filter(category='music').count(), 50)

    def test_seeding_regroups_rows_that_predate_the_column(self):
        """The production case: 681 rows already there, all ungrouped.

        They came from this same file, so the grouping is not unknowable — it
        just had nowhere to live until the column existed.
        """
        seed()
        Interest.objects.update(category='')  # as they were before the column

        output = seed()

        self.assertEqual(Interest.objects.get(name='Football').category, 'sports')
        self.assertEqual(Interest.objects.get(name='Jazz').category, 'music')
        self.assertIn('regrouped', output)

    def test_interests_the_catalogue_forgot_are_still_grouped(self):
        """Three rows from the original 39-entry seed migration are not in the
        current JSON. People have them selected, so they need a heading — but
        they are not re-added as choices. See categories.LEGACY_GROUPS."""
        seed()
        self.assertEqual(
            Interest.objects.get(name='Gym & Weightlifting').category, 'fitness',
        )
        self.assertEqual(Interest.objects.get(name='Martial Arts').category, 'sports')
        self.assertEqual(Interest.objects.get(name='Film Photography').category, 'arts')

    def test_a_custom_interest_keeps_no_category(self):
        """Blank is a real answer. The old "Other" flow's entries belong to no
        group, and filing them under Sports would be a lie the picker repeats."""
        Interest.objects.create(name='Competitive Napping', icon='😴')
        seed()
        self.assertEqual(
            Interest.objects.get(name='Competitive Napping').category, '',
        )

    def test_seeding_twice_regroups_nothing_the_second_time(self):
        seed()
        self.assertIn('0 regrouped', seed())

    def test_an_unknown_heading_is_reported_not_raised(self):
        """It runs on the release line; failing here fails the deploy."""
        err = StringIO()
        with patch(
            'interests.management.commands.seed_interests.json.load',
            return_value={'Sports': [{'name': 'Football', 'icon': '⚽'}],
                          'Underwater Basket Weaving': [{'name': 'Weaving', 'icon': '🧺'}]},
        ):
            call_command('seed_interests', stdout=StringIO(), stderr=err)

        self.assertIn('Underwater Basket Weaving', err.getvalue())
        self.assertEqual(Interest.objects.get(name='Football').category, 'sports')
        self.assertEqual(Interest.objects.get(name='Weaving').category, '')


class InterestApiTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient

        seed()
        self.user = get_user_model().objects.create_user(
            username='u', email='u@example.com', password='pw',
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_the_list_arrives_already_grouped(self):
        """Not alphabetical across the whole catalogue.

        A screen that renders the response as one strip should still see Sports
        together and Music together — the grouping is not something each client
        should have to reconstruct.
        """
        rows = self.client.get('/api/interests/').data
        order = [r['category'] for r in rows]

        # Each category appears in exactly one contiguous run.
        runs = [key for index, key in enumerate(order) if index == 0 or order[index - 1] != key]
        self.assertEqual(len(runs), len(set(runs)))
        # …and those runs are in the table's order, not the alphabet's.
        self.assertEqual(runs, [k for k in categories.KEYS if k in set(order)])

    def test_each_interest_carries_its_heading(self):
        rows = self.client.get('/api/interests/').data
        football = next(r for r in rows if r['name'] == 'Football')
        self.assertEqual(football['category'], 'sports')
        self.assertEqual(football['category_label'], 'Sports')

    def test_expanded_interests_can_be_chosen_and_edited(self):
        expected = {
            'Flag Football': 'sports',
            'Reformer Pilates': 'fitness',
            'Dombra': 'music',
            'Tufting': 'arts',
            'Kazakh Food': 'food',
            'Nature Walks': 'travel',
            'Co-op Games': 'games',
            'Webtoons': 'entertainment',
            'Pet Adoption': 'lifestyle',
        }
        response = self.client.get('/api/interests/')
        self.assertEqual(response.status_code, 200)
        rows = {row['name']: row for row in response.data}
        for name, category in expected.items():
            self.assertEqual(rows[name]['category'], category)

        # Onboarding saves a batch of server-issued IDs.
        ids = [rows[name]['id'] for name in expected]
        response = self.client.post(
            '/api/interests/select/', {'interest_ids': ids}, format='json',
        )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(set(self.user.interests.values_list('id', flat=True)), set(ids))

        # The profile editor removes and adds individual interests.
        removed_id = ids[0]
        added_id = rows['Hot Pot']['id']
        self.assertEqual(
            self.client.delete(f'/api/interests/{removed_id}/select/').status_code, 204,
        )
        self.assertEqual(
            self.client.post(f'/api/interests/{added_id}/select/').status_code, 204,
        )
        self.assertEqual(
            set(self.user.interests.values_list('id', flat=True)),
            (set(ids) - {removed_id}) | {added_id},
        )

    def test_one_category_can_be_fetched_on_its_own(self):
        rows = self.client.get('/api/interests/?category=music').data
        self.assertGreater(len(rows), 50)
        self.assertEqual({r['category'] for r in rows}, {'music'})

    def test_an_unknown_category_returns_nothing_not_everything(self):
        rows = self.client.get('/api/interests/?category=quidditch').data
        self.assertEqual(list(rows), [])

    def test_the_categories_endpoint_lists_all_nine_in_order(self):
        rows = self.client.get('/api/interests/categories/').data
        self.assertEqual([r['key'] for r in rows], list(categories.KEYS))
        self.assertEqual(rows[0]['label'], 'Sports')
        self.assertGreater(rows[0]['count'], 100)
        self.assertEqual(sum(r['count'] for r in rows), Interest.objects.count())

    def test_ungrouped_interests_get_a_trailing_heading_only_when_they_exist(self):
        rows = self.client.get('/api/interests/categories/').data
        self.assertNotIn('More', [r['label'] for r in rows])

        custom = Interest.objects.create(name='Competitive Napping', icon='😴')
        self.user.interests.add(custom)

        rows = self.client.get('/api/interests/categories/').data
        self.assertEqual(rows[-1], {'key': '', 'label': 'More', 'count': 1})

    def test_ungrouped_interests_sort_last_in_the_list(self):
        custom = Interest.objects.create(name='Competitive Napping', icon='😴')
        self.user.interests.add(custom)
        rows = self.client.get('/api/interests/').data
        self.assertEqual(rows[-1]['name'], 'Competitive Napping')
        self.assertEqual(rows[-1]['category'], '')
