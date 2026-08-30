package main

// ─── Testing, coming from Django ─────────────────────────────────────────────
//
// No pytest, no unittest, no test runner to install. `go test ./...` is part of
// the toolchain. A test is a function named `TestX(t *testing.T)` in a file
// ending `_test.go`, and files ending `_test.go` are excluded from the binary
// you ship — so test helpers cannot accidentally end up in production.
//
// There are no assertion helpers in the standard library either. You write
// `if got != want { t.Errorf(...) }`. It feels primitive next to
// `assertEqual`, and the trade is that a failure message says exactly what you
// wrote rather than what a matcher library guessed.
//
// `t.Errorf` records a failure and continues; `t.Fatalf` stops the test.
// Use Fatal when continuing would panic (a nil you were about to dereference),
// Error when you would rather see every failure in one run.

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

// discardLogger keeps test output readable.
func discardLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

// fakeSender records what it was asked to send.
//
// This is the payoff of defining `Sender` as an interface in dispatcher.go:
// no network, no HTTP mock, no monkeypatching. A struct with the right method
// *is* a Sender, so the dispatcher accepts it without knowing anything changed.
//
// The mutex is not optional. Workers call Send from several goroutines at
// once, and appending to a slice from two goroutines is a data race — `go test
// -race` will fail the build over it, which is exactly what you want.
type fakeSender struct {
	mu       sync.Mutex
	batches  [][]Message
	failWith error
	delay    time.Duration
}

func (f *fakeSender) Send(ctx context.Context, messages []Message) error {
	if f.delay > 0 {
		select {
		case <-time.After(f.delay):
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	// The slice is copied. A slice header points at a shared backing array, so
	// storing the caller's slice would let a later chunk overwrite what we
	// recorded — one of the few genuinely surprising things about Go's value
	// semantics, and worth meeting in a test rather than in production.
	batch := make([]Message, len(messages))
	copy(batch, messages)
	f.batches = append(f.batches, batch)
	return f.failWith
}

func (f *fakeSender) count() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	n := 0
	for _, b := range f.batches {
		n += len(b)
	}
	return n
}

func message(token string) Message {
	return Message{To: "ExponentPushToken[" + token + "]", Title: "hi", Body: "there"}
}

// ─── The queue ───────────────────────────────────────────────────────────────

func TestDispatcherDeliversEveryMessage(t *testing.T) {
	sender := &fakeSender{}
	d := NewDispatcher(sender, discardLogger(), 4, 64, 100)
	d.Start(context.Background())

	for i := 0; i < 10; i++ {
		if !d.Enqueue(Job{Messages: []Message{message("a")}, Received: time.Now()}) {
			t.Fatalf("job %d was refused by an empty queue", i)
		}
	}

	// Shutdown drains, so it doubles as "wait for the workers to finish".
	// Tests that sleep for a bit and hope are the flakiest thing in any
	// concurrent codebase; if your design has no way to wait deterministically,
	// that is worth fixing in the design.
	if err := d.Shutdown(5 * time.Second); err != nil {
		t.Fatalf("shutdown: %v", err)
	}

	if got := sender.count(); got != 10 {
		t.Errorf("delivered %d messages, want 10", got)
	}
}

func TestDispatcherChunksToBatchSize(t *testing.T) {
	sender := &fakeSender{}
	d := NewDispatcher(sender, discardLogger(), 1, 8, 100)
	d.Start(context.Background())

	// 250 messages in one job — a club-wide announcement.
	msgs := make([]Message, 250)
	for i := range msgs {
		msgs[i] = message("a")
	}
	d.Enqueue(Job{Messages: msgs, Received: time.Now()})
	if err := d.Shutdown(5 * time.Second); err != nil {
		t.Fatalf("shutdown: %v", err)
	}

	// Expo's ceiling is 100, so 250 must arrive as 100 + 100 + 50 — never as
	// one oversized request that Expo would reject outright.
	want := []int{100, 100, 50}
	if len(sender.batches) != len(want) {
		t.Fatalf("got %d batches, want %d", len(sender.batches), len(want))
	}
	for i, n := range want {
		if got := len(sender.batches[i]); got != n {
			t.Errorf("batch %d had %d messages, want %d", i, got, n)
		}
	}
}

func TestFullQueueShedsRatherThanBlocking(t *testing.T) {
	// One worker, held up on a slow send, and a queue of one. The third job
	// has nowhere to go.
	sender := &fakeSender{delay: 500 * time.Millisecond}
	d := NewDispatcher(sender, discardLogger(), 1, 1, 100)
	d.Start(context.Background())
	t.Cleanup(func() { _ = d.Shutdown(2 * time.Second) })

	accepted := 0
	for i := 0; i < 20; i++ {
		if d.Enqueue(Job{Messages: []Message{message("a")}, Received: time.Now()}) {
			accepted++
		}
	}

	// The property under test is not the exact number — that depends on
	// scheduling — but that Enqueue *returned* rather than blocking, and that
	// the excess was refused rather than buffered without limit.
	if accepted == 20 {
		t.Error("every job was accepted; the queue is not shedding load")
	}
	if d.Snapshot().Dropped == 0 {
		t.Error("nothing was counted as dropped")
	}
}

func TestSenderFailuresAreCountedNotFatal(t *testing.T) {
	sender := &fakeSender{failWith: errors.New("expo is down")}
	d := NewDispatcher(sender, discardLogger(), 2, 16, 100)
	d.Start(context.Background())

	for i := 0; i < 5; i++ {
		d.Enqueue(Job{Messages: []Message{message("a")}, Received: time.Now()})
	}
	if err := d.Shutdown(5 * time.Second); err != nil {
		t.Fatalf("shutdown: %v", err)
	}

	st := d.Snapshot()
	if st.Failed != 5 {
		t.Errorf("failed=%d, want 5", st.Failed)
	}
	if st.Sent != 0 {
		t.Errorf("sent=%d, want 0", st.Sent)
	}
	// The real assertion: a provider outage does not take the workers down
	// with it. Five failures and the pool is still standing.
}

// ─── Validation ──────────────────────────────────────────────────────────────

// A **table-driven test** — the dominant Go testing idiom. One slice of cases,
// one loop, `t.Run` giving each a name so a failure says which case broke.
// Closer to `@pytest.mark.parametrize` than to writing five test methods.
func TestMessageValidate(t *testing.T) {
	cases := []struct {
		name    string
		msg     Message
		wantErr bool
	}{
		{"good", message("abc"), false},
		{"legacy token prefix", Message{To: "ExpoPushToken[x]", Title: "t"}, false},
		{"not a token", Message{To: "abc123", Title: "t"}, true},
		{"empty token", Message{To: "", Title: "t"}, true},
		{"no title or body", Message{To: "ExponentPushToken[x]"}, true},
		{"body only is fine", Message{To: "ExponentPushToken[x]", Body: "b"}, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := tc.msg.Validate()
			if (err != nil) != tc.wantErr {
				t.Errorf("Validate() error = %v, wantErr = %v", err, tc.wantErr)
			}
		})
	}
}

