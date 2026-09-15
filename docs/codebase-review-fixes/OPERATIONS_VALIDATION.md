# Final four-batch validation — 2026-09-15

Implementation scope: M5/M15, M2/M3/M6, M1/M7/M13, H7/M8/M9/M10/M12.
This is not a resolution claim for all 39 original audit findings. The hardening
was committed locally and integrated with mobile `81c860b` and backend `ed4687c`.
Publishing is separate from validation; no production migration or deployment
was performed by this validation run.

Backend checks used an isolated Python 3.12.14 environment with the incoming
dependency lock (Django 5.2.17, DRF 3.17.2). The existing developer venv was not
modified. Dependency consistency (`pip check`) passed.

| Check | Result |
|---|---|
| Mobile Expo declared/installed | Both 54.0.37 |
| Mobile `npx tsc --noEmit` | Pass |
| Mobile Jest | 71 suites, 923 tests pass |
| Mobile lint | 0 errors, 4 warnings |
| Full Django SQLite suite | 1,778 tests; no failures or errors, 9 skipped |
| PostgreSQL/Redis targeted suite | 167 tests pass, no skips |
| Go push `go test -race ./...` | Pass, Go 1.23.12 |
| Go moderation `go test -race ./...` | Pass, Go 1.23.12 |
| `makemigrations --check --dry-run` | Pass; no drift |
| Production-shaped `check --deploy --fail-level WARNING` | Pass; dummy variables, no external verification |

The merged full backend suite is now green. Incoming fixes address the earlier
baseline failures in region geometry/limits, capacity configuration and feed
queries. Merge regression fixtures were updated for ticket authentication and
the capacity preflight without removing their behavioral assertions. Go race
results were obtained before integration; the incoming commits did not change
either Go service's source.

The PostgreSQL run used isolated localhost PostgreSQL 18.6 and Redis 8.10.1,
not Railway or a shared developer database. It covered:

```
core.test_operations core.test_socket_tickets core.test_message_media
core.test_capacity core.test_security core.test_websocket_suspension
notifications.test_push_delivery users.test_privacy_batch
users.test_account_deletion friends.test_connection_removal friends.test_hi_abuse
friends.test_chat_reliability
swifts.test_worker_endpoints swifts.test_publish_integrity
```

Use `--settings=core.ci_test_settings --noinput` with explicitly supplied disposable
CI_DATABASE_URL and CI_REDIS_URL. Ordinary full tests use core.test_settings.

## Remaining release gates

1. Private R2 bucket/permissions/public-access disablement, signed-media playback,
   transformations, URL-expiry behavior and legacy-public-media remediation (M3).
2. Native secure-storage build and coordinated ticket-auth client/backend rollout;
   deployed Redis and edge-log verification (M6).
3. Railway pre-deploy hook, exact proxy trust, migrations and supervised moderation
   worker; no schema work at web boot (H7, M8, M11).
4. Device tests for archived conversations, deletion/reauthentication, invitation
   consent and foreground location; scheduled deletion finalizer (M5, M15, M7).
5. If using Go push, configure/authenticate its dead-token callback. Delayed Expo
   receipt polling is an explicitly unimplemented delivery-monitoring enhancement (M9).
6. Confirm the actual GitHub CI jobs, including dependency-lock/audit checks and
   PostgreSQL 16. Local passing tests do not establish remote CI success.

No live Google Cloud, Railway or Cloudflare settings were changed.
