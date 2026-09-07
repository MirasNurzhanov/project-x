# Approx: Railway capacity plan and staging test

Prepared 7 September 2026. Target: **one million concurrently active users**, not
one million registrations. Current supported concurrency remains **unmeasured**.
These changes reduce avoidable work; they do not establish million-user capacity.
No infrastructure purchases, production changes or live load tests were made.

## What changed, and what remains

- Navigation badges use one small authenticated `/api/notifications/badges/`
  response. The updated mobile client shares one foreground poll across mounted
  navigation components, approximately every 30 seconds. Old clients still issue
  their old requests until users install the update.
- The map assembles its sections on the request's database connection. It no
  longer fans one authenticated request into three database worker threads.
  This reduces connection contention; compare end-to-end map latency under load
  because parallel execution sometimes helped individual requests.
- Interest filtering uses existence checks instead of multiplying and deduplicating
  user rows. An unread-notification index supports the compact badge counts.
- Swift feed visibility checks friendships, accepted Hi requests and blocks in
  SQL when the rows are fetched, instead of repeatedly loading relationship ID
  sets first. Moderation and audience restrictions remain enforced, including
  after a relationship is revoked during a cached recommendation session.
- Location already saves selected columns with `update_fields`. The remaining
  concern is the frequency and downstream work of those updates, not an alleged
  full-row overwrite fixed by this patch. Notification, encounter and region work
  still need profiling with populated data.

The read-only Railway inspection found the API start override:

```sh
sh -c 'python manage.py migrate && uvicorn core.asgi:application --host 0.0.0.0 --port 8000'
```

A separate moderation worker exists. Both API and moderation deployments reported
one replica in `europe-west4-drams3a`. The account plan, effective CPU/RAM limits,
`WEB_CONCURRENCY`, database connection limit and achieved throughput were not
established. Absence of `--workers` alone does not prove one worker: Uvicorn can
read `WEB_CONCURRENCY`. Editing the repository's Procfile does not change an
existing Railway start-command override.

Local verification for this change:

- 216 notification/proximity tests and 476 Swift/recommendation/query-budget
  tests passed using SQLite and mocked external services. The existing ranked
  feed query-budget failure also occurred on pristine backend commit `36c9621`;
  it passes with the visibility-query optimization, without raising the budget.
- Eight offline load-harness helper tests passed. Locust execution and live
  capacity testing have not been performed.
- After the mobile repository moved to SDK 54, the shared polling and navigation
  suites passed all 15 tests. The full mobile typecheck still reports errors in
  other files (including SDK tab API incompatibilities and existing test types).
  These need resolution before treating the whole mobile build as release-ready.
- Django reported no missing model migrations. The PostgreSQL concurrent index
  build still needs validation in staging.

## Capacity arithmetic

Use measured client cohorts rather than multiplying registered users by a made-up
average. The foreground map hook currently runs map retrieval **and location
reporting every 10 seconds** while sharing is enabled. It remains mounted across
tabs. The separate background location task has a nominal 30-second period; iOS
scheduling does not guarantee that exact delivery cadence.

| Foreground sharing cohort | Requests per user per second | At 1,000,000 users |
| --- | ---: | ---: |
| Map retrieval, every 10 seconds | 0.1000 | 100,000/s |
| Location update, every 10 seconds | 0.1000 | 100,000/s |
| Combined badges, nominally every 30 seconds | 0.0333 | 33,333/s |
| **Baseline total** | **0.2333** | **233,333/s** |
| Optional chat polling: three requests per 15 seconds | +0.2000 | +200,000/s |

The optional chat profile intentionally keeps map polling active. It is a stress
cohort, not a claim that every user visits chat simultaneously. Add login/token
refresh, screen-opening requests, messages, uploads and WebSocket reconnects
separately. If `F` is the number of foreground map sharers, `B` background sharers,
and `C` foreground clients also polling chat, the nominal base API demand is:

```text
requests/s ~= F * (1/10 + 1/10 + 1/30) + B/30 + C * (3/15)
```

Set `B=0` for the supplied foreground harness. A user must not be counted in both
`F` and `B`. With one million sustained foreground sharers, location alone is
8.64 billion update attempts/day. A 10 KB average response at 233,333 requests/s
is approximately 2.33 GB/s before transport overhead; measure actual response
sizes by endpoint. These are demand illustrations, not a forecast or proof that
Railway's current service can carry this load.

## Run the staging harness

1. Create an isolated staging environment with its own database and Redis, the
   updated backend, and realistic synthetic records. Disable external push/email
   delivery and other paid side effects there. The test updates locations and can
   create encounters and notifications. Do not point staging at production data.
