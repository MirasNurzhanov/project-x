# Codebase Review Fixes — Index

Baseline established: see [BASELINE.md](BASELINE.md). Source review: `Approx-Codebase-Review.pdf`.
Rows are updated as each batch completes; `TBD` means that finding has not been worked yet.

Severity is inferred only from the ID prefix (`C` = Critical, `H` = High, `M` = Medium, `L` = Low)
per the batch instructions.

| ID | Severity | Finding | Status | Tests | Report |
|---|---|---|---|---|---|
| C1 | Critical | Credentialed CORS on all URLs + wildcard CSRF trusted origins can expose Django admin | FIXED (code verified; Railway variables must be set before deploy) | `core/test_security.py` (37 in file, shared with M11) | [C1.md](C1.md) |
| H1 | High | TBD | UNREVIEWED | — | — |
| H2 | High | Club "invite" force-joins users without consent and ignores blocks | FIXED (pending invitation + acceptance; invitees need the app update to accept; UI not run on device; no deployment) | `clubs/test_invitations.py` (11), `notifications/test_push_targets.py` (1 changed), mobile `clubInvitations.test.ts` (2) | [H2.md](H2.md) |
| H3 | High | TBD | UNREVIEWED | — | — |
| H4 | High | TBD | UNREVIEWED | — | — |
| H5 | High | TBD | UNREVIEWED | — | — |
| H6 | High | TBD | UNREVIEWED | — | — |
| H7 | High | Migrations on web boot | IMPLEMENTED (Railway pre-deploy verification remains) | core.test_operations; deploy/migration checks | [H7.md](H7.md) |
| H8 | High | TBD | UNREVIEWED | — | — |
| H9 | High | TBD | UNREVIEWED | — | — |
| H10 | High | TBD | UNREVIEWED | — | — |
| M1 | Medium | Exact DOB in public payloads | IMPLEMENTED (age-only public contract; no deployment) | users.test_privacy_batch | [M1.md](M1.md) |
| M2 | Medium | Arbitrary image_url on new messages | FIXED (legacy reads retained; no deployment) | core.test_message_media (6); combined PostgreSQL/Redis suite (80) | [M2.md](M2.md) |
| M3 | Medium | Unvalidated uploads and public media | IMPLEMENTED (private R2 provisioning, native playback and legacy media remediation remain) | core.test_uploads; core.test_message_media; Swift suites | [M3.md](M3.md) |
| M4 | Medium | TBD | UNREVIEWED | — | — |
| M5 | Medium | Connection removal erased both message histories | FIXED (per-side flags and readable archive; no deployment) | friends.test_connection_removal; mobile archivedMessages | [M5.md](M5.md) |
| M6 | Medium | Plaintext credentials and JWT socket URLs | IMPLEMENTED (native rebuild, coordinated rollout and external logging verification required) | mobile credential/socket tests; core.test_socket_tickets; real Redis replay/concurrency | [M6.md](M6.md) |
| M7 | Medium | Foreground accuracy omitted and erased | IMPLEMENTED (explicit unknown vs omitted) | users.test_privacy_batch; mobile accuracy tests | [M7.md](M7.md) |
| M8 | Medium | Unbounded moderation threads and DB connections | IMPLEMENTED (supervised worker required) | burst/cleanup/backoff tests; PostgreSQL claim race | [M8.md](M8.md) |
| M9 | Medium | Discarded Expo tickets and dead tokens | IMPLEMENTED (Go callback/live verification remain; delayed receipts not implemented) | notifications.test_push_delivery; Go race suite | [M9.md](M9.md) |
| M10 | Medium | Inconsistent and sensitive production logs | IMPLEMENTED (external log collection verification remains) | core.test_structured_logging; audit; Go race suites | [M10.md](M10.md) |
| M11 | Medium | Deployment security settings are missing and forwarded proxy headers are trusted too broadly | NEEDS EXTERNAL ACTION (code complete; verified `FORWARDED_ALLOW_IPS` and post-deploy results pending) | `core/test_security.py` (37 in file, shared with C1) | [M11.md](M11.md) |
| M12 | Medium | Stale environment example | IMPLEMENTED (no live variables changed) | config inventory; deployment checks | [M12.md](M12.md) |
| M13 | Medium | Normalization discarded non-Latin scripts | IMPLEMENTED (Unicode preserved; multilingual policy gap documented) | users.test_privacy_batch; moderation suite | [M13.md](M13.md) |
| M14 | Medium | Club WebSocket write path skips validation performed by REST | FIXED (one serializer for REST + socket; `?before=` parsed; no deployment) | `clubs/test_message_validation.py` (5) | [M14.md](M14.md) |
| M15 | Medium | Account deletion lacked reauthentication and grace period | IMPLEMENTED (migration and scheduled finalizer required at deployment) | users.test_account_deletion; socket and mobile tests; PostgreSQL concurrency | [M15.md](M15.md) |
| M16 | Medium | TBD | UNREVIEWED | — | — |
| M17 | Medium | TBD | UNREVIEWED | — | — |
| M18 | Medium | TBD | UNREVIEWED | — | — |
| M19 | Medium | Safe, idempotent Hi requests (concurrent sends, notify once, decline retained) | FIXED (concurrency verified on PostgreSQL 18.4; 16.x and production version unverified; no deployment) | `friends/test_hi_abuse.py` (5) + `friends` suite (19) on PostgreSQL | [M19.md](M19.md) |
| M20 | Medium | TBD | UNREVIEWED | — | — |
| L1 | Low | TBD | UNREVIEWED | — | — |
| L2 | Low | TBD | UNREVIEWED | — | — |
| L3 | Low | TBD | UNREVIEWED | — | — |
| L4 | Low | TBD | UNREVIEWED | — | — |
| L5 | Low | TBD | UNREVIEWED | — | — |
| L6 | Low | TBD | UNREVIEWED | — | — |
| L7 | Low | TBD | UNREVIEWED | — | — |
| L8 | Low | TBD | UNREVIEWED | — | — |

