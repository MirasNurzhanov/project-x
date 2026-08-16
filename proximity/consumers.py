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
        from users import visibility
        from users.models import User
        from notifications.utils import notify_nearby_matches
        from encounters.utils import record_encounters
        from friends.models import FriendRequest, HiRequest
        from django.db.models import Q

        user = User.objects.prefetch_related('interests').get(pk=self.user.pk)
        # The socket is not a side door: same fuzzing as the REST endpoint.
        user.latitude, user.longitude = visibility.store_coordinates(user, lat, lon)
        user.save(update_fields=['latitude', 'longitude', 'updated_at'])
        lat, lon = float(user.latitude), float(user.longitude)

        if not user.is_on_map():
            return []

        blocked_ids = visibility.blocked_ids_for(user)
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

        base_qs = visibility.visible_users(user, on_map=True).filter(
            latitude__isnull=False,
            longitude__isnull=False,
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
            # Opting out of interest discovery keeps you off strangers' maps.
            stranger_candidates = stranger_candidates.filter(
                interests__in=user_interest_ids, discoverable_by_interest=True,
            ).distinct()

        nearby = []
        seen = set()
        # Alerts and encounter rows are collected here and written once at the
        # end, rather than costing ~10 queries per person in range. See
        # notifications.utils.notify_nearby_matches.
        matches = []

        # Distances come from the stored position, which is already snapped for
        # anyone on Approximate — coarse by construction, not by withholding.
        def entry_for(candidate, dist):
            return {
                'id': candidate.id,
                'username': candidate.username,
                'distance_km': dist,
                'location_precision': (
                    'approximate'
                    if candidate.location_sharing == User.LocationSharing.APPROXIMATE
                    else 'precise'
                ),
            }

        for candidate in friend_candidates:
            dist = round(haversine(lat, lon, float(candidate.latitude), float(candidate.longitude)), 2)
            matches.append((candidate, dist))
            nearby.append(entry_for(candidate, dist))
            seen.add(candidate.pk)

        for candidate in connected_candidates:
            if candidate.pk in seen:
                continue
            dist = round(haversine(lat, lon, float(candidate.latitude), float(candidate.longitude)), 2)
            if dist <= CONNECTED_RADIUS_KM:
                matches.append((candidate, dist))
                nearby.append(entry_for(candidate, dist))
                seen.add(candidate.pk)

        for candidate in stranger_candidates:
            if candidate.pk in seen:
                continue
            dist = round(haversine(lat, lon, float(candidate.latitude), float(candidate.longitude)), 2)
            if dist <= DEFAULT_RADIUS_KM:
                matches.append((candidate, dist))
                nearby.append(entry_for(candidate, dist))
                seen.add(candidate.pk)

        # The socket is not a side door: same rules as UpdateLocationView.
        # Both are reached only after the paused early-return above, and
        # `visible_users(on_map=True)` settled the candidates.
        notify_nearby_matches(user, matches)
        record_encounters(user, matches)

        nearby.sort(key=lambda u: (u['distance_km'] is None, u['distance_km'] or 0))
        return nearby
