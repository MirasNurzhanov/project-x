# go-push

A push-notification dispatcher for the Approx Django backend, in Go.

> Sibling to [`../go-moderation`](../go-moderation/README.md). The two make the
> same architectural bet from opposite ends: this one owns its queue in memory
> and is therefore the one that loses work on a hard kill, while go-moderation
> keeps its durability in Postgres. The contrast is discussed in the trade-offs
> below and is the main reason go-moderation was the better second choice.

Django decides **who** gets a notification. This decides **how fast the bytes
reach Expo**.

```
                    ┌───────────────────────────────────────────────┐
  Django            │  approx-push (Go)                             │
  notifications/    │                                               │
  push.py           │   POST /v1/push                               │
      │             │        │                                      │
      │  HTTP       │        ▼                                      │
      └────────────▶│   validate ──▶ buffered channel (queue)       │
                    │   202 back          │                         │
                    │   immediately  ┌────┴────┬────────┐           │
                    │                ▼         ▼        ▼           │
                    │             worker    worker   worker  (goroutines)
                    │                └────┬────┴────────┘           │
                    │                     ▼                         │
                    │             token-bucket rate limit           │
                    │                     ▼                         │
                    └─────────────── Expo, 100 messages/request ────┘
```

## Why this workload

`notifications/push.py::_queue` currently does
`threading.Thread(target=_post, daemon=True).start()` per batch. That is fine
at your current volume, and its docstring is honest about the trade. What it
cannot do:

| | thread-per-batch | approx-push |
|---|---|---|
| Bound concurrency | no ceiling | fixed worker pool |
| Rate-limit Expo | no | token bucket |
| Retry transient failures | no (`_post` swallows) | 3 attempts, backoff |
| Shed load | no — grows until OOM | 503 + `Retry-After` |
| Survive worker recycling | tied to the gunicorn worker | separate process |

## Running it

```bash
cd services/go-push
go run .
```

No environment needed — every setting has a working default. Then:

```bash
curl -X POST localhost:8080/v1/push -H 'Content-Type: application/json' -d '{"messages":[{"to":"ExponentPushToken[xxx]","title":"hi","body":"there"}]}'
```

```bash
go test ./... -race -v
```

The `-race` flag is worth internalising: it instruments the binary to detect
concurrent unsynchronised access at runtime. There is no equivalent for Python
threads, and it is one of the strongest arguments for writing concurrent code
in Go rather than in a language where races are found by staring.

## Wiring it to Django

Two settings, both blank by default so nothing changes until you opt in:

```bash
PUSH_DISPATCHER_URL=http://127.0.0.1:8080/v1/push
PUSH_DISPATCHER_TOKEN=<same value as the service's PUSH_AUTH_TOKEN>
```

`push._queue` then tries the dispatcher and **falls back to the existing
thread** if it is unreachable or shedding load. That fallback is what makes
this safe to switch on: the worst case is exactly today's behaviour.

### Why HTTP and not Redis or a shared database

You have Redis (channels + cache) and Postgres, so all three were available.
HTTP wins here for one reason: **this queue does not need to be durable**, and
the other two options only pay off if it does.

A push announces something already saved — `notifications.utils` has written
the `Notification` row before any of this runs. Losing one costs a missed
banner, not lost data. So the simplest transport that gives backpressure is the
right one, and HTTP gives it for free: a 503 is a queue-full signal Django can
read, where a Redis `LPUSH` always succeeds and quietly grows.

Reach for Redis the day pushes become something you must not lose — see
Trade-offs.

### The dead-token loop, not implemented

Expo reports `DeviceNotRegistered` for uninstalled apps. `ExpoClient.OnDeadToken`
is the hook; nothing is wired to it. The natural completion is a small callback
POSTing to Django to delete the `users.Device` row. Left undone deliberately —
it needs an authenticated inbound endpoint on Django, which is a second
security surface, and it is a good first change to make yourself.

## Deploying it

```bash
docker build -t approx-push:0.1.0 .
kubectl apply -f k8s/
```

### The artifact, versus the Django one

| | Django | approx-push |
|---|---|---|
| Image contents | Python, pip, ~40 wheels, source | one binary + CA bundle |
| Image size | ~150–400 MB | ~12 MB |
| Base image | `python:3.12-slim` (a distro) | `scratch` (empty) |
| Start to serving | 5–15 s (import graph) | milliseconds |
| Memory floor | 256–512 Mi | 64 Mi |
| DB connections | pool per worker | zero |
| Needs a `startupProbe` | yes | no |
| Cross-compile | no — build on the target arch | `GOOS=linux GOARCH=arm64 go build` |

