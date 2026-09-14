# Codebase Review Fixes — Index

Baseline established: see [BASELINE.md](BASELINE.md). Source review: `Approx-Codebase-Review.pdf`.
Rows are updated as each batch completes; `TBD` means that finding has not been worked yet.

Severity is inferred only from the ID prefix (`C` = Critical, `H` = High, `M` = Medium, `L` = Low)
per the batch instructions.

| ID | Severity | Finding | Status | Tests | Report |
|---|---|---|---|---|---|
| C1 | Critical | Credentialed CORS on all URLs + wildcard CSRF trusted origins can expose Django admin | FIXED (code verified; Railway variables must be set before deploy) | `core/test_security.py` (37 in file, shared with M11) | [C1.md](C1.md) |
| H1 | High | Suspensions and bans bypass WebSockets | FIXED (initial and in-flight enforcement verified on PostgreSQL 16 + Redis; no deployment) | `core/test_websocket_suspension.py` (5 passed) | [H1.md](H1.md) |
| H2 | High | Club "invite" force-joins users without consent and ignores blocks | FIXED (pending invitation + acceptance; invitees need the app update to accept; UI not run on device; no deployment) | `clubs/test_invitations.py` (11), `notifications/test_push_targets.py` (1 changed), mobile `clubInvitations.test.ts` (2) | [H2.md](H2.md) |
| H3 | High | Authentication endpoints lack rate limits | FIXED (shared DRF scopes, configurable rates; no deployment) | `users/test_auth_throttles.py` (4 passed) + related users suite | [H3.md](H3.md) |
| H4 | High | TBD | UNREVIEWED | — | — |
| H5 | High | Polygon containment performs expensive Python geometry on frequent map polling | FIXED (exact latitude-band edge index, linear in size, built once per region; same answers; 24–30× faster at 1,500 vertices in the synthetic benchmark; production latency not measured; no deployment) | `regions/test_containment_index.py` (8), `manage.py bench_containment` | [H5.md](H5.md) |
| H6 | High | Hand-traced region rings have no vertex limit | FIXED (1,500 points per ring and in total, configurable; refused before parsing; stored geometry readable; pre-existing trace-view 500 fixed; no deployment) | `regions/test_trace_limits.py` (6) | [H6.md](H6.md) |
| H7 | High | TBD | UNREVIEWED | — | — |
| H8 | High | Chat sockets do not reconnect despite the UI label | FIXED (shared reconnect lifecycle, jitter/backoff, foreground cleanup and missed-history recovery; no deployment) | Mobile chat regressions (25); full mobile suite (888); PostgreSQL/Redis chat tests (17) | [H8.md](H8.md) |
| H9 | High | TBD | UNREVIEWED | — | — |
| H10 | High | TBD | UNREVIEWED | — | — |
| M1 | Medium | TBD | UNREVIEWED | — | — |
| M2 | Medium | TBD | UNREVIEWED | — | — |
| M3 | Medium | TBD | UNREVIEWED | — | — |
| M4 | Medium | DM text disappears or its failure removes an unrelated pending image | FIXED (optimistic keyed sends, REST ack, exact-message failure/retry, DB uniqueness; migration 0008 required; no deployment) | `friends/test_chat_reliability.py` (10, including PostgreSQL concurrency and real socket echo); mobile chat tests | [M4.md](M4.md) |
| M5 | Medium | TBD | UNREVIEWED | — | — |
| M6 | Medium | TBD | UNREVIEWED | — | — |
| M7 | Medium | TBD | UNREVIEWED | — | — |
| M8 | Medium | TBD | UNREVIEWED | — | — |
| M9 | Medium | TBD | UNREVIEWED | — | — |
| M10 | Medium | TBD | UNREVIEWED | — | — |
| M11 | Medium | Deployment security settings are missing and forwarded proxy headers are trusted too broadly | NEEDS EXTERNAL ACTION (code complete; verified `FORWARDED_ALLOW_IPS` and post-deploy results pending) | `core/test_security.py` (37 in file, shared with C1) | [M11.md](M11.md) |
| M12 | Medium | TBD | UNREVIEWED | — | — |
| M13 | Medium | TBD | UNREVIEWED | — | — |
| M14 | Medium | Club WebSocket write path skips validation performed by REST | FIXED (one serializer for REST + socket; `?before=` parsed; no deployment) | `clubs/test_message_validation.py` (5) | [M14.md](M14.md) |
| M15 | Medium | TBD | UNREVIEWED | — | — |
| M16 | Medium | DM history is unbounded and GET marks messages read | FIXED (bounded exclusive before cursor, explicit visible-message read ack, updated mobile; coordinated client release required) | Backend pagination/read tests; mobile history/hook/screen tests | [M16.md](M16.md) |
| M17 | Medium | Storage dependencies unpinned; no lock, unused gunicorn | FIXED (direct pins in `requirements.in`, full uv lock in `requirements.txt`, gunicorn removed, daphne kept for `runserver`; pip-audit ran: 49 advisories in 13 packages fixed by upgrading, now clean on PyPI and OSV; CI lock check and audit job added but not yet run on GitHub) | full backend suite (1696) on the upgraded lock; `.github/workflows/backend-ci.yml` | [M17.md](M17.md) |
| M18 | Medium | Map-query endpoints lack throttling | FIXED (shared configurable map budget; no deployment) | `proximity/test_throttles.py` (2 passed) + related proximity suite | [M18.md](M18.md) |
| M19 | Medium | Hi sends race uniqueness, spam and expose hidden users | FIXED (canonical visibility, retained decline, notify once; concurrency verified on PostgreSQL 16.15 + Redis; no deployment) | `friends/test_hi_abuse.py` (5 passed, no skips) | [M19.md](M19.md) |
| M20 | Medium | Only one uvicorn worker is configured | NEEDS EXTERNAL ACTION (code complete: `WEB_CONCURRENCY` workers, default 2, with Redis and connection-budget startup checks; Railway start-command override, `WEB_CONCURRENCY` and `DB_MAX_CONNECTIONS` unverified) | `core/test_capacity.py` (8), `core/test_security.py` (1 case adapted) | [M20.md](M20.md) |
| L1 | Low | TBD | UNREVIEWED | — | — |
| L2 | Low | Many modals do not trap screen-reader focus | FIXED (`accessibilityViewIsModal` on 15 modals in 14 files incl. TermsGate; labelled/added close paths so nothing traps; not verified with VoiceOver/TalkBack on a device) | mobile `modalAccessibility.test.ts` (35), `termsGate.test.tsx` (5) | [L2.md](L2.md) |
| L3 | Low | Module-level map callback registries are never unregistered | FIXED (register returns an identity-checked unregister; `useMapData` effects clean up) | mobile `nearbyRefresh.test.tsx` (3) | [L3.md](L3.md) |
| L4 | Low | AuthProvider value/functions recreated; fixed startup delay | FIXED (`useCallback`/`useMemo`; 2.5 s wait removed; bootstrap otherwise unchanged; cold start not measured on a device) | mobile `authContext.test.tsx` (7) | [L4.md](L4.md) |
| L5 | Low | map.tsx is excessively large | FIXED (4,385 → 432 lines; 8 files in `components/map`, code moved verbatim; no MapScreen render test; not run on a device) | existing suites (886 run), typecheck, lint | [L5.md](L5.md) |
| L6 | Low | Frontend BASE_URL is hardcoded to production | FIXED (`EXPO_PUBLIC_API_URL` per build, production default, production EAS profile pinned, release builds refuse http/local hosts; no EAS or device build run) | mobile `services/__tests__/apiUrl.test.ts` (21) | [L6.md](L6.md) |
| L7 | Low | Google client API keys are committed; restrictions need verification | NEEDS EXTERNAL ACTION (repo side verified: 3 client keys, no private credential in tree or history; Google Cloud restrictions unverified) | — | [L7.md](L7.md) |
| L8 | Low | Blocked-author subquery is duplicated in feed visibility query | FIXED (3 → 1 `friends_blockeduser` subquery in the feed gate; same rows; no deployment) | `swifts/test_visibility_block_subquery.py` (3) | [L8.md](L8.md) |

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
| MAP PERFORMANCE AND CAPACITY | H5, H6, M20, L8 | 2026-09-13 |
| DEPENDENCIES AND BUILD CONFIGURATION | M17, L6, L7 | 2026-09-14 |
| FRONTEND QUALITY | L2, L3, L4, L5 | 2026-09-14 |

**Open deployment action from SECURITY PERIMETER:** with `DEBUG=False`, the backend refuses to
start without `ALLOWED_HOSTS`, or with a wildcard in `CSRF_TRUSTED_ORIGINS`. The web process also
refuses to start without a valid `FORWARDED_ALLOW_IPS`, which must be the Railway edge range verified
with Railway. Set the variables listed in [C1.md](C1.md) and [M11.md](M11.md) *before* the next push
to `main`, then run the post-deploy checks in M11.md.

**Open deployment action from MAP PERFORMANCE AND CAPACITY:** the Procfile now starts
`WEB_CONCURRENCY` uvicorn workers (default 2), and with `DEBUG=False` the web process refuses more
than one worker without `REDIS_URL`. Before relying on any Procfile change, check the Railway start
command: `docs/scaling-railway.md` recorded an override that bypasses the Procfile. Then set
`WEB_CONCURRENCY` and `DB_MAX_CONNECTIONS` as described in [M20.md](M20.md).
