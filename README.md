# deploy
# redeploy
# trigger

## Performance

Two tools, both run against the in-memory SQLite of `core.test_settings` so
that what they report is the application's own cost rather than the database's:

```
python manage.py test core.perf_probe   --settings=core.test_settings -v2   # queries
python manage.py test core.perf_profile --settings=core.test_settings        # CPU
```

`perf_probe` prints query count, wall time and payload size per endpoint;
`core/test_query_budgets.py` turns its findings into ceilings the suite
enforces. `perf_profile` prints where the remaining milliseconds go, which is
the question that matters once the query counts are already low.

### Libraries chosen for speed

- **orjson** encodes every API response, wired in through
  `REST_FRAMEWORK['DEFAULT_RENDERER_CLASSES']`. `core/jsonio.py` explains how
  it is pinned to DRF's exact output and `core/test_json.py` asserts the two
  are byte-identical on real payloads — including the U+2028 escape and
  `Decimal` rendering as a number, which is what the map's coordinates rely on.
- **psycopg 3** (`psycopg`, not `psycopg2`) with its connection pool. Django
  selects it from the same engine name; installing it is the whole switch.
- **redis[hiredis]** gives redis-py a C protocol parser. No configuration:
  redis-py uses it when it is importable, and both the channel layer and the
  cache go through it.
- **uvicorn[standard]** serves HTTP and websockets in place of daphne. Measured
  on this app's 401 path — server plus Django cycle, no database — uvicorn ran
  ~196 req/s against daphne's ~171, median 40ms against 47ms, over three
  interleaved runs with `DEBUG=False`. That was on Windows, where the
  `standard` extra cannot install uvloop; a Linux dyno also gets uvloop.

  Rollback is one line: put `daphne -b 0.0.0.0 -p $PORT core.asgi:application`
  back in the `Procfile`. `daphne` stays installed and in `INSTALLED_APPS`,
  where it also backs `runserver`.

### Connection pooling

`DB_POOL` (default on) puts a psycopg pool in front of Postgres and forces
`CONN_MAX_AGE` to 0, which Django requires alongside a pool.

This is not a tuning preference. Django runs each sync view inside asgiref's
`ThreadSensitiveContext` and tears its executor down at the end of the request,
so a persistent connection pinned to a thread does not survive the thread —
`CONN_MAX_AGE` was buying much less than its value suggested. A pool belongs to
the process instead, so connections outlive requests for real.

| variable | default | |
|---|---|---|
| `DB_POOL` | `True` | set `False` to fall back to `CONN_MAX_AGE` |
| `DB_POOL_MIN_SIZE` | `2` | connections opened at startup |
| `DB_POOL_MAX_SIZE` | `12` | ceiling **per process** — this is the number that has to fit the provider's connection limit |
| `DB_POOL_TIMEOUT_S` | `10` | how long a request waits for a free connection |
| `DB_SERVER_SIDE_BINDING` | `False` | send parameters separately from the SQL — cheaper per query, but changes parameter type inference |
| `DB_PREPARE_THRESHOLD` | `0` (off) | executions before Postgres prepares and reuses a plan. Django disables preparation by default because it does not survive a transaction-mode pooler like PgBouncer; psycopg's own pool above is session-level and does not have that problem |

The last two are off because neither can be verified without a Postgres to
point at, and both fail as errors rather than as slowness. Enable them one at a
time in staging.

Migrations deliberately run without the pool, and without the statement
timeout: one long-lived connection doing one thing needs neither.

Confirm the effect against `pg_stat_activity` after deploying — connection
count should stop tracking request volume and settle between the two sizes
above.

## AI moderation for Swift video

A Swift is not watchable by anybody but its author until its media has been
screened. The pipeline is:

