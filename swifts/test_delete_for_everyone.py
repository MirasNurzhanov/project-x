"""Deleting a Swift takes it down for *everyone*, on every surface.

The claim the product makes when it says "Delete for everyone" is not "the
author stops seeing it". It is that the video leaves the app — for the stranger
scrolling the feed, for the friend who saved it, for anyone holding a direct
link, and for the author too. That is a claim about a dozen read paths, and it
is only as true as the least careful one.

`live_q()` carries `deleted_at__isnull=True` and every read is meant to be built
on it, so in principle one filter covers all of them. This file is the proof
that no surface has quietly grown its own query around the side — including the
ranked feed, which serves ids chosen *before* the deletion and so is the one
place where "already decided" could beat "now gone".

Written against the real `DELETE /api/swifts/<id>/` rather than a column
update, because what is being tested is the button, not the column.
"""
from django.urls import reverse
from rest_framework.test import APIClient

from .models import SavedSwift, SwiftVideo
from .testing import SwiftTestCase, make_swift, make_user


class DeletedSwiftIsGoneEverywhere(SwiftTestCase):
    def setUp(self):
        super().setUp()
        self.author = make_user('author')
        self.viewer = make_user('viewer')
        self.swift = make_swift(self.author, located=True, shared=True)

        self.author_client = APIClient()
        self.author_client.force_authenticate(self.author)
        self.viewer_client = APIClient()
        self.viewer_client.force_authenticate(self.viewer)

        # The viewer has watched it and saved it — the two states most likely
        # to keep a stale copy reachable.
        self.viewer_client.post(reverse('swift-view', args=[self.swift.pk]))
        self.viewer_client.post(reverse('swift-save', args=[self.swift.pk]))

    def delete_it(self):
        response = self.author_client.delete(
            reverse('swift-detail', args=[self.swift.pk]),
        )
        self.assertEqual(response.status_code, 204, response.data)

    def feed_ids(self, client):
        # `chrono=1`: the reverse-chronological mode. Deterministic, where the
        # ranked one is a recommender whose ordering is not this file's subject
        # — the ranked path gets its own test below.
        body = client.get(reverse('swift-feed'), {'chrono': '1'}).json()
        return [row['id'] for row in body.get('results', [])]

    # ── Before, so the tests cannot pass by asserting an absence that was
    #    always true. ──────────────────────────────────────────────────────────
    def test_the_fixture_is_visible_to_begin_with(self):
        self.assertIn(self.swift.pk, self.feed_ids(self.viewer_client))
        self.assertEqual(
            self.viewer_client.get(reverse('swift-detail', args=[self.swift.pk])).status_code,
            200,
        )
        self.assertEqual(
            self.viewer_client.get(reverse('swift-saved')).json()['count'], 1,
        )

    # ── After ────────────────────────────────────────────────────────────────
    def test_it_leaves_another_persons_feed(self):
        self.delete_it()
        self.assertNotIn(self.swift.pk, self.feed_ids(self.viewer_client))

    def test_a_direct_link_stops_working_for_everyone(self):
        self.delete_it()
        url = reverse('swift-detail', args=[self.swift.pk])
        # 404 rather than 410 or 403: a Swift you cannot reach is
        # indistinguishable from one that never existed.
        self.assertEqual(self.viewer_client.get(url).status_code, 404)
        # The author included. Deleting is not archiving.
        self.assertEqual(self.author_client.get(url).status_code, 404)

    def test_it_leaves_the_creator_gallery(self):
        self.delete_it()
        for client in (self.viewer_client, self.author_client):
            body = client.get(reverse('swift-creator', args=[self.author.pk])).json()
            self.assertNotIn(self.swift.pk, [row['id'] for row in body.get('results', [])])

    def test_it_leaves_the_authors_own_list(self):
        """`mine/` shows the author rows the feed withholds — drafts, pending
        uploads. A deleted one is not among them."""
        self.delete_it()
        body = self.author_client.get(reverse('swift-mine')).json()
        self.assertNotIn(self.swift.pk, [row['id'] for row in body.get('results', [])])

    def test_it_leaves_the_shortlist_of_whoever_saved_it(self):
        self.delete_it()
        self.assertEqual(self.viewer_client.get(reverse('swift-saved')).json()['count'], 0)
        # The bookmark row survives; it is the Swift that is gone. Nothing was
        # deleted out of somebody else's data.
        self.assertEqual(SavedSwift.objects.filter(user=self.viewer).count(), 1)

    def test_its_location_can_no_longer_be_revealed(self):
        """The sharpest one: the viewer had already watched it, so the reveal
        gate was satisfied before the deletion."""
        self.delete_it()
        response = self.viewer_client.post(
            reverse('swift-reveal-location', args=[self.swift.pk]),
        )
        self.assertEqual(response.status_code, 404)

    def test_nobody_can_engage_with_it_any_more(self):
        self.delete_it()
        for name in ('swift-like', 'swift-repost', 'swift-save', 'swift-comments'):
            response = self.viewer_client.post(reverse(name, args=[self.swift.pk]))
            self.assertEqual(response.status_code, 404, f'{name} answered {response.status_code}')

    def test_it_is_not_served_from_a_ranked_session_opened_before_the_delete(self):
        """The one place "already decided" could beat "now gone".

        The ranked feed picks ids up front and pages through them, so a
        deletion lands *after* this Swift was chosen. Every page has to
        re-apply the gate rather than trust the session.

        A fresh viewer, because `setUp`'s has already watched this Swift and
        the recommender does not re-serve what somebody has seen — which would
        make the assertion pass for the wrong reason.
        """
        fresh = APIClient()
        fresh.force_authenticate(make_user('fresh'))

        first = fresh.get(reverse('swift-feed')).json()
        self.assertIn(
            self.swift.pk, [row['id'] for row in first.get('results', [])],
            'fixture should be in the ranked feed before the delete',
        )

        self.delete_it()

        # Drain every remaining page of the session opened above.
        seen = []
        cursor = first.get('next_cursor')
        while cursor:
            body = fresh.get(reverse('swift-feed'), {'cursor': cursor}).json()
            seen += [row['id'] for row in body.get('results', [])]
            cursor = body.get('next_cursor')
        # And a fresh read of page one, which is served from the same session.
        seen += [row['id'] for row in fresh.get(reverse('swift-feed')).json().get('results', [])]
        self.assertNotIn(self.swift.pk, seen)

    def test_the_row_survives_so_the_audit_does(self):
        """Soft, not hard. The reveal audit, the comments and the likes are
        worth more after a Swift comes down than before — but none of it is
        reachable through the product."""
        self.delete_it()
        row = SwiftVideo.objects.filter(pk=self.swift.pk).first()
        self.assertIsNotNone(row)
        self.assertIsNotNone(row.deleted_at)


