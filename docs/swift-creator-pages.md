# Swift profiles, creator analytics and Ghost Mode

## App navigation

- The profile's **My Swifts** section shows the owner's recent uploads. **See all** opens the paginated gallery; each tile opens the existing Swift player.
- Owners see review, processing and audience labels. Other viewers only receive ready, approved videos permitted by the creator's profile and video audience settings.
- **Analytics** opens Creator studio. The Analytics button on an owned Swift opens the same page for that video alone.
- Creator studio has 7-, 28- and 90-day periods, views/likes/comments/reposts, previous-period comparisons, interactive line and bar charts, lifetime totals and the top ten viewed Swifts in the selected period.

## Privacy contract

Ghost Mode is the anonymous mode. It hides profile details, stories and Swifts from other viewers, including friends and accepted connections. Basic profile identity can still be shown. Owners retain access to their uploads and analytics.

Ghost Mode also prevents a user from seeing other people's map locations and from appearing on their maps. The server enforces this for nearby queries, map story pins, Swift location reveals and encounter access. Ordinary private profiles retain their existing approval rules when Ghost Mode is off.

Creator galleries and individual Swift links revalidate when revisited. Profile/gallery screens also revalidate when the app returns to the foreground. Pending responses cannot restore content after those screens are left. Visibility changes are enforced on API requests; these changes do not introduce a live push channel to erase content already loaded on another device.

## API

All endpoints require authentication and return `Cache-Control: no-store`.

| Endpoint | Parameters | Access |
| --- | --- | --- |
| `GET /api/swifts/creator/<user_id>/` | `limit` (default 24, maximum 50), `offset` | Owner's retained uploads, or the allowed public profile gallery |
| `GET /api/swifts/analytics/` | `days` (7, 28 or 90; default 7), optional `swift_id` | Authenticated owner's aggregates only |

Analytics uses the `swift_analytics` throttle scope, configured by `THROTTLE_SWIFT_ANALYTICS` (default `60/min`). Queries aggregate across the requested windows rather than issuing one activity query per video. Responses contain counts, dates and the owner's video metadata; they do not expose viewer identities or locations.

## Metric definitions

- **Views:** each viewer counts once per Swift, on the stored first-view date. Replays do not add views; watching two different Swifts counts separately.
- **Likes, comments and reposts:** retained actions grouped by creation date. Undoing an action, or removing a comment, can change historical figures. Only visible, undeleted comments are included in the daily series.
- Dates use UTC. The current reporting period includes today up to the request time; today may be incomplete. The preceding period covers the same number of calendar dates.
- Lifetime totals use the existing video counters. Removed/deleted Swifts are excluded from the gallery and analytics.
- These are statistics supported by existing stored activity. Watch-time, completion and follower-growth statistics are not collected by this change.

## Rollout and device checks

Deploy the backend before releasing the matching mobile JavaScript. No database migration or native dependency change is required. Use the existing SDK 54 development build to check the screens on iOS and Android.

1. Upload a Swift and return to your profile. Check that it appears with its current review status and opens in the player.
2. Open the same profile from another account. Verify only approved, audience-allowed Swifts appear; check an ordinary private account before and after approval.
3. Turn on Ghost Mode. Check with both a friend and a non-friend that profile details, stories and Swifts are hidden, including saved Swift/gallery links. Check the owner's gallery and analytics still work.
4. Check both users' maps after the privacy change, including previously open location sheets. Ghost Mode must hide other people from the anonymous viewer and hide that viewer from everyone else.
5. Open Analytics from the profile and from an owned Swift. Change date periods and metrics, tap dates, switch line/bar charts and pull to refresh. Check an empty account as well as one with activity.
6. Open analytics over a playing Swift and return. The underlying video must stop while analytics is open and reload its permission when revisited.
