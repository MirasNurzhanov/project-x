# Baseline — Approx Codebase Review Remediation

Established: 2026-09-13. No application code was modified to produce this document.

Location of this file: `docs/codebase-review-fixes/` inside the `project-x` (backend) git
repository. Rationale: `project-x` is the only git repository that contains both a `docs/`
directory and a working Django project; `aura-mobile` is a separate repository (see below) and
the top-level `aura/` and `aura/backend/` directories are not git repositories at all. Findings
that are frontend-only will still be tracked from here; their `Files changed` sections will
reference paths under `aura-mobile/`.

## Repository layout

| Path | Is git repo? | Notes |
|---|---|---|
| `aura/` | No | Plain folder, not version controlled itself |
| `aura/backend/` | No | Contains `project-x/` (the real repo) plus scratch dirs (`.tmp/`, `.venv/`) |
| `aura/backend/project-x/` | **Yes** | Django backend. Remote: `https://github.com/MirasNurzhanov/project-x.git` |
| `aura/aura-mobile/` | **Yes** | Expo/React Native app. Remote: `https://github.com/MirasNurzhanov/aura-mobile.git` |

## Git status

### Backend (`project-x`)

```
On branch main
Your branch is up to date with 'origin/main'.
nothing to commit, working tree clean
```

Recent history (`git log --oneline -5`):
```
5eaf830 agents md file for better use of ai
03ad8ae deletion swift
1f04de3 swift update
44455f0 zone issue
fe68bc6 swifts change
```

### Frontend (`aura-mobile`)

Branch `main`, up to date with `origin/main`, but **working tree is dirty** with pre-existing,
uncommitted local changes that predate this review batch. These were left untouched — they are
not part of this remediation and were not authored by this process.

Modified (17 files): `app/(tabs)/map.tsx`, `app/(tabs)/profile.tsx`, `app/pinned.tsx`,
`app/swifts/analytics.tsx`, `components/DirectionsSheet.tsx`, `components/PinNoteEditor.tsx`,
`components/ReportSheet.tsx`, `components/ZonePlayer.tsx`,
`components/profile/SwiftProfileSection.tsx`,
`components/profile/__tests__/swiftCreator.test.tsx`, `components/profile/useCreatorSwifts.ts`,
`components/swifts/SwiftCommentsSheet.tsx`, `hooks/useTravelEstimates.ts`,
`hooks/useZonePlayback.ts`, `services/__tests__/directionsSheet.test.tsx`,
`services/__tests__/savedSwiftsLoading.test.tsx`, `services/__tests__/useTravelEstimates.test.tsx`

Untracked (4 files): `services/__tests__/pinNoteEditor.test.tsx`,
`services/__tests__/reportSheet.test.tsx`, `services/__tests__/swiftCommentsPagination.test.tsx`,
`services/__tests__/zonePlayerLifecycle.test.tsx`

Recent history (`git log --oneline -5`):
```
6f01a16 agents md file adding context and some background location bug fixing
00a0a77 Merge branch 'main' of https://github.com/MirasNurzhanov/aura-mobile
fb5e318 Swift layout, resolution change, deletion change etc
551b581 Rewrite the location purpose strings to match Ghost Mode
7fd1930 last updates
```

**Any future fix touching these 17 modified / 4 untracked files must diff against the working
tree as found, not against `origin/main`**, to avoid clobbering this in-progress work.

## Backend runtime / versions

- Python: **3.12.10** (project venv at `project-x/.venv`)
- Django: **5.2.14** (installed, matches `requirements.txt` pin)
- Key pinned dependencies (`requirements.txt`): `djangorestframework==3.17.1`,
  `djangorestframework_simplejwt==5.5.1`, `channels==4.3.2`, `channels_redis==4.3.0`,
  `daphne==4.2.1`, `uvicorn[standard]==0.46.0`, `psycopg[binary,pool]==3.3.4`,
  `redis[hiredis]==7.4.0`, `dj-database-url==3.1.2`, `django-storages[s3]`, `boto3`.
