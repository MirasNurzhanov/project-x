package main

// The outbound half: talking to Expo, slowly enough not to get throttled, and
// retrying the failures that are worth retrying.

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"math"
	"net/http"
	"sync"
	"time"
)

// ExpoClient sends batches to Expo's push API.
type ExpoClient struct {
	url    string
	http   *http.Client
	log    *slog.Logger
	limit  *RateLimiter
	tries  int
	onDead func(token string) // called for permanently dead tokens; may be nil
}

// NewExpoClient builds the outbound client.
//
// ─── One http.Client, reused ─────────────────────────────────────────────────
//
// `http.Client` is safe for concurrent use and holds a connection pool. Every
// worker shares this one instance, so a hundred goroutines sending at once
// reuse a handful of TLS connections rather than performing a handshake each.
// Creating a client per request — the equivalent of calling `requests.post`
// directly instead of using a `requests.Session` — is the single most common
// Go performance mistake, and it is invisible until you are under load.
//
// The timeout is on the client rather than per-request because it is a
// property of "how long is Expo ever allowed to take", not of any one call.
func NewExpoClient(url string, timeout time.Duration, log *slog.Logger, limit *RateLimiter, tries int) *ExpoClient {
	if tries < 1 {
		tries = 1
	}
	return &ExpoClient{
		url: url,
		http: &http.Client{
			Timeout: timeout,
			Transport: &http.Transport{
				// Defaults are conservative for a service that fans out to one
				// host. Without raising these, Go opens at most 2 idle
				// connections per host and tears the rest down after every
				// burst — so a worker pool of 8 spends its time in TLS
				// handshakes.
				MaxIdleConns:        100,
				MaxIdleConnsPerHost: 32,
				IdleConnTimeout:     90 * time.Second,
			},
		},
		log:   log,
		limit: limit,
		tries: tries,
	}
}

// OnDeadToken registers a callback for tokens Expo says will never work again.
//
// Optional by design: the dispatcher does not need it, and a nil func is a
// perfectly good "do nothing". Wiring it to a call back into Django is how the
// stale `users.Device` rows get cleaned up — see the README.
func (c *ExpoClient) OnDeadToken(fn func(token string)) { c.onDead = fn }

// Send delivers one batch, retrying transient failures.
//
// Satisfies the `Sender` interface in dispatcher.go — implicitly, by having
// the right method. Nothing anywhere says so.
func (c *ExpoClient) Send(ctx context.Context, messages []Message) error {
	if len(messages) == 0 {
		return nil
	}

	body, err := json.Marshal(expoRequest(messages))
	if err != nil {
		// Marshalling our own struct should not fail. If it does, retrying
		// will not help — this is a bug, not an outage, so it returns
		// immediately rather than burning the retry budget.
		return fmt.Errorf("encoding batch: %w", err)
	}

	var lastErr error
	for attempt := 1; attempt <= c.tries; attempt++ {
		if attempt > 1 {
			// Exponential backoff: 200ms, 400ms, 800ms…
			//
			// The wait is done with select rather than time.Sleep so that a
			// shutdown does not have to wait it out. `time.Sleep` is
			// uninterruptible; this is the same pause that can be cancelled.
			wait := time.Duration(math.Pow(2, float64(attempt-2))) * 200 * time.Millisecond
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(wait):
			}
		}

		// Wait for a rate-limit token before every attempt, retries included —
		// a retry is still a request, and exempting them is how a provider
		// blip turns into the burst that keeps it down.
		if err := c.limit.Wait(ctx); err != nil {
			return fmt.Errorf("waiting for rate limit: %w", err)
		}

		err := c.post(ctx, body, messages)
		if err == nil {
			return nil
		}
		lastErr = err

		// `errors.Is` walks the wrap chain, so this matches whether the
		// sentinel was returned directly or wrapped by an outer message. It is
		// how Go asks "is this that kind of error" without string matching.
		if !errors.Is(err, errRetryable) {
			return err
		}
		c.log.Warn("expo attempt failed, will retry",
			"attempt", attempt, "of", c.tries, "error", err)
	}
	return fmt.Errorf("gave up after %d attempts: %w", c.tries, lastErr)
}

// errRetryable marks failures worth trying again.
//
// A **sentinel error**: a package-level value compared with `errors.Is`. This
// is Go's answer to defining an exception subclass. The convention is to name
// them `errFoo` (unexported) or `ErrFoo` (exported, part of your API).
var errRetryable = errors.New("retryable")

