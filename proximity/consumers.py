import json
import math
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from .utils import haversine

DEFAULT_RADIUS_KM = 5


class LocationConsumer(AsyncWebsocketConsumer):

    async def connect(self):
        user = self.scope.get('user')
        if not user or not user.is_authenticated:
            await self.close(code=4001)
            return
        self.user = user
        await self.accept()

    async def disconnect(self, code):
        pass

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
            lat = float(data['latitude'])
            lon = float(data['longitude'])
        except (KeyError, ValueError, TypeError):
            await self._send_error('Invalid payload. Expected {"latitude": ..., "longitude": ...}.')
            return

        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            await self._send_error('Coordinates out of range.')
            return

        nearby = await self._update_and_find_nearby(lat, lon)
        await self.send(json.dumps({'type': 'nearby_users', 'users': nearby}))

    async def _send_error(self, message):
        await self.send(json.dumps({'type': 'error', 'message': message}))

    @database_sync_to_async
    def _update_and_find_nearby(self, lat, lon):
        from users.models import User
        from notifications.utils import notify_nearby_match
        from encounters.utils import record_encounter

        user = User.objects.prefetch_related('interests').get(pk=self.user.pk)
        user.latitude = lat
        user.longitude = lon
        user.save(update_fields=['latitude', 'longitude', 'updated_at'])

        if user.privacy_mode == 'invisible':
            return []

        lat_delta = DEFAULT_RADIUS_KM / 111.0
        lon_delta = DEFAULT_RADIUS_KM / (111.0 * math.cos(math.radians(lat)))

        candidates = (
            User.objects.filter(
                latitude__isnull=False,
                longitude__isnull=False,
                privacy_mode__in=['active', 'ghost'],
                age_tier=user.age_tier,
                latitude__range=(lat - lat_delta, lat + lat_delta),
                longitude__range=(lon - lon_delta, lon + lon_delta),
            )
            .exclude(pk=user.pk)
            .prefetch_related('interests')
        )

        user_interest_ids = list(user.interests.values_list('id', flat=True))
        if user_interest_ids:
            candidates = candidates.filter(interests__in=user_interest_ids).distinct()

        nearby = []
        for candidate in candidates:
            dist = round(
                haversine(lat, lon, float(candidate.latitude), float(candidate.longitude)), 2
            )
            if dist <= DEFAULT_RADIUS_KM:
                notify_nearby_match(user, candidate, dist)
                record_encounter(user, candidate, dist)
                entry = {'id': candidate.id, 'username': candidate.username}
                if candidate.privacy_mode == 'active':
                    entry['distance_km'] = dist
                else:
                    entry['distance_km'] = None
                nearby.append(entry)

        nearby.sort(key=lambda u: (u['distance_km'] is None, u['distance_km'] or 0))
        return nearby