class OnlyTheAuthorCanTakeItDown(SwiftTestCase):
    def setUp(self):
        super().setUp()
        self.author = make_user('author')
        self.swift = make_swift(self.author)

    def delete_as(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client.delete(reverse('swift-detail', args=[self.swift.pk]))

    def test_a_stranger_cannot(self):
        response = self.delete_as(make_user('stranger'))
        self.assertEqual(response.status_code, 404)
        self.assertIsNone(SwiftVideo.objects.get(pk=self.swift.pk).deleted_at)

    def test_somebody_who_saved_it_cannot(self):
        """Saving is a bookmark, not a claim on the thing."""
        saver = make_user('saver')
        client = APIClient()
        client.force_authenticate(saver)
        client.post(reverse('swift-save', args=[self.swift.pk]))

        self.assertEqual(self.delete_as(saver).status_code, 404)
        self.assertIsNone(SwiftVideo.objects.get(pk=self.swift.pk).deleted_at)

    def test_a_signed_out_caller_cannot(self):
        response = APIClient().delete(reverse('swift-detail', args=[self.swift.pk]))
        self.assertIn(response.status_code, (401, 403))
        self.assertIsNone(SwiftVideo.objects.get(pk=self.swift.pk).deleted_at)

    def test_deleting_twice_is_not_an_error_the_second_time_it_is_a_404(self):
        """Once it is gone it is gone, including to the author — so a retried
        request cannot resurrect or re-stamp it."""
        client = APIClient()
        client.force_authenticate(self.author)
        url = reverse('swift-detail', args=[self.swift.pk])
        self.assertEqual(client.delete(url).status_code, 204)
        first_stamp = SwiftVideo.objects.get(pk=self.swift.pk).deleted_at

        self.assertEqual(client.delete(url).status_code, 404)
        self.assertEqual(SwiftVideo.objects.get(pk=self.swift.pk).deleted_at, first_stamp)