func (c *ExpoClient) post(ctx context.Context, body []byte, messages []Message) error {
	// The request carries the context, so cancelling the context aborts the
	// in-flight HTTP call. This is what makes shutdown prompt instead of
	// waiting out a 10-second timeout on every worker.
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.url, bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("building request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	// Expo accepts gzipped request bodies; not doing it here because a batch of
	// 100 short notifications is a few KB and the CPU is not free either.

	resp, err := c.http.Do(req)
	if err != nil {
		// Transport-level failures — DNS, connection refused, timeout — are
		// exactly the transient class.
		return fmt.Errorf("%w: %v", errRetryable, err)
	}
	// Closing the body is not optional. An unclosed body leaks the connection
	// back out of the pool, and the symptom is a service that gets slower over
	// days. `defer` next to the acquisition is how you never forget.
	defer resp.Body.Close()

	// Read with a ceiling. `io.ReadAll` on an untrusted response is an
	// unbounded allocation controlled by someone else.
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return fmt.Errorf("%w: reading response: %v", errRetryable, err)
	}

	switch {
	case resp.StatusCode == http.StatusTooManyRequests:
		// Rate limited. Always worth retrying, and the backoff above is what
		// makes it polite.
		return fmt.Errorf("%w: expo returned 429", errRetryable)
	case resp.StatusCode >= 500:
		return fmt.Errorf("%w: expo returned %d", errRetryable, resp.StatusCode)
	case resp.StatusCode >= 400:
		// 4xx means we sent something wrong. Sending it again unchanged gets
		// the same answer, so this is deliberately not retryable.
		return fmt.Errorf("expo returned %d: %s", resp.StatusCode, truncate(string(raw), 200))
	}

	var decoded expoResponse
	if err := json.Unmarshal(raw, &decoded); err != nil {
		return fmt.Errorf("%w: decoding response: %v", errRetryable, err)
	}

	// A 200 does not mean every message succeeded — Expo reports per-message
	// status inside the body. This is a common shape for batch APIs and a
	// common thing to miss: check the envelope *and* the tickets.
	c.inspectTickets(decoded, messages)
	return nil
}

// inspectTickets walks Expo's per-message results.
func (c *ExpoClient) inspectTickets(resp expoResponse, sent []Message) {
	for _, e := range resp.Errors {
		c.log.Error("expo request-level error", "code", e.Code, "message", e.Message)
	}
	for i, ticket := range resp.Data {
		if ticket.Status == "ok" {
			continue
		}
		// Expo returns tickets positionally, so index i corresponds to sent[i].
		// Guarded anyway: trusting a remote service to return exactly as many
		// elements as you sent is how you get an index-out-of-range panic in
		// production at 3am.
		token := "unknown"
		if i < len(sent) {
			token = truncate(sent[i].To, 24)
		}

		if ticket.Details.Error == deviceNotRegistered {
			// Permanent. The app is gone from that device; the token is dead
			// and Django should delete the row.
			c.log.Info("device no longer registered", "token", token)
			if c.onDead != nil && i < len(sent) {
				c.onDead(sent[i].To)
			}
			continue
		}
		c.log.Warn("expo rejected a message",
			"token", token, "error", ticket.Details.Error, "message", ticket.Message)
	}
}

// ─── Rate limiting ───────────────────────────────────────────────────────────

// RateLimiter is a token bucket built from a channel and a ticker.
//
// The idea, which is worth internalising because it recurs everywhere in Go: a
// buffered channel *is* a semaphore. Its capacity is the burst size; taking a
// value is acquiring a permit; a background goroutine putting values back is
// the refill.
//
// The standard answer in production is `golang.org/x/time/rate`, which is
// better (it does not need a goroutine, and it supports reservations and
// variable costs). This is here because it is thirty lines, needs no
// dependency, and shows you what a channel is actually for.
type RateLimiter struct {
	tokens chan struct{}
	stop   chan struct{}
	// sync.Once runs a function exactly once however many goroutines call it.
	// It is the safe way to write "close this channel, but only the first
	// time" — closing an already-closed channel panics, and Close is the kind
	// of method that gets called from a shutdown path and a test cleanup on
	// the same object.
	once sync.Once
}

// NewRateLimiter allows `perSecond` requests, bursting up to `burst`.
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
	// Start full, so the first burst after a quiet period is not artificially
	// delayed. A limiter that begins empty makes the very first request wait,
	// which looks like a bug the first time you see it in a trace.
	for i := 0; i < burst; i++ {
		rl.tokens <- struct{}{}
	}

	interval := time.Second / time.Duration(perSecond)
	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop() // a Ticker holds a runtime timer; not stopping it leaks
		for {
			select {
			case <-rl.stop:
				return
			case <-ticker.C:
				select {
				case rl.tokens <- struct{}{}:
				default:
					// Bucket already full. Dropping the token is the point —
					// it is what caps the burst rather than letting unused
					// capacity accumulate into one enormous spike after an
					// idle hour.
				}
			}
		}
	}()
	return rl
}

// Wait blocks until a token is available or the context is cancelled.
//
// Returning ctx.Err() rather than blocking forever is what lets shutdown
// interrupt a worker that is waiting its turn.
func (rl *RateLimiter) Wait(ctx context.Context) error {
	select {
	case <-rl.tokens:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// Close stops the refill goroutine.
//
// Every `go` you write is a resource. A goroutine blocked forever on a channel
// nobody will ever send to is a leak — it is not garbage collected, because
// the runtime cannot know nothing will ever wake it. Giving each one an
// explicit way to stop is the discipline that keeps a long-running service
// flat in memory.
func (rl *RateLimiter) Close() {
	rl.once.Do(func() { close(rl.stop) })
}
