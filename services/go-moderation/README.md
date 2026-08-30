# go-moderation

Classifies Swift video frames for the Approx Django backend.

Django leases it a claimed job; it fetches and classifies the sample frames
concurrently; it hands the raw model answers back. **Django decides what they
mean.**

```
   Django                                 go-moderation                Cloudflare
   ──────                                 ─────────────                ──────────
   SwiftModerationJob (Postgres)
        │
        │  GET /api/internal/moderation/lease/
        ├──────────────────────────────────▶  poll loop
        │   claims via the existing              │
        │   atomic UPDATE                        ├─ job ──┐  (≤ MOD_MAX_JOBS)
        │                                        │        │
        │                                        │   ┌────┴────┬────────┐
        │                                        │   ▼         ▼        ▼   (≤ MOD_MAX_FRAMES)
        │                                        │  frame    frame    frame
        │                                        │   │         │        │
        │                                        │   └────┬────┴────────┘
        │                                        │        │
        │                                        │   shared token bucket ──▶ /cdn-cgi/media/
        │                                        │                           /ai/run/{model}
        │  POST /api/internal/moderation/settle/ │
        ◀────────────────────────────────────────┘
   policy.combine_frames() decides
   ai_moderation.settle() writes
```

## Why this is in Go and not in Django

`swifts/ai_moderation.py::_classify` walks the sample points in a plain loop:

```python
for at_ms in timestamps:
    frame = cloudflare_ai.fetch_frame(source, at_ms)     # ~200ms
    results.append(cloudflare_ai.classify_image(frame))  # ~800ms
```

Every call is network-bound and none depends on the one before it, so a
ten-frame Swift spends ~10s doing something that can take ~1s. Python *can* do
this concurrently — a `ThreadPoolExecutor` would work — but then the rate limit
becomes a shared lock, per-frame errors need collecting across threads, and
cancellation has to be plumbed by hand. In Go that is the obvious way to write
it rather than the clever way.

The honest second reason: **the integration was already safe.**
`SwiftModerationJob` has an atomic claim (`UPDATE ... WHERE state='queued'`)
that the in-process thread and the `moderate_swifts` sweeper already race
through. Adding a third consumer needed no new concurrency design.

## What it deliberately does *not* do

This service is ignorant on purpose. It does not know what a Swift is, has no
opinion on what counts as unsafe, and **cannot change anybody's moderation
state**.

| Stays in Django | Why |
|---|---|
| `policy.combine_frames` thresholds | A safety decision. Two copies drift. |
| `policy.sample_timestamps` | "Which frames represent this video" is policy, already tested. |
| `parse_vision_response` | 40 lines of leniency for models that answer `"high"` where a float was asked for. A second parser is a second set of those bugs. |
| The conditional `PENDING → OK` update | Stops a late classification overturning a moderator. |
| `record_ai_report`'s `get_or_create` | Stops a retry filing a second queue item. |
| `VISION_PROMPT` | A filter prompt is a specification for getting past the filter. One home. |

**There is no verdict field in the settle payload.** A compromised or buggy
worker can delay a Swift; it cannot publish one. `combine_frames` treats a
barely-sampled video as a failure, not an approval —
`test_a_worker_cannot_publish_a_swift_by_asserting_it_is_safe` pins that.

## Running it

```bash
docker compose up --build
```

Or standalone against `manage.py runserver`:

```bash
cd services/go-moderation && MODERATION_WORKER_TOKEN=dev-moderation-token go run .
```

```bash
go test ./... -race -v
```

`-race` matters here: this service writes results from N goroutines into one
slice. It is safe (each goroutine owns its own index), but "safe by argument"
and "safe by tool" are different things.

### Verified output

`go1.27.0 windows/amd64`, `go vet ./...` clean, `go test ./... -race`:

```
ok  github.com/arcanix/approx/services/go-moderation  13.878s
```

18 tests, 0 data races. Breakdown:

| Group | Tests | Covers |
|---|---|---|
| Fan-out | 7 | ordering, concurrency, the per-job frame ceiling, failure reporting |
| Token secrecy | 3 (+6 subtests) | every error path — see below |
| Contract canary | 4 | strict decode, drift counting, renamed-field failure |
| Rate limit | 1 | production shape under load |
| Poll loop | 2 | lease/settle round trip, Django being down |

**The first run of this suite found three failures and one real defect.**
`apiRoot` was a package constant with no injection seam, so the tests were
making live calls to `api.cloudflare.com` — and `TestTheCloudflareTokenIsNeverReportedBack` was asserting against a response it never received. The seam is
now `Cloudflare.WithAPIRoot`, test-only, with no environment variable behind
it. **If you add an outbound call, give it the same seam**, or the test you
write for it will silently exercise the internet.

### Observed rate limit

`TestSharedRateLimitHoldsUnderProductionConcurrency` drives the real
production shape — `MOD_MAX_JOBS=4` × `MOD_MAX_FRAMES=6` = 24 goroutines
competing for one bucket at `MOD_RATE_PER_SEC=8`, `MOD_RATE_BURST=16`:

```
aggregate:      40 calls in 3.001s
observed rate:  13.33/s  (including the initial burst of 16)
sustained rate:  8.00/s  (target 8/s)
```

**The aggregate is 8/s, not 8/s per job.** That is worth stating plainly
because the intuitive reading is the other one: the bucket is *shared across
every in-flight job*, so concurrency does not multiply the request rate. Four
jobs each politely limiting themselves to 8/s would be 32/s at Cloudflare, and
preventing exactly that multiplication is the entire reason the limiter is a
field on the client rather than created per job.

