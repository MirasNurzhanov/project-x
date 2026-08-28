"""The 0026 data migration, run for real.

`users/test_zones.py` proves the shape the migration lands *in* behaves. This
proves the transform itself: rows written in the old two-value world come out
the other side as the right one of three, which is the only part a fresh test
database never exercises — it has no rows when the migration runs.

Done by driving the migration executor backwards to 0025, writing rows through
the historical model, then migrating forward. TransactionTestCase because the
schema changes have to commit; that makes this the slowest file in the suite by
some way, which is the price of testing a migration rather than trusting it.
"""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class ZoneTypeMigrationTests(TransactionTestCase):
    # Deliberately no `available_apps`. Restricting the registry to users and
    # regions leaves the migration graph inconsistent — users.0003 depends on
    # interests, and the loader refuses to build a graph with a dangling parent.
    # The cost of the full registry is that this flushes every table between
    # tests, which is the reason this file is slow rather than a problem.

    # `regions` is pinned as well as `users`. Targeting users/0025 alone
    # resolves the historical state of every other app to whatever its own
    # dependencies require — which for regions is 0001, before `kind` was added
    # in 0002. The fixtures below need that column, and the migration under test
    # reads it.
    REGIONS_HEAD = ('regions', '0006_region_region_kind_lat_idx_and_more')
    migrate_from = [('users', '0025_remove_user_music_zone_separate'), REGIONS_HEAD]
    migrate_to = [('users', '0026_zone_type'), REGIONS_HEAD]

    def setUp(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        executor.loader.build_graph()
        self.old_apps = executor.loader.project_state(self.migrate_from).apps

    def tearDown(self):
        # Leave the database at **head**, not at `migrate_to`.
        #
        # This used to target `migrate_to`, which was the head when it was
        # written. It has not been since 0027, and the consequence is invisible
        # right up until it is not: every `TransactionTestCase` that runs after
        # this one — they share a bucket, and Django runs the bucket in
        # discovery order — inherits a schema missing every column added since
        # 0026, and fails with "no such column" somewhere that has nothing to
        # do with migrations. Whether it shows up at all depends on which app
        # labels the run was given, so it hides in a full run and appears in a
        # narrower one.
        #
        # Reading the head off the graph rather than naming it means this
        # cannot rot again the next time a migration lands.
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())

    def migrate_forward(self):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(self.migrate_to)
        executor.loader.build_graph()
        return executor.loader.project_state(self.migrate_to).apps

    def make_region(self, apps, osm_id, kind):
        Region = apps.get_model('regions', 'Region')
        return Region.objects.create(
            osm_type='relation', osm_id=osm_id,
            name=f'R{osm_id}', display_name=f'R{osm_id}, Testland',
            kind=kind,
            min_lat=0.0, min_lon=0.0, max_lat=1.0, max_lon=1.0,
            centroid_lat=0.5, centroid_lon=0.5,
            rings=[[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]]],
            point_count=5,
        )

    def make_user(self, apps, username, **kwargs):
        User = apps.get_model('users', 'User')
        return User.objects.create(
            username=username, email=f'{username}@example.com', password='x', **kwargs
        )

    def test_every_old_shape_lands_on_the_right_type(self):
        city = self.make_region(self.old_apps, 900, 'city')
        district = self.make_region(self.old_apps, 901, 'district')
        province = self.make_region(self.old_apps, 902, 'other')

        # A legacy circle, with a radius that is not the default — the value has
        # to survive, which is the promise made to every existing account.
        self.make_user(self.old_apps, 'legacy-circle', zone_mode='radius', zone_radius=1337)
        self.make_user(self.old_apps, 'city-zone', zone_mode='region', zone_region=city)
        self.make_user(self.old_apps, 'district-zone', zone_mode='region', zone_region=district)
        self.make_user(self.old_apps, 'other-zone', zone_mode='region', zone_region=province)
        # Region mode with the Region evicted — `zone_region` is SET_NULL, so
        # this is a real row, and it has no boundary to carry forward.
        self.make_user(self.old_apps, 'orphan-zone', zone_mode='region', zone_region=None)

        new_apps = self.migrate_forward()
        User = new_apps.get_model('users', 'User')
        got = {u.username: u for u in User.objects.all()}

        self.assertEqual(got['legacy-circle'].zone_type, 'circle')
        self.assertEqual(got['legacy-circle'].zone_radius, 1337)
        self.assertEqual(got['city-zone'].zone_type, 'city')
        self.assertEqual(got['district-zone'].zone_type, 'district')
        # Grandfathered rather than classified — see the migration's docstring.
        self.assertEqual(got['other-zone'].zone_type, 'city')
        # The one that must not become a city covering nothing.
        self.assertEqual(got['orphan-zone'].zone_type, 'circle')

    def test_the_boundary_survives_the_transform(self):
        """The type is only half of it — a city zone whose region was dropped on
        the way through would migrate to a state that cannot be tested."""
        city = self.make_region(self.old_apps, 903, 'city')
        self.make_user(self.old_apps, 'keeps-city', zone_mode='region', zone_region=city)

        new_apps = self.migrate_forward()
        User = new_apps.get_model('users', 'User')
        user = User.objects.get(username='keeps-city')
        self.assertEqual(user.zone_type, 'city')
        self.assertEqual(user.zone_region_id, city.pk)