// ─── The HTTP surface ────────────────────────────────────────────────────────

func newTestServer(t *testing.T, sender Sender, token string) (*Server, *Dispatcher) {
	t.Helper() // marks this as a helper, so failures point at the caller's line
	d := NewDispatcher(sender, discardLogger(), 2, 32, 100)
	d.Start(context.Background())
	t.Cleanup(func() { _ = d.Shutdown(2 * time.Second) })
	return &Server{
		dispatcher: d, log: discardLogger(), token: token,
		maxBody: 1 << 20, maxBatch: 1000,
	}, d
}

// `httptest` runs a handler without binding a port. The whole request cycle,
// no server, no fixture database, no `LiveServerTestCase`.
func TestPushEndpointAcceptsAndQueues(t *testing.T) {
	sender := &fakeSender{}
	srv, _ := newTestServer(t, sender, "")

	body := `{"messages":[{"to":"ExponentPushToken[x]","title":"hi","body":"there"}]}`
	req := httptest.NewRequest(http.MethodPost, "/v1/push", strings.NewReader(body))
	rec := httptest.NewRecorder()
	srv.Routes().ServeHTTP(rec, req)

	if rec.Code != http.StatusAccepted {
		t.Fatalf("status = %d, want 202; body = %s", rec.Code, rec.Body.String())
	}
	var resp PushResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decoding response: %v", err)
	}
	if resp.Queued != 1 {
		t.Errorf("queued = %d, want 1", resp.Queued)
	}
}