If you want 32/s aggregate, set `MOD_RATE_PER_SEC=32` — do not add a second
limiter.

## Configuration

| Variable | Default | |
|---|---|---|
| `DJANGO_BASE_URL` | `http://127.0.0.1:8000` | |
| `MODERATION_WORKER_TOKEN` | — | Shared secret. Must match Django's. |
| `CLOUDFLARE_ACCOUNT_ID` / `CLOUDFLARE_API_TOKEN` | — | Same values Django reads. |
| `CLOUDFLARE_MEDIA_TRANSFORM_BASE` | — | Must be a **custom domain**, not `r2.dev`. |
| `MOD_MAX_JOBS` | 4 | Concurrent Swifts. |
| `MOD_MAX_FRAMES` | 6 | Concurrent frames per Swift. |
| `MOD_RATE_PER_SEC` | 8 | Workers AI calls/sec, **shared across all jobs**. |
| `MOD_JOB_BUDGET_S` | 180 | Must stay well under Django's `SWIFT_AI_STALE_AFTER_S`. |

## Trade-offs and things to watch

**Contract drift, not schema drift.** This service never touches Postgres, so
there is no migration it can fall behind. What it *can* fall behind is the
lease/settle JSON — and that failure is silent by default, because JSON
decoding ignores unknown fields.

Three things guard it, and you need to know about the middle one:

1. `swifts/test_worker_endpoints.py` asserts every lease field exists on the
   Django side.
2. **The canary tests in `hardening_test.go`.** `canaryLeasePayload` is a
   hand-written transcription of what
   `ModerationLeaseView._describe` returns. `TestTheCurrentContractDecodesStrictly` asserts today's payload decodes under `DisallowUnknownFields` *and*
   that every field arrives with its expected value — so a **rename** fails
   too, which strict decoding alone would not catch once the lenient fallback
   ran. `TestLeaseDecodingRejectsAnUnknownField` asserts the strict decoder
   still rejects a field this build has never heard of.

   > **When you change the lease contract, change `canaryLeasePayload` in
   > `hardening_test.go` and add the field to `Lease` in `types.go`.** The
   > canary is written out by hand on purpose — generating it by marshalling
   > `Lease` would make it agree with itself no matter what the struct said,
   > and prove nothing. If CI fails here, that is the test working.

3. At runtime, `decodeLeases` tries strict first and falls back to lenient,
   logging a warning and incrementing `contract_drift_total` on `/v1/stats`.

That last point is a deliberate departure from "fail loudly and stop". Pure
strict decoding would turn a *backwards-compatible* Django addition into an
outage — a field this worker does not need would reject every lease and stop
moderation until Go was redeployed. So drift is loud (log + metric + CI) but
never fatal. **Alert on `contract_drift_total > 0`.** The dangerous case, a
renamed field, is still hard-failed: `Lease.Validate` refuses a lease with no
timestamps as non-retryable rather than reporting zero classified frames.

**`MOD_JOB_BUDGET_S` vs `SWIFT_AI_STALE_AFTER_S` (default 900s).** If a job
takes longer than Django's stale threshold, Django re-queues it while this
service is still working on it — and the work is done twice and billed twice.
Keep the budget comfortably under.

**Duplicate work is bounded, not impossible.** If this service dies mid-job the
row stays `running` until the stale sweep re-queues it. That is by design — the
durability lives in Postgres, not in this process, which is why this workload
was a better candidate than the push dispatcher, whose queue is in memory.

**Both workers can run at once.** The Python sweeper and this service lease
through the same atomic claim, so a job goes to exactly one. That is what makes
the migration incremental: run both, watch the counters, then turn
`moderate_swifts --loop` off.

**Credentials are duplicated, not shared.** Both sides read
`CLOUDFLARE_API_TOKEN`. There is no way around that without a secret manager,
but it means a rotation has to touch two services.

The token never reaches anything a moderator can read, and this is verified
across *every* error path rather than the happy one:

| Path | Test |
|---|---|
| 4xx with the header echoed back | `TestTheTokenNeverLeaksOnAnyErrorPath/400` |
| 5xx with the header echoed back | `.../500` |
| `200 {"success": false}` | `.../success-false` |
| `200` that is not JSON | `.../malformed` |
| `200` with an empty model response | `.../empty-response` |
| Client-side timeout | `.../hang` |
| Media-Transformations 4xx body | `TestTheTokenNeverLeaksThroughTheFrameFetchPath` |
| The whole settle payload, end to end | `TestTheTokenNeverReachesTheSettlePayload` |

The last one marshals the entire `SettleRequest` and greps the bytes, rather
than checking the fields somebody remembered — so a *future* field carrying an
unredacted provider string fails it. `assertNoToken` also rejects a 16-character
prefix, because a truncation that cut a token in half would still publish most
of it. `redact()` was extended to the Media-Transformations path during this
work: no `Authorization` header is sent there, but "there should be nothing to
scrub" is not a property worth resting a credential on.

**Deployment differs from Django's.** Django deploys to Railway from the
`Procfile`. This deploys as a container built from the Dockerfile here — a
static binary in a `scratch` image, ~10MB, no interpreter and no dependency
resolution at boot. The `docker-compose.yml` at the repo root is for local
development only; it is not how either side reaches production.
