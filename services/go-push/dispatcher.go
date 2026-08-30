package main

// The concurrency core: a queue, a pool of workers, and a way to stop cleanly.
//
// ─── Goroutines and channels, coming from Python ─────────────────────────────
//
// A **goroutine** is a function running concurrently. You start one by putting
// `go` in front of a call: `go worker()`. That is the entire syntax.
//
// It is not a thread. The Go runtime multiplexes many goroutines onto a small
// number of OS threads, and a goroutine starts with a ~2KB stack that grows on
// demand. A thread starts at ~1MB. This is why the answer to "how many can I
// have" is different by three orders of magnitude: your Django code spawns one
// daemon thread per push batch and that is a real cost you have to think
// about; here, tens of thousands of goroutines is unremarkable.
//
// A **channel** is a typed pipe between goroutines. `make(chan Job, 100)` is a
// queue holding up to 100 `Job` values. `ch <- job` sends, `job := <-ch`
// receives. Both block when they cannot proceed — a send blocks when the
// buffer is full, a receive blocks when it is empty — and that blocking *is*
// the synchronisation. There is no lock to forget to release.
//
// The slogan is "share memory by communicating, don't communicate by sharing
// memory". In Python you would reach for `queue.Queue` plus a `threading.Lock`
// around any shared counter. Here the queue is a language primitive, and for
// the counters below we use `sync/atomic` rather than a mutex because they are
// single integers touched from many goroutines.
//
// ─── What this file actually does ────────────────────────────────────────────
//
//	POST /v1/push ──▶ Enqueue() ──▶ buffered channel ──┬──▶ worker goroutine ──┐
//	                  (returns                          ├──▶ worker goroutine ──┤
//	                   immediately)                     └──▶ worker goroutine ──┘
//	                                                                            │
//	                                                    token-bucket rate limit ─┤
//	                                                                            ▼
//	                                                             Expo, 100 per request

import (
	"context"
	"log/slog"
	"sync"
	"sync/atomic"
	"time"
)

// Job is one unit of work on the queue: a batch of messages that arrived
// together, plus enough context to explain it in a log line.
type Job struct {
	Messages []Message
	// Key is the caller's idempotency key, for correlating logs across Django
	// and this service. Not used for deduplication — see the note on
	// PushRequest.
	Key string
	// Received is when we accepted it, so a worker can report how long the job
	// sat in the queue. Queue latency is the number that tells you whether the
	// pool is big enough, and it cannot be reconstructed after the fact.
	Received time.Time
}

// Sender is what a Dispatcher needs from the outbound side.
//
// ─── Interfaces, coming from Python ──────────────────────────────────────────
//
// This is an interface: a list of method signatures. `ExpoClient` satisfies it
// simply by having a `Send` method with a matching signature — there is no
// `implements` keyword and no registration. Go interfaces are structural, like
// Python's duck typing, but checked at compile time.
//
// It is here for one concrete reason: the test replaces the real Expo client
// with a fake one that records what it was asked to send, without a network
// mock or a live endpoint. Defining the interface *here*, next to the consumer
// rather than next to the implementation, is the Go convention — "accept
// interfaces, return structs".
type Sender interface {
	Send(ctx context.Context, messages []Message) error
}

// Dispatcher owns the queue and the workers.
type Dispatcher struct {
	jobs    chan Job
	sender  Sender
	log     *slog.Logger
	workers int

	// batchSize is how many messages go in one outbound request. Expo's
	// documented ceiling is 100.
	batchSize int

	// wg tracks the running workers so Shutdown can wait for them.
	// A WaitGroup is a counter you Add to before starting work and Done when
	// it finishes; Wait blocks until it hits zero. It is `threading.Barrier`'s
	// simpler cousin, and the closest analogue to `Thread.join()` for a group.
	wg sync.WaitGroup

	// Counters, touched from every worker at once.
	//
	// `atomic.Int64` rather than a plain int64 guarded by a mutex: these are
	// single values with no invariant tying them to each other, which is
	// exactly the case atomics are for. A plain `d.sent++` from eight
	// goroutines is a data race, and Go's race detector (`go test -race`) will
	// tell you so — the language does not stop you writing it, but the
	// toolchain makes it very hard to ship.
	queued  atomic.Int64
	sent    atomic.Int64
	failed  atomic.Int64
	dropped atomic.Int64
}

