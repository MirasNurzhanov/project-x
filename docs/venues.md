# Venue meetup foundation

Only Dostyk Plaza is seeded as an active venue. This is an editorial demo place,
not a claim of partnership and not a reservation system. No Google Places calls
or new client API keys/native modules are required.

## Deployment

Apply the normal backend migrations before trying the updated mobile flow:

```
python manage.py migrate
```

`venues.0001` creates the directory; `venues.0002` adds Dostyk Plaza idempotently
without overwriting existing staff edits. `clubs.0017` adds nullable venue links
and optional meeting-point notes to existing events. The seed reversal is a
no-op so rolling back the seed alone cannot erase an edited venue. Normal schema
rollback can still remove the new tables/columns; back up first if data matters.

In Django admin, **Venues** controls the title, address, category, description,
coordinates, optional authorized image URL, and active status. New entries
default inactive. Leave other entries inactive for this single-venue preview.
There is an optional Google Place ID field for future integration, not a Places
content cache. Deactivate instead of deleting to preserve the venue relationship.
If a venue is deleted, existing events keep their location snapshot.

Address and coordinates were verified on 2026-09-15 against the venue's own
[contact page](https://dostykplaza.kz/en/contacts/), including its map's
`data-lat`/`data-lng`: Samal-2, 111, Almaty; 43.233139, 76.956562.
The default card uses a generic storefront icon, not an unlicensed mall photo.

## API and client flow

- Authenticated, read-only `GET /api/venues/` returns active venues, paginated at
  100 per page. The initial map preview loads the first page on focus and has an
  explicit retry state. Before a broader rollout, add viewport-based discovery
  or pagination to the map; this is not an unlimited global-directory fetch.
- `GET /api/venues/<id>/` returns an active venue or 404. Both reads use the
  existing map-query throttle.
- Pin → venue card → **Propose a meetup** → user's joined-club picker → existing
  club-chat proposal form. A club choice is shown even for one membership.
- The route carries the venue ID, not trusted coordinates. The proposal form
  reloads the venue from the server and offers an optional meeting-point note.
- `POST /api/clubs/<id>/events/` accepts optional `venue_id` and `meeting_point`.
  Existing membership/visibility checks remain in force. The server takes the
  location name and coordinates from the active venue, ignoring client location
  overrides, and keeps the event pending under the existing approval flow.
- Clearing or replacing the selected location removes the venue association;
  ordinary non-venue proposals remain supported.
- **Make a swift** is visible, disabled and labelled **Coming soon**. There is
  no Swift publishing, booking, payment, or business-dashboard integration.

## Verification and device checklist

Local validation on 2026-09-16: full backend suite 1,789 tests, no failures/errors
(9 environment-specific skips); PostgreSQL/Redis venue and club suite 101 tests,
no skips. Migrations were applied only by disposable test databases, not against
the developer database or production.

Automated coverage is in `venues.tests` and mobile `venues.test.tsx`: permissions,
active-only reads, location authority, location snapshots, seed repeatability,
pagination, club picking, errors/retry, prefill/reset, duplicate-submit guarding,
disabled Swift action and native marker parking. PostgreSQL CI includes venues.

Still verify on iPhone and Android development builds:

1. Pan to Dostyk Plaza in Almaty; tap the purple labelled storefront marker.
2. Check light/dark mode, large text, VoiceOver/TalkBack and a small screen.
3. Choose a club; verify the card closes before the proposal appears on iOS.
4. Enter a title/time and meeting point, submit, and confirm the pending proposal
   in club chat. Approve as a club owner and verify the normal meetup map card.
5. Cancel and reopen; ensure there is no old draft or duplicate modal.
6. Check zero-club and offline/retry states. Deactivate the venue in a test
   environment and confirm it disappears after map refocus and rejects new proposals.

No native dependencies changed: an existing SDK 54 development build can run
the JS changes. The backend migration/deployment is still required. Automated
tests do not establish native appearance or production deployment success.
