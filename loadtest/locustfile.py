"""Load profile for Aura's API, shaped like the app actually behaves.

Not a synthetic hammer on one URL. The weights below come from the client's own
polling: the map ticks every 10s, the chat tab every 15s (and fetches *two* club
lists per tick), the alert badge every 10s, and background location reports
every 30s whether the app is open or not. Getting those ratios right is the
difference between a load test and a number.

    pip install locust
    locust -f loadtest/locustfile.py --host https://your-api.example.com

Headless, for a repeatable run in CI or before a deploy:

    locust -f loadtest/locustfile.py --host https://your-api.example.com \
           --headless --users 200 --spawn-rate 20 --run-time 5m \
           --csv=loadtest/results/baseline

Credentials come from the environment so no password is ever written down here:

    AURA_LOAD_EMAIL=you@example.com AURA_LOAD_PASSWORD=... locust -f ...

Point this at a staging database. It writes: locations move, encounters and
nearby alerts get logged, and clubs get joined.
"""
import os
import random

from locust import HttpUser, between, task

#: Dubai, near the centre — the region this backend is being tuned for.
BASE_LAT, BASE_LON = 25.2048, 55.2708
#: About 1.5km of jitter, so users land in and out of each other's 500m radius
#: instead of all sitting on one point (which would make every proximity query
#: trivially empty or trivially full).
JITTER = 0.015


class AuraUser(HttpUser):
    # The app is a poller, not a thinker. Short waits, because the intervals in
    # the tasks below are what actually shape the load.
    wait_time = between(1, 3)

    def on_start(self):
        email = os.environ.get('AURA_LOAD_EMAIL')
        password = os.environ.get('AURA_LOAD_PASSWORD')
        if not email or not password:
            raise RuntimeError(
                'Set AURA_LOAD_EMAIL and AURA_LOAD_PASSWORD before running.'
            )
        response = self.client.post(
            '/api/users/login/',
            json={'email': email, 'password': password},
            name='POST /api/users/login/',
        )
        response.raise_for_status()
        self.client.headers.update({
            'Authorization': f"Bearer {response.json()['access']}",
            # Marks the traffic as foregrounded, which is what the presence
            # middleware keys on — without it this load test would not exercise
            # the `last_seen` write path at all.
            'X-Aura-Presence': 'foreground',
            'Accept-Encoding': 'gzip',
        })
        self.club_ids = []

    def _position(self):
        return {
            'latitude': f'{BASE_LAT + random.uniform(-JITTER, JITTER):.8f}',
            'longitude': f'{BASE_LON + random.uniform(-JITTER, JITTER):.8f}',
            'accuracy': random.uniform(5, 40),
            'speed': random.choice([-1, 0.5, 1.4, 3.0, 9.0]),
        }

    # ── The map, every 10s per client ─────────────────────────────────────────
    @task(30)
    def map_nearby(self):
        self.client.get('/api/map/nearby/', name='GET /api/map/nearby/')

    # ── Location reports, every 30s per client, app open or not ───────────────
    @task(20)
    def report_location(self):
        self.client.patch(
            '/api/proximity/location/', json=self._position(),
            name='PATCH /api/proximity/location/',
        )

    # ── The chat tab: two club lists plus the place, every 15s ────────────────
    @task(12)
    def club_lists(self):
        response = self.client.get('/api/clubs/', name='GET /api/clubs/ (discover)')
        if response.ok:
            self.club_ids = [club['id'] for club in response.json()][:20]
        self.client.get('/api/clubs/?joined=true', name='GET /api/clubs/ (joined)')
        self.client.get('/api/clubs/my-location/', name='GET /api/clubs/my-location/')

    # ── The badge polls that run on every authenticated screen ────────────────
    @task(12)
    def badges(self):
        self.client.get(
            '/api/friends/messages/unread/', name='GET /api/friends/messages/unread/',
        )
        self.client.get('/api/notifications/', name='GET /api/notifications/')

    @task(8)
    def friends_list(self):
        self.client.get('/api/friends/list/', name='GET /api/friends/list/')

    # ── Opening things ────────────────────────────────────────────────────────
    @task(5)
    def club_detail(self):
        if not self.club_ids:
            return
        club_id = random.choice(self.club_ids)
        self.client.get(f'/api/clubs/{club_id}/', name='GET /api/clubs/[id]/')
        self.client.get(
            f'/api/clubs/{club_id}/members/', name='GET /api/clubs/[id]/members/',
        )

    @task(3)
    def stories(self):
        self.client.get('/api/stories/nearby/', name='GET /api/stories/nearby/')

    # ── Writes, rare by comparison ────────────────────────────────────────────
    @task(1)
    def join_a_club(self):
        if not self.club_ids:
            return
        club_id = random.choice(self.club_ids)
        # 400 "already a member" is an expected outcome here, not a failure.
        with self.client.post(
            f'/api/clubs/{club_id}/join/', name='POST /api/clubs/[id]/join/',
            catch_response=True,
        ) as response:
            if response.status_code in (201, 400, 404):
                response.success()

    @task(1)
    def create_a_story(self):
        position = self._position()
        self.client.post(
            '/api/stories/',
            json={
                'content': 'load test story',
                'latitude': position['latitude'],
                'longitude': position['longitude'],
            },
            name='POST /api/stories/',
        )