// NewDispatcher builds a dispatcher. It does not start anything yet.
//
// Go has no constructors. The convention is a package-level `NewX` function
// returning `*X`. Note there is no error return: nothing here can fail, so
// pretending it might would make every call site noisier for nothing.
func NewDispatcher(sender Sender, log *slog.Logger, workers, queueSize, batchSize int) *Dispatcher {
	// Defensive floors. Go's zero value for an int is 0, and a config struct
	// with a missing field will hand you exactly that — a dispatcher with zero
	// workers accepts jobs forever and delivers none, which is the worst
	// possible failure because it looks like it is working.
	if workers < 1 {
		workers = 1
	}
	if queueSize < 1 {
		queueSize = 1
	}
	if batchSize < 1 {
		batchSize = 100
	}
	return &Dispatcher{
		// A *buffered* channel. The capacity is the backlog this service will
		// hold before it starts refusing work. Unbuffered (`make(chan Job)`)
		// would mean every Enqueue blocks until a worker is free, which turns
		// this into a synchronous proxy and defeats the point.
		jobs:      make(chan Job, queueSize),
		sender:    sender,
		log:       log,
		workers:   workers,
		batchSize: batchSize,
	}
}

// Start launches the worker goroutines. Returns immediately.
func (d *Dispatcher) Start(ctx context.Context) {
	for i := 0; i < d.workers; i++ {
		d.wg.Add(1)
		// `go func(){...}(i)` starts a goroutine running that closure.
		//
		// The argument is passed explicitly rather than captured. Since Go
		// 1.22 loop variables are per-iteration and capturing `i` directly
		// would be correct — but passing it is clearer about intent and still
		// what you will see in most codebases.
		go func(id int) {
			// `defer` schedules a call for when the surrounding function
			// returns, however it returns. It is Python's `finally`, and the
			// idiom is to write the cleanup on the line after the thing that
			// needs cleaning up, so the two cannot drift apart.
			defer d.wg.Done()
			d.work(ctx, id)
		}(i)
	}
	d.log.Info("dispatcher started", "workers", d.workers, "queue_capacity", cap(d.jobs))
}

// Enqueue offers a job to the queue. Reports whether it was accepted.
//
// ─── select, and why this never blocks ───────────────────────────────────────
//
// `select` waits on several channel operations and runs whichever is ready
// first. With a `default` branch it does not wait at all: if no case can
// proceed immediately, `default` runs. That turns a blocking send into a
// try-send, which is exactly what an HTTP handler wants — the one thing an
// ingest endpoint must never do is block because a downstream is slow.
//
// So a full queue is a fast 503, not a hung request. That is **backpressure**,
// and choosing to shed load rather than absorb it without limit is the single
// most important decision in a service like this. Your Django version has no
// equivalent: `threading.Thread(...).start()` always succeeds, right up until
// the process runs out of memory.
func (d *Dispatcher) Enqueue(job Job) bool {
	select {
	case d.jobs <- job:
		d.queued.Add(int64(len(job.Messages)))
		return true
	default:
		d.dropped.Add(int64(len(job.Messages)))
		d.log.Warn("queue full, shedding job",
			"messages", len(job.Messages),
			"queue_capacity", cap(d.jobs),
			"key", job.Key)
		return false
	}
}