Two of those deserve elaboration because they change how you operate the thing:

**`CGO_ENABLED=0` is what makes the rest true.** With cgo on, Go's DNS resolver
links against the system libc and the binary will not run on `scratch`. That
one flag is the difference between a 12 MB image and needing a distro under it.

**`scratch` has no CA bundle**, so HTTPS to Expo fails with `x509: certificate
signed by unknown authority` — after working perfectly on your laptop. The
Dockerfile copies `ca-certificates.crt` out of the build stage. This is the
single most common way a first Go container fails.

**Replica count is not bounded by Postgres.** Scaling Django out claims more
connections; scale far enough and you exhaust the limit and make the incident
worse. This service opens none, so the HPA in `k8s/service.yaml` is genuinely
elastic in a way a Django deployment's is not.

---

## Trade-offs: Go here, Django there

The honest version, including the case against.

### Keep it in Django when

- **It touches the models.** Anything reading `User`, `Club`, `SwiftVideo`, or
  the visibility rules in `users/visibility.py` should stay. Reimplementing
  `watchable_q` in Go would mean two copies of your privacy rules in two
  languages, and the second one will drift. That is not a performance question;
  it is the reason this service knows nothing about your domain.
- **The work is rare or small.** A management command, a nightly sweep, an
  admin action. Go buys nothing and costs you a second toolchain.
- **You are I/O-bound on Postgres.** Go will not make your query faster. If the
  bottleneck is an N+1 or a missing index — the thing `core/test_query_budgets.py`
  exists to catch — Go moves the same slow query to a different language.
- **It needs the ORM, the admin, or DRF.** Rebuilding those is a project, not a
  service.

### Move it to Go when

- **Concurrency is the workload**, not an implementation detail. Fan-out to
  thousands of connections, rate-limited outbound calls, long-lived
  connections. This service is one goroutine per job at ~2 KB of stack against
  one Python thread per batch at ~1 MB — three orders of magnitude, and it is
  why the queue can be bounded and measured rather than merely started.
- **Latency has a hard ceiling** — a webhook receiver that must answer in
  milliseconds regardless of what the database is doing.
- **The deployment shape matters**: a sidecar, an edge worker, something that
  must start in milliseconds or fit in tens of MB.
- **You want races caught by a tool.** `go test -race` has no Python equivalent.

### The case against doing this at all

At your current volume this is not needed. The daemon thread works; the
failures it cannot handle are ones whose cost is a missed banner. What you are
buying is a second language in the build, a second deployment, a second thing
to page someone about — and a network hop that can fail, which is why
`push_dispatcher.send` has a fallback.

The honest reason to build it now is that **it is a genuinely good first Go
service**: small, real, and at a boundary where the split is defensible on its
own merits rather than manufactured for the exercise. Do it to learn Go on
something that matters, not because push is slow.

If you want the second one, the stronger candidate in your codebase is the
**Swift AI moderation sweeper** (`swifts/ai_moderation.py`). It already has a
durable job table, an in-process daemon thread doing the work, and a management
command that must be run for correctness. A Go consumer of that table would be
a better fit than this one — the durability problem is already solved by the
`SwiftModerationJob` rows, and the workload (N concurrent Cloudflare calls per
video, rate-limited, retried) is exactly what goroutines are for.

### Where this design is weakest

**The queue is in memory.** A crash, an OOM kill, or a node failure loses it.
Graceful shutdown covers the common case (a deploy), which is why
`terminationGracePeriodSeconds` must exceed `PUSH_DRAIN_WAIT_S` — but there is
no coverage for a SIGKILL.

That is deliberate and it is the same durability you have today. The moment
pushes must not be lost, the fix is not a bigger buffer: it is moving the queue
to Redis (`LPUSH`/`BRPOPLPUSH`) or to a Postgres job table, and turning this
service into a consumer instead of the owner. The worker pool, the rate
limiter, the retry logic and the shutdown path all survive that change
unmodified — only `Enqueue` and the source of `Job` values move. It is worth
seeing that the concurrency design and the durability decision are independent,
because conflating them is how people conclude they need Kafka.