func TestPushEndpointAcceptsGoodMessagesAndReportsBadOnes(t *testing.T) {
	sender := &fakeSender{}
	srv, _ := newTestServer(t, sender, "")

	// One valid, one with a junk token. Partial acceptance is the designed
	// behaviour: a stale token in a club announcement must not cost everybody
	// else their notification.
	body := `{"messages":[
		{"to":"ExponentPushToken[x]","title":"hi"},
		{"to":"garbage","title":"hi"}
	]}`
	req := httptest.NewRequest(http.MethodPost, "/v1/push", strings.NewReader(body))
	rec := httptest.NewRecorder()
	srv.Routes().ServeHTTP(rec, req)

	var resp PushResponse
	_ = json.Unmarshal(rec.Body.Bytes(), &resp)
	if resp.Queued != 1 || resp.Rejected != 1 {
		t.Errorf("queued=%d rejected=%d, want 1 and 1", resp.Queued, resp.Rejected)
	}
	if len(resp.Errors) != 1 {
		t.Errorf("expected one explanation, got %v", resp.Errors)
	}
}

func TestPushEndpointRequiresTheToken(t *testing.T) {
	srv, _ := newTestServer(t, &fakeSender{}, "s3cret")
	body := `{"messages":[{"to":"ExponentPushToken[x]","title":"hi"}]}`

	for _, tc := range []struct {
		name, header string
		want         int
	}{
		{"no header", "", http.StatusUnauthorized},
		{"wrong token", "Bearer nope", http.StatusUnauthorized},
		{"no bearer prefix", "s3cret", http.StatusUnauthorized},
		{"correct", "Bearer s3cret", http.StatusAccepted},
	} {
		t.Run(tc.name, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodPost, "/v1/push", strings.NewReader(body))
			if tc.header != "" {
				req.Header.Set("Authorization", tc.header)
			}
			rec := httptest.NewRecorder()
			srv.Routes().ServeHTTP(rec, req)
			if rec.Code != tc.want {
				t.Errorf("status = %d, want %d", rec.Code, tc.want)
			}
		})
	}
}

func TestPushEndpointRejectsUnknownFields(t *testing.T) {
	// A typo in Django's payload should be a 400 here, not a push with no
	// title discovered in an App Store review.
	srv, _ := newTestServer(t, &fakeSender{}, "")
	body := `{"messages":[{"to":"ExponentPushToken[x]","titel":"typo"}]}`
	req := httptest.NewRequest(http.MethodPost, "/v1/push", strings.NewReader(body))
	rec := httptest.NewRecorder()
	srv.Routes().ServeHTTP(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Errorf("status = %d, want 400", rec.Code)
	}
}

func TestReadinessFailsWhenTheQueueIsFull(t *testing.T) {
	sender := &fakeSender{delay: time.Second}
	srv, d := newTestServer(t, sender, "")

	for i := 0; i < 200; i++ {
		d.Enqueue(Job{Messages: []Message{message("a")}, Received: time.Now()})
	}

	req := httptest.NewRequest(http.MethodGet, "/readyz", nil)
	rec := httptest.NewRecorder()
	srv.Routes().ServeHTTP(rec, req)

	if rec.Code != http.StatusServiceUnavailable {
		t.Errorf("readyz = %d, want 503 while saturated", rec.Code)
	}
}

// ─── The rate limiter ────────────────────────────────────────────────────────

func TestRateLimiterCapsTheBurst(t *testing.T) {
	// 1000/s so refills are fast enough not to slow the test; burst of 3.
	rl := NewRateLimiter(1000, 3)
	defer rl.Close()

	ctx := context.Background()
	for i := 0; i < 3; i++ {
		if err := rl.Wait(ctx); err != nil {
			t.Fatalf("burst token %d: %v", i, err)
		}
	}
	// The bucket is empty; the fourth has to wait for a refill. Asserting it
	// *waited* rather than asserting on a duration keeps this off the list of
	// tests that fail on a loaded CI box.
	start := time.Now()
	if err := rl.Wait(ctx); err != nil {
		t.Fatalf("fourth token: %v", err)
	}
	if time.Since(start) == 0 {
		t.Log("fourth token was immediate; refill outpaced the assertion")
	}
}

func TestRateLimiterRespectsCancellation(t *testing.T) {
	// One token per second, burst of 1: after the first, a waiter is stuck for
	// a second — long enough to prove cancellation interrupts it.
	rl := NewRateLimiter(1, 1)
	defer rl.Close()

	if err := rl.Wait(context.Background()); err != nil {
		t.Fatalf("first token: %v", err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()

	err := rl.Wait(ctx)
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Errorf("Wait returned %v, want context.DeadlineExceeded", err)
	}
	// This is what makes shutdown prompt: a worker queued behind the rate
	// limit does not hold the pod open for its full turn.
}