// work is one worker's loop: take a job, chunk it, send the chunks.
func (d *Dispatcher) work(ctx context.Context, id int) {
	log := d.log.With("worker", id)

	// `range` over a channel receives until the channel is closed and drained.
	// It is the cleanest way to write a worker loop, and it is why Shutdown
	// below closes the channel rather than setting a flag: closing is the
	// signal, and it is delivered to every worker exactly once with no shared
	// state involved.
	for job := range d.jobs {
		waited := time.Since(job.Received)
		if waited > time.Second {
			// Queue latency is the early warning that the pool is undersized.
			// Logged rather than silently tolerated because by the time it
			// shows up as user-visible slowness it has been true for a while.
			log.Warn("job waited in queue", "waited", waited, "messages", len(job.Messages))
		}

		// Expo caps a request at 100 messages, so a job for a 250-member club
		// is three requests. Chunking here rather than at ingest keeps the
		// queue's unit "one thing Django asked for", which is the unit worth
		// logging and counting.
		for start := 0; start < len(job.Messages); start += d.batchSize {
			end := min(start+d.batchSize, len(job.Messages))
			chunk := job.Messages[start:end]

			// `ctx` carries the shutdown signal. Checking it between chunks
			// means a SIGTERM interrupts a large job at a chunk boundary
			// rather than after all of it — see Shutdown for why that matters.
			if err := ctx.Err(); err != nil {
				d.failed.Add(int64(len(job.Messages) - start))
				log.Warn("abandoning remaining chunks, shutting down",
					"unsent", len(job.Messages)-start)
				return
			}

			if err := d.sender.Send(ctx, chunk); err != nil {
				d.failed.Add(int64(len(chunk)))
				// Logged and dropped, not retried here. The retry lives in the
				// Expo client, which is the layer that can tell a transient
				// failure from a permanent one. A second retry loop out here
				// would multiply with that one and turn a provider outage into
				// a self-inflicted traffic spike.
				log.Error("chunk failed", "messages", len(chunk), "error", err)
				continue
			}
			d.sent.Add(int64(len(chunk)))
		}
	}
	log.Debug("worker stopped")
}

// Shutdown stops accepting work and waits for what is in flight.
//
// ─── Graceful shutdown, and why it is not optional ───────────────────────────
//
// Kubernetes stops a pod by sending SIGTERM and waiting
// `terminationGracePeriodSeconds` before SIGKILL. A service that exits
// immediately on SIGTERM throws away whatever was in its queue. Since this
// queue is in memory, that is silent data loss on every single deploy — and
// you deploy far more often than Expo goes down.
//
// So: close the channel (workers finish the current job and exit as `range`
// drains), then wait for them, with a ceiling so a wedged worker cannot hold
// the pod open past the grace period.
//
// This is also the honest limit of the design, and it is worth being blunt
// about: a crash, an OOM kill, or a node failure loses the queue. That is the
// same durability your current daemon-thread version has, which is why it is
// an acceptable starting point — but if pushes ever become something you must
// not lose, the queue has to move to Redis or Postgres and this becomes a
// consumer rather than an owner. See the README's trade-offs section.
func (d *Dispatcher) Shutdown(timeout time.Duration) error {
	close(d.jobs)

	done := make(chan struct{})
	// `chan struct{}` is the idiomatic "signal only, no data" channel —
	// `struct{}` is a type that occupies zero bytes. Closing it is the signal.
	go func() {
		d.wg.Wait()
		close(done)
	}()

	select {
	case <-done:
		d.log.Info("dispatcher drained",
			"sent", d.sent.Load(), "failed", d.failed.Load())
		return nil
	case <-time.After(timeout):
		// `time.After` returns a channel that fires once after the duration.
		// Combined with select, that is Go's timeout idiom.
		d.log.Error("dispatcher shutdown timed out; queue not fully drained",
			"unsent_estimate", len(d.jobs))
		return context.DeadlineExceeded
	}
}

// Snapshot returns the current counters.
//
// A value, not a pointer, and not the live struct — the caller gets a copy it
// cannot use to mutate our state. `atomic.Int64` cannot be copied safely
// (`go vet` will tell you so), which is one reason Stats is a separate type
// rather than the Dispatcher itself.
func (d *Dispatcher) Snapshot() Stats {
	return Stats{
		Queued:     d.queued.Load(),
		Sent:       d.sent.Load(),
		Failed:     d.failed.Load(),
		Dropped:    d.dropped.Load(),
		QueueDepth: len(d.jobs),
		QueueCap:   cap(d.jobs),
		Workers:    d.workers,
	}
}
