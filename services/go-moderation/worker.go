package main

// The reason this service is in Go.
//
// ─── What Python was doing ───────────────────────────────────────────────────
//
// `swifts/ai_moderation.py::_classify` walks the sample points in a plain
// `for` loop:
//
//	for at_ms in timestamps:
//	    frame = cloudflare_ai.fetch_frame(source, at_ms)   # ~200ms
//	    results.append(cloudflare_ai.classify_image(frame)) # ~800ms
//
// Every one of those is network-bound and none of them depends on the one
// before it, so a ten-frame Swift spends ten seconds doing something that
// could take one. Python *can* do this concurrently — a ThreadPoolExecutor
// would work — but then the rate limit has to become a shared lock, the
// per-frame errors have to be collected across threads, and cancellation has
// to be plumbed by hand. That is the code below, except in Go it is the
// obvious way to write it rather than the clever way.
//
// ─── The two concurrency levels ──────────────────────────────────────────────
//
//	jobs        bounded by a semaphore  (maxConcurrentJobs)
//	  └─ frames bounded by a semaphore  (maxConcurrentFrames, per job)
//	       └─ every Workers AI call passes one shared token bucket
//
// The rate limiter is the one that actually protects the bill, and it is
// deliberately *global* rather than per job: eight Swifts each politely
// limiting themselves to four requests a second is thirty-two requests a
// second at Cloudflare.

import (
	"context"
	"fmt"
	"log/slog"
	"sync"
	"sync/atomic"
	"time"
)

type Worker struct {
	cf     *Cloudflare
	django *DjangoClient
	log    *slog.Logger

	maxJobs   int
	maxFrames int
	jobBudget time.Duration

	// jobSlots is a **counting semaphore built from a buffered channel** — the
	// same trick as the rate limiter, used for a different purpose. Sending
	// acquires, receiving releases, and the capacity is the ceiling. There is
	// no `sync.Semaphore` in the standard library because this is it.
	jobSlots chan struct{}

	leased       atomic.Int64
	settled      atomic.Int64
	framesOK     atomic.Int64
	framesFailed atomic.Int64
	jobsFailed   atomic.Int64
	inFlight     atomic.Int64
}

func NewWorker(cf *Cloudflare, django *DjangoClient, log *slog.Logger, maxJobs, maxFrames int, budget time.Duration) *Worker {
	if maxJobs < 1 {
		maxJobs = 4
	}
	if maxFrames < 1 {
		maxFrames = 6
	}
	return &Worker{
		cf: cf, django: django, log: log,
		maxJobs: maxJobs, maxFrames: maxFrames, jobBudget: budget,
		jobSlots: make(chan struct{}, maxJobs),
	}
}

// Run polls Django for work until the context is cancelled.
//
// A poll loop rather than a push, because the alternative — Django calling
// this service when a Swift is uploaded — makes moderation depend on the web
// worker being able to reach us at that instant. Polling means a restart, a
// deploy, or a five-minute outage of this service costs latency and nothing
// else: the jobs are sitting in Postgres where Django left them, and the
// existing `manage.py moderate_swifts` sweeper is still a valid fallback.
func (w *Worker) Run(ctx context.Context, interval time.Duration) {
	// A WaitGroup for the in-flight jobs, so Run does not return while
	// goroutines it started are still writing to Django.
	var wg sync.WaitGroup
	defer wg.Wait()

	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	for {
		// Fill every free slot on each tick rather than taking one job — a
		// backlog should drain at `maxJobs` wide, not one job per interval.
		free := w.maxJobs - int(w.inFlight.Load())
		if free > 0 {
			leases, err := w.django.Lease(ctx, free)
			if err != nil {
				// Django being unreachable is expected during its own deploys.
				// Logged at warn, not error: it is self-correcting, and paging
				// on it would page on every Django release.
				w.log.Warn("lease failed", "error_type", fmt.Sprintf("%T", err))
			}
			for _, lease := range leases {
				w.leased.Add(1)
				wg.Add(1)
				go func(l Lease) {
					defer wg.Done()
					w.runJob(ctx, l)
				}(lease)
			}
		}

		select {
		case <-ctx.Done():
			w.log.Info("poll loop stopping", "in_flight", w.inFlight.Load())
			return
		case <-ticker.C:
		}
	}
}