```
POST /api/swifts/  ->  moderation=PENDING (hidden everywhere, in the same INSERT)
                   ->  frames out of Cloudflare Media Transformations
                   ->  each frame through a Workers AI vision model
                   ->  caption + overlay through the existing moderation/text.py
                   ->  (optional) audio track -> Whisper -> the same text filter
                   ->  APPROVED  -> moderation=OK, now in feeds
                       QUARANTINED -> stays hidden, SwiftReport(source=ai_video)
                       FAILED    -> stays PENDING, job retries, then quarantines
```

Nothing in the pipeline can remove content or touch an account. Permanent
takedowns are a human at `/admin/`, and the AI's item lands in the same
`SwiftReport` queue with the same four actions as a report a person filed.

| module | what it is |
|---|---|
| `moderation/cloudflare_ai.py` | the only code that talks to Cloudflare. No models, no Swift knowledge |
| `moderation/policy.py` | how frame results become one decision. Pure functions |
| `moderation/checks.py` | startup warnings for a misconfiguration |
| `swifts/ai_moderation.py` | the orchestration — the only place the two meet a row |
| `swifts/models.py::SwiftModerationJob` | the durable job, and the evidence a moderator reads |

### Running the worker

`ai_moderation.enqueue` starts a daemon thread on commit, which finishes the
common case in seconds. That thread is **latency, not durability**: a deploy or
a crash between the commit and the thread finishing leaves the job `QUEUED` and
the Swift `PENDING` — safe, because nobody can see it, but permanent unless
something comes back for it.

That something is:

```
python manage.py moderate_swifts --loop      # a worker service
python manage.py moderate_swifts             # or one pass, on a schedule
python manage.py moderate_swifts --status    # what is configured. Prints no secrets
```

A deployment that never runs it has a feature that mostly works and silently
strands Swifts whenever a dyno restarts at the wrong moment. Both forms are
safe to run alongside the web process and each other — claiming a job is a
conditional UPDATE, so two sweepers racing produce one winner and one no-op.

Failed jobs are not retried by a normal sweep. After fixing the configuration,
retry specific **Swift IDs** with:

```
python manage.py moderate_swifts --retry-failed 22 23
```

This runs only those eligible failed jobs and then exits. It resets the attempt
budget and moves each upload back to `pending`; the video remains hidden until
moderation returns `ok`. Deleted videos, human-reviewed cases, independent
reports and content rejected by the classifier are skipped. The command prints
how many IDs were skipped and each processed job's resulting state.

Public Swift feeds, recommendations and interactions require
`moderation="ok"`. Pending, quarantined, flagged and unrecognized states are
hidden. Reporting an approved Swift flags it and withholds it until review.
Creators can still check their uploads and review status through their own
detail and collection endpoints.

### Cloudflare configuration this needs

1. **Workers AI** — an API token with the Workers AI permission, and the
   account id. `CLOUDFLARE_API_TOKEN` / `CLOUDFLARE_ACCOUNT_ID`, server-side
   only.
2. **Media Transformations, enabled on the zone that serves the R2 bucket.**
   This is the one that is easy to get wrong: transformations are enabled *per
   zone*, and the default `pub-*.r2.dev` address is Cloudflare's hostname, not
   one of yours — there is nowhere to switch them on. The bucket has to be
   published on a custom domain in the account, and `R2_PUBLIC_URL` (or
   `CLOUDFLARE_MEDIA_TRANSFORM_BASE`) pointed at it. `moderation.W003` warns
   about exactly this at startup.
3. **Allowed origins** for transformations must include the bucket's host if it
   differs from the zone serving `/cdn-cgi/media/`.

`SWIFT_AI_MODERATION_ENABLED` defaults to **off**, and with it off a Swift
publishes exactly as it did before this feature existed. Turn it on only once
1 and 2 are done: otherwise every new Swift is held invisible until the sweeper
gives up and quarantines it for a human.

Cloudflare Stream is **not** used. The videos are already in R2 and Media
Transformations reads them there, so there is no encoding pipeline to adopt and
no processing-complete webhook to verify.