2. Pre-create a different synthetic `.invalid` account for every simultaneous
   virtual user. Give them valid credentials, appropriate account/consent state,
   sharing enabled, seeded coordinates and a realistic distribution of ages,
   interests, friendships, blocks, unread notifications/messages, clubs and
   stories. A mostly empty database would understate the cost of this app.
3. Put credentials in a private CSV outside source control. Coordinates should
   agree with the users' initial seeded locations; distribute them among both
   crowded and sparse regions. Example format (placeholder credentials only):

   ```csv
   email,password,latitude,longitude
   load-000001@example.invalid,replace-with-staging-secret,25.2048,55.2708
   load-000002@example.invalid,replace-with-staging-secret,25.2050,55.2710
   ```

4. Install Locust into a dedicated test environment. Run the following **from
   `project-x`**, substituting the staging origin and private CSV path. This is a
   proposed 10-user smoke test, not a command already executed:

   ```powershell
   python -m venv loadtest/.venv
   .\loadtest\.venv\Scripts\python.exe -m pip install 'locust>=2.31,<3'
   $env:APPROX_LOAD_STAGING_ORIGIN = 'https://your-staging-api.example.com'
   $env:APPROX_LOAD_ALLOW_WRITES = 'staging-only'
   $env:APPROX_LOAD_ACCOUNTS = 'C:\private\approx-staging-accounts.csv'
   $env:APPROX_LOAD_CHAT = '0'
   New-Item -ItemType Directory -Path 'loadtest/results' -Force | Out-Null
   .\loadtest\.venv\Scripts\python.exe -m locust -f loadtest/locustfile.py --host $env:APPROX_LOAD_STAGING_ORIGIN --headless --users 10 --spawn-rate 1 --run-time 5m --reset-stats --csv loadtest/results/smoke
   ```

   Record the installed Locust version with the results. Use `APPROX_LOAD_CHAT=1`
   for the separate map-plus-chat stress run. The harness refuses a different host,
   missing write opt-in, duplicate account addresses or exhausted account pool.
   This is an operator acknowledgement of staging, not an automatic audit of the
   server's database configuration. API redirects are disabled.

5. Check every sampled response against the expected fixture: visible map users,
   block exclusions, friend precision/non-friend approximation, correct badge
   counts and persisted location updates. Invalid JSON, authentication errors,
   HTTP 4xx and HTTP 5xx count as failures; the old blanket acceptance of club
   join errors has been removed along with that unrelated write workload.

Run the offline harness tests without Locust or network access:

```sh
python -m unittest loadtest.test_scenario -v
```

Each virtual user uses independent 10/10/30-second clocks with randomized initial
phases and at most one in-flight request. Slow responses skip missed polling slots
instead of queuing catch-up bursts. Logs report skipped slots per generator. This
is a **closed-loop** workload: when either the API or generator stalls, achieved
request rate falls. A high virtual-user count with low achieved throughput or many
skipped slots is not a capacity pass. Use a separately controlled open-loop arrival
test to quantify saturation and queueing before making large-scale claims.

Access-token expiry schedules refresh one minute early; rotated refresh tokens
replace the old token. Authentication failures stop the affected simulated user
and remain visible in Locust results. Keep generator clocks synchronized. Separate
the login ramp from steady-state statistics and retain a distinct login-storm test.

For distributed Locust, provide the same ordered CSV on every worker and assign
each worker a **unique** `APPROX_LOAD_SHARD_INDEX` from `0` to `N-1`, with
`APPROX_LOAD_SHARD_COUNT=N`. Every worker must use that same `N` and CSV, and receive
the explicit `--host`, staging-origin and write-opt-in settings. The master needs
the origin/write opt-in but no account CSV. Use `--expect-workers N`, a fixed
worker set, and enough accounts per shard for the largest allocation. Never run
two workers with the same shard index or overlap accounts with another test run.
Automatic `--processes` is rejected because copying one shard across processes
would reuse accounts. Sharding is validated within each file/process; verify the
orchestration assigns distinct indices across hosts. Do not change shard counts
or autoscale generators during a run. Check generator CPU, memory and networking
independently from the application.

## Measurement gates and deployment order

1. **Backend first.** Deploy the badge endpoint and index to staging, verify map
   privacy tests and inspect the PostgreSQL index build. Then test the mobile
   build against it. Keep old badge endpoints during mobile adoption. Release
   backend before mobile so new clients do not poll an unavailable endpoint.
   Migration `notifications.0008_notification_unread_idx` builds its partial
   index concurrently on PostgreSQL, outside a transaction. Run it once through
   the controlled migration step; do not let multiple replicas race to build it.
   Verify the index is valid after completion. An interrupted concurrent build
   can leave an invalid index that needs inspection before a migration retry.
   SQLite migration tests do not validate PostgreSQL build duration or locking.
