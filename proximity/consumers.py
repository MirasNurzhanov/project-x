import json
import math
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from .utils import haversine

DEFAULT_RADIUS_KM = 0.5
CONNECTED_RADIUS_KM = 1.0


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
        from friends.models import BlockedUser, FriendRequest, HiRequest
        from django.db.models import Q

        user = User.objects.prefetch_related('interests').get(pk=self.user.pk)
        user.latitude = lat
        user.longitude = lon
        user.save(update_fields=['latitude', 'longitude', 'updated_at'])

        if user.privacy_mode == 'invisible':
            return []

        blocked_others_ids = set(BlockedUser.objects.filter(blocker=user).values_list('blocked_id', flat=True))
        blocked_me_ids = set(BlockedUser.objects.filter(blocked=user).values_list('blocker_id', flat=True))
        blocked_ids = blocked_others_ids | blocked_me_ids
        excluded_ids = blocked_ids | {user.pk}

        friend_ids = set()
        for s, r in FriendRequest.objects.filter(
            status=FriendRequest.Status.ACCEPTED
        ).filter(
            Q(sender=user) | Q(receiver=user)
        ).values_list('sender_id', 'receiver_id'):
            other = r if s == user.pk else s
            if other not in excluded_ids:
                friend_ids.add(other)

        connected_ids = set()
        for s, r in HiRequest.objects.filter(
            status=HiRequest.Status.ACCEPTED
        ).filter(
            Q(sender=user) | Q(receiver=user)
        ).values_list('sender_id', 'receiver_id'):
            other = r if s == user.pk else s
            if other not in excluded_ids and other not in friend_ids:
                connected_ids.add(other)

        base_qs = (
            User.objects.filter(
                latitude__isnull=False,
                longitude__isnull=False,
                privacy_mode__in=['active', 'ghost'],
            )
            .exclude(pk=user.pk)
            .exclude(pk__in=blocked_ids)
        )

        # Friends: always visible, no distance limit
        friend_candidates = base_qs.filter(pk__in=friend_ids) if friend_ids else User.objects.none()

        # Connected: visible within CONNECTED_RADIUS_KM
        if connected_ids:
            conn_lat_delta = CONNECTED_RADIUS_KM / 111.0
            conn_lon_delta = CONNECTED_RADIUS_KM / (111.0 * math.cos(math.radians(lat)))
            connected_candidates = base_qs.filter(
                pk__in=connected_ids,
                latitude__range=(lat - conn_lat_delta, lat + conn_lat_delta),
                longitude__range=(lon - conn_lon_delta, lon + conn_lon_delta),
            )
        else:
            connected_candidates = User.objects.none()

        # Strangers: visible within DEFAULT_RADIUS_KM, filtered by age_tier and interests
        str_lat_delta = DEFAULT_RADIUS_KM / 111.0
        str_lon_delta = DEFAULT_RADIUS_KM / (111.0 * math.cos(math.radians(lat)))
        stranger_candidates = (
            base_qs
            .exclude(pk__in=friend_ids)
            .exclude(pk__in=connected_ids)
            .filter(
                latitude__range=(lat - str_lat_delta, lat + str_lat_delta),
                longitude__range=(lon - str_lon_delta, lon + str_lon_delta),
            )
        )
        if user.age_tier:
            stranger_candidates = stranger_candidates.filter(age_tier=user.age_tier)
        user_interest_ids = list(user.interests.values_list('id', flat=True))
        if user_interest_ids:
            stranger_candidates = stranger_candidates.filter(interests__in=user_interest_ids).distinct()

        nearby = []
        seen = set()

        for candidate in friend_candidates:
            dist = round(haversine(lat, lon, float(candidate.latitude), float(candidate.longitude)), 2)
            notify_nearby_match(user, candidate, dist)
            record_encounter(user, candidate, dist)
            entry = {'id': candidate.id, 'username': candidate.username}
            entry['distance_km'] = dist if candidate.privacy_mode == 'active' else None
            nearby.append(entry)
            seen.add(candidate.pk)

        for candidate in connected_candidates:
            if candidate.pk in seen:
                continue
            dist = round(haversine(lat, lon, float(candidate.latitude), float(candidate.longitude)), 2)
            if dist <= CONNECTED_RADIUS_KM:
                notify_nearby_match(user, candidate, dist)
                record_encounter(user, candidate, dist)
                entry = {'id': candidate.id, 'username': candidate.username}
                entry['distance_km'] = dist if candidate.privacy_mode == 'active' else None
                nearby.append(entry)
                seen.add(candidate.pk)

        for candidate in stranger_candidates:
            if candidate.pk in seen:
                continue
            dist = round(haversine(lat, lon, float(candidate.latitude), float(candidate.longitude)), 2)
            if dist <= DEFAULT_RADIUS_KM:
                notify_nearby_match(user, candidate, dist)
                record_encounter(user, candidate, dist)
                entry = {'id': candidate.id, 'username': candidate.username}
                entry['distance_km'] = dist if candidate.privacy_mode == 'active' else None
                nearby.append(entry)
                seen.add(candidate.pk)

        nearby.sort(key=lambda u: (u['distance_km'] is None, u['distance_km'] or 0))
        return nearby