// runJob classifies one Swift and reports back.
func (w *Worker) runJob(ctx context.Context, lease Lease) {
	// Acquire a job slot. This blocks when `maxJobs` are already running,
	// which is the backpressure that stops a large backlog opening hundreds of
	// concurrent Cloudflare calls.
	select {
	case w.jobSlots <- struct{}{}:
		defer func() { <-w.jobSlots }()
	case <-ctx.Done():
		return
	}

	w.inFlight.Add(1)
	defer w.inFlight.Add(-1)

	log := w.log.With("job", lease.JobID, "swift", lease.SwiftID)

	// Every job gets a deadline of its own. Without one, a single Swift whose
	// frames all hang would hold a slot until the process died — and
	// `SWIFT_AI_STALE_AFTER_S` on Django's side would eventually re-queue it,
	// producing a second run of work that was never abandoned. A budget here
	// keeps the two sides' notions of "still running" roughly in step.
	ctx, cancel := context.WithTimeout(ctx, w.jobBudget)
	defer cancel()

	if err := lease.Validate(); err != nil {
		// Not retryable: the payload will be identical next time.
		log.Error("unusable lease", "error_type", fmt.Sprintf("%T", err))
		w.settle(ctx, SettleRequest{
			JobID: lease.JobID, Error: err.Error(), Retryable: false,
		})
		return
	}

	started := time.Now()
	frames := w.classifyFrames(ctx, lease, log)

	result := SettleRequest{
		JobID:     lease.JobID,
		Frames:    frames,
		ModelUsed: lease.VisionModel,
		Retryable: true,
	}

	// Audio is best effort and deliberately cannot fail the job. A video that
	// has been classified frame by frame and found clean is not held back
	// because the audio track would not fetch — the transcript is a second
	// opinion on a different channel, not a second gate.
	if lease.WantAudio {
		if transcript, err := w.transcribe(ctx, lease); err != nil {
			log.Warn("audio skipped", "error_type", fmt.Sprintf("%T", err))
		} else {
			result.Transcript = transcript
			result.AudioChecked = true
		}
	}

	w.settle(ctx, result)
	log.Info("job finished",
		"frames", len(frames),
		"took_ms", time.Since(started).Milliseconds())
}

// classifyFrames is the fan-out: every sample point at once, bounded.
//
// ─── Collecting results from goroutines without a mutex ──────────────────────
//
// The results slice is allocated up front at exactly the right length, and
// each goroutine writes to *its own index*. Concurrent writes to distinct
// elements of a slice are safe — the goroutines never touch the same memory —
// so no lock is needed and the output stays in timestamp order for free.
//
// This is worth internalising because the alternative most people reach for is
// `append` under a mutex, which needs the lock, loses the ordering, and is
// slower. Preallocate-and-index is the idiomatic shape whenever you know how
// many results you are going to get.
func (w *Worker) classifyFrames(ctx context.Context, lease Lease, log *slog.Logger) []FrameResult {
	results := make([]FrameResult, len(lease.TimestampsMS))

	// Per-job frame semaphore. Separate from the job semaphore so the two
	// ceilings compose: maxJobs × maxFrames is the worst case in flight.
	slots := make(chan struct{}, w.maxFrames)
	var wg sync.WaitGroup

	for i, atMS := range lease.TimestampsMS {
		wg.Add(1)
		go func(idx, at int) {
			defer wg.Done()

			select {
			case slots <- struct{}{}:
				defer func() { <-slots }()
			case <-ctx.Done():
				results[idx] = FrameResult{AtMS: at, Error: "cancelled"}
				return
			}

			results[idx] = w.classifyOne(ctx, lease, at)
			if results[idx].Error != "" {
				w.framesFailed.Add(1)
				// Debug, not warn: one failed frame out of ten is a normal
				// outcome that Django already knows how to weigh. Logging it
				// at warn would make a healthy service look sick.
				log.Debug("frame failed", "at_ms", at)
			} else {
				w.framesOK.Add(1)
			}
		}(i, atMS)
	}

	wg.Wait()
	return results
}

