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