2. **Baseline.** Use identical resource limits and fixture snapshots for before/
   after runs. Start at 10 users, then 100, 500, 1,000, increasing only while the
   previous stage meets the agreed targets. Include at least 10 minutes of steady
   load after ramp-up and a longer soak once token refresh will occur. These are
   test stages, not supported user counts.
3. **Record.** Store commit/build, account/dataset size, cohort, achieved RPS,
   p50/p95/p99 latency by route, status/error rate, response bytes, skipped poll
   counts and actual active users. Record per-replica CPU/RAM, pool wait time,
   database CPU/I/O, active/idle connections, slow queries, lock waits, dead tuple
   growth, Redis memory/eviction and moderation queue age.
4. **Gate.** Agree endpoint-specific targets before running. Suggested initial
   targets are p95 below 500 ms and request failures below 1%, with no sustained
   queue growth or memory growth; these are targets, not observed results. Include
   throttled requests in the result and investigate them instead of hiding them.
   Stop escalating at sustained overload, pool timeouts or a growing backlog.
5. **Scale one constraint at a time.** Re-run after a query, worker or replica
   change. Include rolling deployment/reconnection and one-replica loss tests.
   A healthy small test cannot be linearly extrapolated to one million users.

Railway's [load-test guide](https://docs.railway.com/guides/load-test-k6) describes
measuring latency/error rates separately from service resource metrics. A private
network test isolates application performance; public-edge measurements include
the actual client path. Load generators and network traffic can incur charges, so
set a run duration, stopping condition and budget before executing large tests.

## Railway process and database budget

Confirm the actual service plan, per-replica limits, process count and database
configuration in Railway before changing them. Railway supports adding API
replicas, but the resources and limits remain plan-dependent. See
[Railway scaling](https://docs.railway.com/deployments/scaling).

Before adding replicas, move schema migration execution out of each API replica's
start command into one controlled migration/pre-deploy step. Keep API startup
focused on serving requests. Verify this against Railway's active override,
including the listening port and healthcheck; it is not changed by this runbook.

For direct PostgreSQL connections, budget maximum pools across **all** processes:

```text
API replicas * API worker processes per replica * DB_POOL_MAX_SIZE
  + moderation/other worker pools
  + migration/admin/maintenance reserve
  <= PostgreSQL max_connections minus PostgreSQL reserved connections
```

For example, three replicas with two processes and 12 connections each can open
72 API connections before worker/admin connections. This is arithmetic, not a
recommended deployment size. Keep `DB_POOL_MIN_SIZE` in the idle-connection budget
too. Raising every pool and adding replicas together can overload the database.

If measurements justify it, evaluate Railway's
[PgBouncer connection pooling](https://docs.railway.com/guides/connection-pooling-pgbouncer)
in staging. Railway exposes pooled and unpooled connection URLs; migrations and
operations requiring a stable session should use the direct/unpooled connection.
Transaction pooling must be checked against Django cursor behavior, prepared
statements and this project's startup `statement_timeout` options. Do not enable
it by merely replacing a URL. Budget PgBouncer's server-side pools across every
pooler replica and database/user pair. Pooling reduces connection overhead; it
does not make expensive SQL or unlimited writes cheap.

## Architecture still required for a credible million-user target

| Area | Next work, subject to measurement |
| --- | --- |
| Location ingestion | Separate frequently changing presence/location state from account/profile rows; coalesce updates, handle out-of-order samples, and move durable encounter/notification work into bounded queues. Preserve consent revocation and deletion semantics. |
| Nearby discovery | Evaluate PostGIS spatial indexes and query plans with real geographic density, then geographic partitioning and bounded candidate selection. Preserve precise friend locations, approximate non-friend results, age boundaries and block checks. Do not share personalized map caches without viewer-aware authorization. |
| Counters and live events | Maintain event-driven unread counters and subscription updates, with idempotency, reconciliation and bounded fallback polling. Account for connection count, fan-out, reconnect storms and multi-replica delivery. |
| Database/Redis | Establish capacity, retention and backup/restore targets; index hot predicates; partition large event/message tables where measured. Plan replicas/HA and failover, without sending authorization-sensitive reads to lagging replicas blindly. |
| Push/moderation | Use a durable bounded dispatcher and independent workers with retries, rate limits and queue-age metrics. Measure Cloudflare quotas and cost separately; no unmoderated video should become public because a queue is overloaded. |
| Media | Deliver video/images through the media CDN/object store, separate from Python API workers. Load-test playback/cache behavior and upload/moderation processing as separate workloads. |
| Operations | Track cost per active user and per feature, regional failure recovery, abuse controls, load shedding, data retention and rollout/rollback. Agree the deployment plan and account limits with Railway before scheduling million-client tests. |

The next defensible milestone is a measured staging envelope with a documented
dataset and latency/error targets. A million-user claim requires representative
distributed tests and the architecture changes those measurements justify.