func (w *Worker) classifyOne(ctx context.Context, lease Lease, atMS int) FrameResult {
	image, err := w.cf.FetchFrame(ctx, lease.SourceURL, atMS, lease.FrameWidth)
	if err != nil {
		return FrameResult{AtMS: atMS, Error: redact(err.Error())}
	}
	raw, err := w.cf.ClassifyFrame(ctx, lease.VisionModel, lease.Prompt, image, VisionOptions{
		ResponseFormat: lease.VisionResponseFormat,
		MaxTokens:      lease.VisionMaxTokens,
		InputFormat:    lease.VisionInputFormat,
	})
	if err != nil {
		return FrameResult{AtMS: atMS, Error: redact(err.Error())}
	}
	// The model's answer, untouched. Django parses it — see FrameResult.
	return FrameResult{AtMS: atMS, Raw: raw}
}

func (w *Worker) transcribe(ctx context.Context, lease Lease) (string, error) {
	audio, err := w.cf.FetchAudio(ctx, lease.SourceURL, lease.DurationMS)
	if err != nil {
		return "", err
	}
	return w.cf.Transcribe(ctx, lease.TranscribeModel, audio)
}

// settle reports the outcome, with its own context.
//
// The job's own deadline may already have expired — that is precisely when
// reporting matters most, because otherwise Django waits out
// `SWIFT_AI_STALE_AFTER_S` before re-queueing. So this deliberately does not
// inherit the job budget.
func (w *Worker) settle(parent context.Context, req SettleRequest) {
	ctx, cancel := context.WithTimeout(context.WithoutCancel(parent), 15*time.Second)
	defer cancel()

	if err := w.django.Settle(ctx, req); err != nil {
		w.jobsFailed.Add(1)
		// The job is now orphaned in `running` on Django's side. That is
		// survivable by design: `due_jobs` picks up stale running rows after
		// SWIFT_AI_STALE_AFTER_S, so the worst case is a delayed retry rather
		// than a Swift stuck invisible forever. Worth an error line because a
		// rising count here means the two sides cannot talk.
		w.log.Error("settle failed; job will be re-queued as stale",
			"job", req.JobID, "error_type", fmt.Sprintf("%T", err))
		return
	}
	w.settled.Add(1)
}

func (w *Worker) Snapshot() Stats {
	return Stats{
		Leased:        w.leased.Load(),
		Settled:       w.settled.Load(),
		FramesOK:      w.framesOK.Load(),
		FramesFailed:  w.framesFailed.Load(),
		JobsFailed:    w.jobsFailed.Load(),
		InFlight:      w.inFlight.Load(),
		MaxConcurrent: w.maxJobs,
		ContractDrift: w.django.Drift(),
	}
}

// ─── Rate limiting ───────────────────────────────────────────────────────────

// RateLimiter is a token bucket: a buffered channel refilled by a ticker.
//
// The same shape as the one in services/go-push, and shared here across *all*
// in-flight jobs — which is the point. A per-job limiter would let concurrency
// multiply the request rate, which is exactly the bill this exists to bound.
type RateLimiter struct {
	tokens chan struct{}
	stop   chan struct{}
	once   sync.Once
}

func NewRateLimiter(perSecond, burst int) *RateLimiter {
	if perSecond < 1 {
		perSecond = 1
	}
	if burst < 1 {
		burst = 1
	}
	rl := &RateLimiter{
		tokens: make(chan struct{}, burst),
		stop:   make(chan struct{}),
	}
	for i := 0; i < burst; i++ {
		rl.tokens <- struct{}{}
	}
	go func() {
		ticker := time.NewTicker(time.Second / time.Duration(perSecond))
		defer ticker.Stop()
		for {
			select {
			case <-rl.stop:
				return
			case <-ticker.C:
				select {
				case rl.tokens <- struct{}{}:
				default: // bucket full; dropping the token is what caps the burst
				}
			}
		}
	}()
	return rl
}

func (rl *RateLimiter) Wait(ctx context.Context) error {
	select {
	case <-rl.tokens:
		return nil
	case <-ctx.Done():
		return fmt.Errorf("rate limit wait cancelled: %w", ctx.Err())
	}
}

func (rl *RateLimiter) Close() { rl.once.Do(func() { close(rl.stop) }) }