- ASGI server: `uvicorn` (prod, via Procfile) / `daphne` (installed, in `INSTALLED_APPS` for
  Channels' benefit).
- Deployment: Railway, via `Procfile`:
  - `release`: `python manage.py migrate && python manage.py seed_interests`
  - `web`: `python manage.py migrate && python manage.py fix_age_tiers && uvicorn core.asgi:application ...`
  - `worker`: `python manage.py moderate_swifts --loop`
- Two auxiliary Go services (`services/go-moderation`, `services/go-push`), deployed as separate
  Railway containers built from their own Dockerfiles (not part of this Python baseline).

## Frontend runtime / versions

- Node: **v24.14.0**, npm **11.9.0**
- Expo SDK: `~54.0.37`; `expo-router` `~6.0.24`
- React **19.1.0**, React Native **0.81.5**
- TypeScript: `~5.9.2` (devDependency)
- Test runner: `jest` `~29.7.0` with `jest-expo` `~54.0.18` preset
- Lint: `eslint` `^9.25.0` via `eslint-config-expo` `~10.0.0`, invoked as `expo lint`

## Database / cache assumptions

- **Local dev** (`docker-compose.yml`, explicitly documented in the file as dev-only, not a
  description of production): `postgres:16-alpine` + `redis:7-alpine`, both with healthchecks.
  Django container runs `python manage.py migrate` then `uvicorn` with `--reload`.
- **Production**: `DATABASE_URL` and `REDIS_URL` are read from the environment via
  `python-decouple` (`core/settings.py`) — Railway supplies these; not verifiable from the repo.
  `dj_database_url.parse(..., conn_max_age=CONN_MAX_AGE)` builds `DATABASES['default']`.
  `CACHES['default']` uses Redis when `REDIS_URL` is set, otherwise falls back (see below).
  `CHANNEL_LAYERS` uses `channels_redis` against `REDIS_URL`, defaulting to
  `redis://localhost:6379` if unset.
- **Test settings** (`core/test_settings.py`, used for `--settings=core.test_settings`):
  - `DATABASES`: SQLite, `:memory:`
  - `CACHES`: `django.core.cache.backends.locmem.LocMemCache` (no Redis)
  - `CHANNEL_LAYERS`: `channels.layers.InMemoryChannelLayer` (no Redis)
  - `STORAGES`: local `FileSystemStorage` (no R2/S3)
  - `PASSWORD_HASHERS`: MD5 only, for speed
  - `PUSH_DISABLED = True` (no outbound Expo push during tests)
  - No `.env` file exists in the repo (only `.env.example`); `core.settings` (the production
    module) cannot be imported standalone without a real environment — confirmed by reproducing
    `decouple.UndefinedValueError: SECRET_KEY not found` when invoking `manage.py` without
    `--settings=core.test_settings` and without a `.env`. **All local backend commands in this
    baseline therefore use `--settings=core.test_settings`**, which is the repository-appropriate
    equivalent for local/CI execution per the module's own docstring.

## Migration state

Apps with migrations present: `clubs`, `encounters`, `friends`, `interests`, `moderation`,
`notifications`, `recs`, `regions`, `stories`, `swifts`, `users`.

`python manage.py makemigrations --check --dry-run --settings=core.test_settings` → **exit 0**,
output: `No changes detected`. Models and migrations are in sync as of this baseline.

## Existing CI workflows

- **Backend (`project-x`)**: **no `.github/workflows` directory exists**. There is no CI
  currently running `manage.py test`, `check --deploy`, or `makemigrations --check` on this repo.
  The only workflow-adjacent file is `docker-compose.yml`, which is for local multi-service dev
  (Django + Postgres + Redis + the two Go services), not CI.
- **Frontend (`aura-mobile`)**: one workflow, `.github/workflows/react-doctor.yml` — runs
  `millionco/react-doctor@v2` on PRs and on push to `main`. It is **advisory only** (the
  `blocking` gate is commented out), posts a sticky PR comment and commit status, but **does not
  run `jest`, `tsc`, or `eslint`, and cannot fail a PR**. There is no workflow that runs the unit
  tests, type check, or lint for either repository today.

## Commands run and results

All commands below were executed against the current working trees exactly as found (backend:
`project-x/.venv` activated; frontend: `aura-mobile`, `npm` deps already installed in
`node_modules`). Exit codes were verified directly (not through a `| tail` pipe, which would mask
the true exit code of the piped-from command) by redirecting to a file and checking `$?`
immediately after.

### Backend

| Command | Result | Notes |
|---|---|---|
| `python manage.py test --settings=core.test_settings` | **FAIL** (exit 1) | 1586 tests, **1 failure**, 1 skipped. See below. |
| `python manage.py check --deploy --settings=core.test_settings` | PASS (exit 0) | 6 warnings, 0 errors. See below. |
| `python manage.py makemigrations --check --dry-run --settings=core.test_settings` | PASS (exit 0) | `No changes detected`. |

**Test failure detail:**

```
FAIL: test_ranked_feed_costs_one_ranking_pass_not_one_per_swipe
  (core.test_query_budgets.QueryBudgetTests.test_ranked_feed_costs_one_ranking_pass_not_one_per_swipe)
The personalised path has a different budget, and it is honest.
----------------------------------------------------------------------
AssertionError: 9 not less than or equal to 8 : a continued page ran 9 queries, budget is 8
— the session cache is what keeps this cheap, so this failing means every swipe is now re-ranking
```

This is a pre-existing query-budget regression in the ranked-feed pagination path
(`core/test_query_budgets.py`), present on `main` before any remediation work in this batch. It is
not caused by, and was not touched by, this baseline pass. It is recorded here as-is per
instructions ("do not modify application code" during BASELINE); it should be triaged against the
review's findings once their descriptions are available (see "Open gap" below) — if the review
already covers a ranked-feed/query-budget finding, this failing test is corroborating evidence for
it and the budget must **not** be relaxed to make it pass (per testing rules: never raise query
budgets just to make a test green).

**`check --deploy` warnings (6):** `security.W004` (no `SECURE_HSTS_SECONDS`), `security.W008`
(`SECURE_SSL_REDIRECT` not `True`), `security.W009` (short/insecure `SECRET_KEY`),
`security.W012` (`SESSION_COOKIE_SECURE` not `True`), `security.W016` (`CSRF_COOKIE_SECURE` not
`True`), `security.W018` (`DEBUG` is `True`).

Of these, **W009 and W018 are artifacts of `test_settings.py`** (it deliberately sets
`SECRET_KEY='test-secret-key'` and `DEBUG=True` for local/CI runs — see file docstring) and are
expected to differ in the real production environment, which is not reproducible from this repo
(Railway env vars for `SECRET_KEY`/`DEBUG` were not available to inspect).

**W004, W008, W012, and W016 are not settings-module artifacts** — a direct grep of
`core/settings.py` confirms `SECURE_HSTS_SECONDS`, `SECURE_SSL_REDIRECT`, `SESSION_COOKIE_SECURE`,
and `CSRF_COOKIE_SECURE` are **never set anywhere in the shared settings module**, for any
environment. Only `SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')` is set. This
means these four warnings would very likely also fire against the real production configuration
unless Railway or a reverse proxy is independently enforcing HTTPS/secure cookies at the platform
level (not verifiable from this repo). This is a baseline observation, not a fix in this batch —
flagging it in case it maps to one of the review's `H`/`M` findings.

### Frontend

| Command | Result | Notes |
|---|---|---|
| `npm test` (`jest --ci`) | **PASS** (exit 0) | 53 suites, **786 tests**, all passed. |
| `npx tsc --noEmit` | **FAIL** (exit 2) | 26 errors, confined to 2 test files (see below). |
| `npm run lint` (`expo lint`) | PASS (exit 0) | 0 errors, 3 warnings (unused vars). |

**`tsc --noEmit` failure detail:** all 26 errors are in test files, not application source:

- `services/__tests__/swiftLocationPrivacy.test.tsx` — 9 errors: `ReactTestInstance` has no
  `findAll` (should likely be `findAllByType`/`findAllByProps` or an updated
  `react-test-renderer` type), several implicit-`any` callback parameters, one unsound type
  assertion (`Swift` → `Record<string, unknown>`).
- `services/__tests__/swiftsFeedPlayback.test.tsx` — 17 errors: same `findAll` issue, implicit-`any`
  parameters, and one untyped generic function call.

No errors were reported in any non-test file. This is recorded as a baseline fact; whether it
corresponds to one of the review's findings is unknown until finding descriptions are available.

**Lint warnings (3, non-blocking):** `app/(tabs)/settings.tsx:542` unused `setSaving`;
`app/_layout.tsx:12` unused `useState` import; `app/_layout.tsx:13` unused `Alert` import.

## Comparison with the review's claims

The findings' actual content (titles, severities, descriptions, proposed fixes) for IDs `C1`,
`H1`–`H10`, `M1`–`M20`, `L1`–`L8` were **not included in the batch instructions** — only the ID
scheme itself was given, to be initialized as `UNREVIEWED` in `INDEX.md`. Two review-shaped
documents exist in scratch directories (`backend/.tmp/review-pdf-text.txt`, an App Store
submission review dated 2026-09-07, and `reports/approx-change-report.tex`, a narrative change
report), but neither is structured as an ID-tagged findings list matching `C1`/`H*`/`M*`/`L*`, so
neither was used to backfill descriptions. **The current repository state above is treated as
authoritative**; per-finding comparison against the review's claims will happen once each
finding's actual text is provided in a later batch.

## What this baseline does NOT establish

- Production environment values (Railway `SECRET_KEY`, `DEBUG`, `REDIS_URL`, `DATABASE_URL`,
  Cloudflare credentials) — not accessible from this repo.
- Whether CI runs these checks anywhere outside this repo (e.g., a Railway build hook) —
  no evidence of one was found in-repo.
- Content/mapping of the `C1`/`H1-H10`/`M1-M20`/`L1-L8` findings — pending.
