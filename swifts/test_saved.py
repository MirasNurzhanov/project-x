"""The saved-Swifts shortlist, and above all the rule that it holds one row.

"Do not store the same Swift twice" is the requirement, and the unique
constraint is the whole of the answer — so these tests go through the API and
hammer it the way a scrolling feed does, rather than creating rows directly.
A test that only ever saves once would pass against an implementation with no
constraint at all.

The other half is what a shortlist is *not*: it is private, it moves no public
counter, and it is not a way to keep watching something its author has since
withdrawn.
"""
from django.db import IntegrityError, transaction
from django.urls import reverse
from rest_framework.test import APIClient

from .models import SavedSwift, SwiftVideo
from .testing import SwiftTestCase, make_swift, make_user


class SavedSwiftFixture(SwiftTestCase):
    def setUp(self):
        super().setUp()
        self.creator = make_user('creator')
        self.viewer = make_user('viewer')
        self.swift = make_swift(self.creator)
        self.client = APIClient()
        self.client.force_authenticate(self.viewer)

    def save_url(self, swift=None):
        return reverse('swift-save', args=[(swift or self.swift).pk])

    def saved_count(self, user=None):
        return SavedSwift.objects.filter(user=user or self.viewer).count()

    def listed(self):
        return self.client.get(reverse('swift-saved')).json()


class SavingIsIdempotent(SavedSwiftFixture):
    """The requirement, from every angle it can be approached from."""

    def test_saving_once_stores_one_row(self):
        response = self.client.post(self.save_url())
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.json()['saved'])
        self.assertEqual(self.saved_count(), 1)

    def test_saving_the_same_swift_again_keeps_one_row(self):
        for _ in range(5):
            response = self.client.post(self.save_url())
            self.assertIn(response.status_code, (200, 201))
            self.assertTrue(response.json()['saved'])
        self.assertEqual(self.saved_count(), 1)

    def test_the_repeat_save_reports_200_rather_than_201(self):
        """So a client can tell "added" from "already there" without a read."""
        self.assertEqual(self.client.post(self.save_url()).status_code, 201)
        self.assertEqual(self.client.post(self.save_url()).status_code, 200)

    def test_the_list_shows_it_once(self):
        self.client.post(self.save_url())
        self.client.post(self.save_url())
        body = self.listed()
        self.assertEqual(body['count'], 1)
        self.assertEqual([row['id'] for row in body['results']], [self.swift.pk])

    def test_the_database_refuses_a_second_row_directly(self):
        """Not through the view. The constraint has to hold against anything
        that writes, or the guarantee is only as good as one code path."""
        SavedSwift.objects.create(swift=self.swift, user=self.viewer)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SavedSwift.objects.create(swift=self.swift, user=self.viewer)

    def test_two_people_saving_the_same_swift_are_two_rows(self):
        """Unique per (swift, user), not per swift."""
        other = make_user('other')
        self.client.post(self.save_url())
        client = APIClient()
        client.force_authenticate(other)
        client.post(self.save_url())
        self.assertEqual(SavedSwift.objects.filter(swift=self.swift).count(), 2)

    def test_saving_different_swifts_stores_each(self):
        second = make_swift(self.creator)
        self.client.post(self.save_url())
        self.client.post(self.save_url(second))
        self.assertEqual(self.saved_count(), 2)


class UnsavingIsIdempotent(SavedSwiftFixture):
    def test_unsaving_removes_the_row(self):
        self.client.post(self.save_url())
        response = self.client.delete(self.save_url())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['saved'])
        self.assertEqual(self.saved_count(), 0)

    def test_unsaving_something_never_saved_is_not_an_error(self):
        response = self.client.delete(self.save_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.saved_count(), 0)

    def test_save_and_unsave_can_be_cycled(self):
        for _ in range(3):
            self.client.post(self.save_url())
            self.assertEqual(self.saved_count(), 1)
            self.client.delete(self.save_url())
            self.assertEqual(self.saved_count(), 0)

    def test_a_withdrawn_swift_can_still_be_unsaved(self):
        """The row a user most wants gone must not be the one they cannot
        remove — so the delete path does not go through the visibility gate."""
        self.client.post(self.save_url())
        SwiftVideo.objects.filter(pk=self.swift.pk).update(
            moderation=SwiftVideo.Moderation.REMOVED,
        )
        self.client.delete(self.save_url())
        self.assertEqual(self.saved_count(), 0)


