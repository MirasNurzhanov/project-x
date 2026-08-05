"""CPU profile of the hot endpoints. The other half of `perf_probe`.

    python manage.py test core.perf_profile --settings=core.test_settings -v2

`perf_probe` answers "how many queries", which is the right question when the
database is the bottleneck. Once it isn't, the remaining milliseconds are
Python — serialising, encoding, and the middleware chain — and this says how
they split, so a library swap can be aimed instead of guessed.

Runs against the in-memory SQLite of `core.test_settings`, where the database
costs almost nothing. That is a feature here: what is left is the CPU that a
faster database would not remove.
"""
import cProfile
import io
import pstats
import time

from rest_framework.test import APIClient

from core import perf_probe

# Imported as a module, not `from ... import Probe`: a TestCase bound at module
# level here would be collected and run a second time by every invocation of
# this module.

REPEATS = 40
#: Rounds of REPEATS. The reported figure is the fastest round, not the mean:
#: a developer machine only ever interferes in one direction, so the minimum is
#: the closest thing to an uncontended measurement that a laptop will give.
#: Averaging just folds in whatever else was running, and on a box with a file
#: sync daemon that noise swamped a 2x difference entirely.
ROUNDS = 3


class Profile(perf_probe.Probe):
    """Reuses `Probe`'s fixture; measures time instead of queries."""

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.me)

    def test_probe(self):
        # Inherited from Probe. Query counts are its job, not this module's.
        self.skipTest('query counts live in core.perf_probe')

    def _hot(self, label, path, repeats=REPEATS):
        self.client.get(path)  # warm

        # Wall clock and CPU, because they answer different questions and
        # disagree exactly when the measurement is untrustworthy. CPU time is
        # what a library swap moves, and it does not count the milliseconds
        # this process spent descheduled while something else used the core.
        wall = cpu = float('inf')
        for _ in range(ROUNDS):
            w0, c0 = time.perf_counter(), time.process_time()
            for _ in range(repeats):
                self.client.get(path)
            wall = min(wall, (time.perf_counter() - w0) / repeats * 1000)
            cpu = min(cpu, (time.process_time() - c0) / repeats * 1000)

        profiler = cProfile.Profile()
        profiler.enable()
        for _ in range(repeats):
            self.client.get(path)
        profiler.disable()

        # The figure to compare two revisions on. Timings on a laptop wander by
        # tens of percent between identical runs; the number of Python calls it
        # takes to answer the request does not move at all, and is what any of
        # this is ultimately buying down.
        stats = pstats.Stats(profiler)
        calls = stats.total_calls / repeats

        print(f'\n{"=" * 92}\n{label}   {calls:,.0f} python calls/request   '
              f'{cpu:.1f}ms CPU / {wall:.1f}ms wall '
              f'(best of {ROUNDS}x{repeats})\n{"=" * 92}')
        buf = io.StringIO()
        pstats.Stats(profiler, stream=buf).sort_stats('tottime').print_stats(18)
        # Drop pstats' preamble; the table is the point.
        print('\n'.join(buf.getvalue().splitlines()[4:]))

        # Named chokepoints, by cumulative time: which *layer* the request is
        # in, rather than which line. This is what a library swap moves.
        buf = io.StringIO()
        pstats.Stats(profiler, stream=buf).sort_stats('cumulative').print_stats(
            r'(render|encoder|gzip|get_fields|to_representation|'
            r'execute_sql|_fetch_all|authenticate|middleware)'
        )
        print('\n'.join(buf.getvalue().splitlines()[4:]))
        return cpu

    def test_profile(self):
        self._hot('GET /api/proximity/nearby/', '/api/proximity/nearby/')
        self._hot('GET /api/clubs/', '/api/clubs/')