## Baseline test/check results (for reference — see BASELINE.md for full detail)

| Check | Command | Result |
|---|---|---|
| Backend tests | `python manage.py test --settings=core.test_settings` | **FAIL** — 1586 tests, 1 failure (`test_ranked_feed_costs_one_ranking_pass_not_one_per_swipe`), 1 skipped |
| Backend deploy check | `python manage.py check --deploy --settings=core.test_settings` | PASS — 6 warnings |
| Backend migrations | `python manage.py makemigrations --check --dry-run --settings=core.test_settings` | PASS — no changes detected |
| Frontend tests | `npm test` (`jest --ci`) | PASS — 53 suites / 786 tests |
| Frontend types | `npx tsc --noEmit` | **FAIL** — 26 errors, all in 2 test files |
| Frontend lint | `npm run lint` (`expo lint`) | PASS — 0 errors, 3 warnings |

## Batches

| Batch | Findings | Date |
|---|---|---|
| BASELINE | — | 2026-09-13 |
| SECURITY PERIMETER | C1, M11 | 2026-09-13 |
| M11 PROXY TRUST | M11 | 2026-09-13 |
| CLUB CONSENT AND MESSAGE VALIDATION | H2, M14 | 2026-09-13 |
| USER DATA LIFECYCLE | M5, M15 | 2026-09-15 |
| MEDIA AND TOKEN SECURITY | M2, M3, M6 | 2026-09-15 |
| PRIVACY, LOCATION AND MODERATION | M1, M7, M13 | 2026-09-15 |
| OPERATIONS AND BACKGROUND WORK | H7, M8, M9, M10, M12 | 2026-09-15 |

Latest results and release gates: [OPERATIONS_VALIDATION.md](OPERATIONS_VALIDATION.md).
All four requested batches have implementation reports. This does not imply all
39 findings are resolved: untouched rows have not been re-audited here.
The full backend suite retains its independently reproduced baseline failures;
mobile, targeted PostgreSQL/Redis and Go race checks pass.

**Open deployment action from SECURITY PERIMETER:** with `DEBUG=False`, the backend refuses to
start without `ALLOWED_HOSTS`, or with a wildcard in `CSRF_TRUSTED_ORIGINS`. The web process also
refuses to start without a valid `FORWARDED_ALLOW_IPS`, which must be the Railway edge range verified
with Railway. Set the variables listed in [C1.md](C1.md) and [M11.md](M11.md) *before* the next push
to `main`, then run the post-deploy checks in M11.md.