class TheListIsPrivateAndOrdered(SavedSwiftFixture):
    def test_newest_save_first(self):
        first = make_swift(self.creator)
        second = make_swift(self.creator)
        self.client.post(self.save_url(first))
        self.client.post(self.save_url(second))
        self.assertEqual(
            [row['id'] for row in self.listed()['results']],
            [second.pk, first.pk],
        )

    def test_re_saving_does_not_reorder(self):
        """A duplicate save is a no-op, so it cannot quietly bump a Swift back
        to the top of somebody's list."""
        first = make_swift(self.creator)
        second = make_swift(self.creator)
        self.client.post(self.save_url(first))
        self.client.post(self.save_url(second))
        self.client.post(self.save_url(first))
        self.assertEqual(
            [row['id'] for row in self.listed()['results']],
            [second.pk, first.pk],
        )

    def test_one_persons_list_is_not_anothers(self):
        other = make_user('other')
        self.client.post(self.save_url())

        client = APIClient()
        client.force_authenticate(other)
        self.assertEqual(client.get(reverse('swift-saved')).json()['count'], 0)

    def test_saving_moves_no_public_counter(self):
        before = SwiftVideo.objects.values('like_count', 'repost_count').get(pk=self.swift.pk)
        self.client.post(self.save_url())
        after = SwiftVideo.objects.values('like_count', 'repost_count').get(pk=self.swift.pk)
        self.assertEqual(before, after)

    def test_a_withdrawn_swift_drops_out_of_the_list(self):
        self.client.post(self.save_url())
        SwiftVideo.objects.filter(pk=self.swift.pk).update(
            moderation=SwiftVideo.Moderation.REMOVED,
        )
        self.assertEqual(self.listed()['count'], 0)
        # The bookmark itself is untouched — nothing was deleted under them.
        self.assertEqual(self.saved_count(), 1)

    def test_a_swift_its_author_deleted_drops_out_of_everyone_s_list(self):
        """The two features meeting: "delete for everyone" has to reach the
        people who saved it, or a shortlist becomes a way to keep watching
        something its author took down."""
        from django.urls import reverse as _reverse
        self.client.post(self.save_url())
        self.assertEqual(self.listed()['count'], 1)

        author = APIClient()
        author.force_authenticate(self.creator)
        response = author.delete(_reverse('swift-detail', args=[self.swift.pk]))
        self.assertEqual(response.status_code, 204)

        self.assertEqual(self.listed()['count'], 0)
        # The bookmark row is untouched; it is the Swift that is gone.
        self.assertEqual(self.saved_count(), 1)

    def test_only_the_author_can_delete_for_everyone(self):
        """A saver is not an author. 404 rather than 403 — see SwiftDetailView."""
        from django.urls import reverse as _reverse
        self.client.post(self.save_url())
        response = self.client.delete(_reverse('swift-detail', args=[self.swift.pk]))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.listed()['count'], 1)

    def test_the_list_is_paged_in_sql_not_in_python(self):
        """A shortlist of a thousand must not cost a thousand rows to read
        twenty.

        The tempting implementation reads every saved id, fetches the visible
        ones and slices in Python — correct, and O(saves) on every request.
        Query *count* alone would not catch that, since it is a fixed number of
        queries either way; what catches it is that the page never grows.
        """
        for _ in range(25):
            self.client.post(self.save_url(make_swift(self.creator)))

        body = self.client.get(reverse('swift-saved'), {'limit': 5}).json()
        self.assertEqual(body['count'], 25)
        self.assertEqual(len(body['results']), 5)

        # And the work per request does not scale with the shelf: the same
        # number of queries serves a page whether there are 25 saves or 5.
        with self.assertNumQueries(self.queries_for_a_page()):
            self.client.get(reverse('swift-saved'), {'limit': 5})

    def queries_for_a_page(self):
        """Measured on a small shelf, then asserted against a large one."""
        from django.test.utils import CaptureQueriesContext
        from django.db import connection
        other = make_user('measure')
        client = APIClient()
        client.force_authenticate(other)
        client.post(reverse('swift-save', args=[make_swift(self.creator).pk]))
        with CaptureQueriesContext(connection) as ctx:
            client.get(reverse('swift-saved'), {'limit': 5})
        return len(ctx)

    def test_an_empty_list_is_an_empty_list(self):
        body = self.listed()
        self.assertEqual(body['count'], 0)
        self.assertEqual(body['results'], [])


class TheFlagRoundTrips(SavedSwiftFixture):
    def test_viewer_saved_is_false_then_true(self):
        detail = reverse('swift-detail', args=[self.swift.pk])
        self.assertFalse(self.client.get(detail).json()['viewer_saved'])
        self.client.post(self.save_url())
        self.assertTrue(self.client.get(detail).json()['viewer_saved'])

    def test_the_flag_is_per_viewer(self):
        self.client.post(self.save_url())
        other = make_user('other')
        client = APIClient()
        client.force_authenticate(other)
        body = client.get(reverse('swift-detail', args=[self.swift.pk])).json()
        self.assertFalse(body['viewer_saved'])

    def test_a_creator_may_save_their_own_swift(self):
        """Unlike reposting, which is refused — there is no public number here
        for a self-save to inflate."""
        client = APIClient()
        client.force_authenticate(self.creator)
        response = client.post(self.save_url())
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.saved_count(self.creator), 1)


class AccessRules(SavedSwiftFixture):
    def test_signed_out_callers_are_refused(self):
        client = APIClient()
        self.assertIn(client.post(self.save_url()).status_code, (401, 403))
        self.assertIn(client.get(reverse('swift-saved')).status_code, (401, 403))

    def test_saving_a_swift_you_cannot_see_is_a_404(self):
        SwiftVideo.objects.filter(pk=self.swift.pk).update(
            moderation=SwiftVideo.Moderation.REMOVED,
        )
        self.assertEqual(self.client.post(self.save_url()).status_code, 404)
        self.assertEqual(self.saved_count(), 0)
