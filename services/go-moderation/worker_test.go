package main

// Tests for the parts that are this service's own: the fan-out, the
// concurrency ceilings, and the shape of what goes back to Django.
//
// Everything about *moderation policy* is tested on the Django side, in
// `swifts/test_worker_endpoints.py` and `moderation/test_policy.py`. That is
// not an oversight — there is no policy here to test, which is the point of
// where the boundary was drawn.

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func discardLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

// fakeCloudflare stands in for the two Cloudflare endpoints with an
// `httptest.Server` — a real HTTP server on a real port, so the client's own
// request building, header setting and response handling are all exercised.
// Mocking `http.Client` instead would test less and be harder to read.
type fakeCloudflare struct {
	server *httptest.Server

	mu        sync.Mutex
	maxInFlight int32
	inFlight    int32

	frameCalls   atomic.Int64
	classifyCalls atomic.Int64

	// response is what the vision model "says". Raw, because this service
	// never parses it.
	response string
	// failFrames makes frame fetches return 500.
	failFrames bool
	// delay simulates a slow provider, so concurrency is observable.
	delay time.Duration
}

func newFakeCloudflare(t *testing.T) *fakeCloudflare {
	t.Helper()
	f := &fakeCloudflare{response: `{"unsafe": false, "confidence": 0.9}`}

	mux := http.NewServeMux()

	// Media Transformations: /cdn-cgi/media/<opts>/<source>
	mux.HandleFunc("/cdn-cgi/media/", func(w http.ResponseWriter, r *http.Request) {
		f.frameCalls.Add(1)
		if f.failFrames {
			w.WriteHeader(http.StatusInternalServerError)
			return
		}
		if f.delay > 0 {
			time.Sleep(f.delay)
		}
		_, _ = w.Write([]byte{0xFF, 0xD8, 0xFF}) // a plausible JPEG header
	})

	// Workers AI: /accounts/{id}/ai/run/{model}
	mux.HandleFunc("/accounts/", func(w http.ResponseWriter, r *http.Request) {
		// Track peak concurrency, which is what the ceiling tests assert on.
		cur := atomic.AddInt32(&f.inFlight, 1)
		defer atomic.AddInt32(&f.inFlight, -1)
		f.mu.Lock()
		if cur > f.maxInFlight {
			f.maxInFlight = cur
		}
		f.mu.Unlock()

		f.classifyCalls.Add(1)
		if f.delay > 0 {
			time.Sleep(f.delay)
		}
		if r.Header.Get("Authorization") == "" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprintf(w, `{"success":true,"result":{"response":%q}}`, f.response)
	})

	f.server = httptest.NewServer(mux)
	t.Cleanup(f.server.Close)
	return f
}

func (f *fakeCloudflare) peak() int32 {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.maxInFlight
}

// fakeDjango records what was settled and hands out leases on demand.
type fakeDjango struct {
	server *httptest.Server

	mu      sync.Mutex
	pending []Lease
	settled []SettleRequest
}

func newFakeDjango(t *testing.T) *fakeDjango {
	t.Helper()
	d := &fakeDjango{}
	mux := http.NewServeMux()

	mux.HandleFunc("GET /api/internal/moderation/lease/", func(w http.ResponseWriter, r *http.Request) {
		d.mu.Lock()
		out := d.pending
		d.pending = nil
		d.mu.Unlock()
		_ = json.NewEncoder(w).Encode(leaseResponse{Leases: out})
	})

	mux.HandleFunc("POST /api/internal/moderation/settle/", func(w http.ResponseWriter, r *http.Request) {
		var req SettleRequest
		_ = json.NewDecoder(r.Body).Decode(&req)
		d.mu.Lock()
		d.settled = append(d.settled, req)
		d.mu.Unlock()
		_, _ = w.Write([]byte(`{"applied":true}`))
	})

	d.server = httptest.NewServer(mux)
	t.Cleanup(d.server.Close)
	return d
}

func (d *fakeDjango) offer(l Lease) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.pending = append(d.pending, l)
}

func (d *fakeDjango) results() []SettleRequest {
	d.mu.Lock()
	defer d.mu.Unlock()
	out := make([]SettleRequest, len(d.settled))
	copy(out, d.settled)
	return out
}

func lease(id int64, source string, stamps ...int) Lease {
	return Lease{
		JobID: id, SwiftID: id, SourceURL: source,
		TimestampsMS: stamps, DurationMS: 15000, FrameWidth: 640,
		Prompt: "classify this", VisionModel: "@cf/test/vision",
		TranscribeModel: "@cf/test/whisper",
	}
}

func newWorker(t *testing.T, cf *fakeCloudflare, dj *fakeDjango, maxJobs, maxFrames, rate int) *Worker {
	t.Helper()
	limiter := NewRateLimiter(rate, rate)
	t.Cleanup(limiter.Close)
	// `.WithAPIRoot` is the whole reason this suite is offline. Without it the
	// Workers AI base is the package default and every classify call goes to
	// the real api.cloudflare.com — which is exactly what happened the first
	// time this was run, and why three tests were asserting nothing.
	client := NewCloudflare("acct", "token", cf.server.URL, 10*time.Second, limiter).
		WithAPIRoot(cf.server.URL)
	django := NewDjangoClient(dj.server.URL, "worker-token", 10*time.Second, discardLogger())
	return NewWorker(client, django, discardLogger(), maxJobs, maxFrames, 30*time.Second)
}

// ─── The fan-out ─────────────────────────────────────────────────────────────

func TestEveryTimestampIsClassified(t *testing.T) {
	cf := newFakeCloudflare(t)
	dj := newFakeDjango(t)
	w := newWorker(t, cf, dj, 4, 6, 1000)

	w.runJob(context.Background(), lease(1, "https://cdn.example/v.mp4", 0, 1000, 2000, 3000, 4000))

	settled := dj.results()
	if len(settled) != 1 {
		t.Fatalf("settled %d jobs, want 1", len(settled))
	}
	if got := len(settled[0].Frames); got != 5 {
		t.Fatalf("reported %d frames, want 5", got)
	}
	// Order must match the requested timestamps — Django pairs them
	// positionally when it rebuilds the results.
	for i, want := range []int{0, 1000, 2000, 3000, 4000} {
		if settled[0].Frames[i].AtMS != want {
			t.Errorf("frame %d at_ms = %d, want %d", i, settled[0].Frames[i].AtMS, want)
		}
	}
}

func TestFramesAreClassifiedConcurrently(t *testing.T) {
	// The whole justification for this service. Sequentially, ten 100ms frames
	// take a second; concurrently they take ~100ms.
	cf := newFakeCloudflare(t)
	cf.delay = 80 * time.Millisecond
	dj := newFakeDjango(t)
	w := newWorker(t, cf, dj, 4, 8, 1000)

	stamps := make([]int, 8)
	for i := range stamps {
		stamps[i] = i * 1000
	}

	start := time.Now()
	w.runJob(context.Background(), lease(1, "https://cdn.example/v.mp4", stamps...))
	elapsed := time.Since(start)

	// Sequential would be at least 8 × (80ms fetch + 80ms classify) = 1.28s.
	// Generous ceiling so this does not flake on a loaded CI box; the point is
	// the order of magnitude, not the number.
	if elapsed > 900*time.Millisecond {
		t.Errorf("8 frames took %v — that looks sequential", elapsed)
	}
	if peak := cf.peak(); peak < 2 {
		t.Errorf("peak concurrent classify calls = %d, want > 1", peak)
	}
}

func TestFrameConcurrencyIsCapped(t *testing.T) {
	// The ceiling exists so a long video cannot open twelve simultaneous
	// inference calls and blow the rate limit in one burst.
	cf := newFakeCloudflare(t)
	cf.delay = 50 * time.Millisecond
	dj := newFakeDjango(t)
	w := newWorker(t, cf, dj, 1, 3, 1000)

	stamps := make([]int, 12)
	for i := range stamps {
		stamps[i] = i * 500
	}
	w.runJob(context.Background(), lease(1, "https://cdn.example/v.mp4", stamps...))

	if peak := cf.peak(); peak > 3 {
		t.Errorf("peak concurrency = %d, want <= 3", peak)
	}
}

// ─── Failure handling ────────────────────────────────────────────────────────

func TestFailedFramesAreReportedNotDropped(t *testing.T) {
	// Django counts requested-but-unclassified frames to decide whether enough
	// of the video was seen. Silently omitting them would make a barely-sampled
	// video look fully sampled — which is the fail-open bug this whole design
	// is arranged to prevent.
	cf := newFakeCloudflare(t)
	cf.failFrames = true
	dj := newFakeDjango(t)
	w := newWorker(t, cf, dj, 4, 6, 1000)

	w.runJob(context.Background(), lease(1, "https://cdn.example/v.mp4", 0, 1000, 2000))

	settled := dj.results()
	if len(settled) != 1 {
		t.Fatalf("nothing was settled")
	}
	if len(settled[0].Frames) != 3 {
		t.Fatalf("reported %d frames, want all 3", len(settled[0].Frames))
	}
	for i, f := range settled[0].Frames {
		if f.Error == "" {
			t.Errorf("frame %d has no error despite the fetch failing", i)
		}
		if f.Raw != "" {
			t.Errorf("frame %d carries model output it never got", i)
		}
	}
}

func TestModelOutputIsPassedThroughUnparsed(t *testing.T) {
	// Prose around the JSON is a thing models really do. This service must not
	// try to clean it up — `parse_vision_response` on the Django side has the
	// tests for that, and a second parser here would be a second set of bugs.
	cf := newFakeCloudflare(t)
	cf.response = "Sure! Here you go: {\"unsafe\": true, \"confidence\": 0.8}"
	dj := newFakeDjango(t)
	w := newWorker(t, cf, dj, 4, 6, 1000)

	w.runJob(context.Background(), lease(1, "https://cdn.example/v.mp4", 0))

	got := dj.results()[0].Frames[0].Raw
	if got != cf.response {
		t.Errorf("raw = %q, want it byte-identical to what the model said", got)
	}
}

func TestAnUnusableLeaseIsReportedAsNonRetryable(t *testing.T) {
	cf := newFakeCloudflare(t)
	dj := newFakeDjango(t)
	w := newWorker(t, cf, dj, 4, 6, 1000)

	// Relative URL: Cloudflare cannot fetch it, and it will be just as
	// unfetchable next time.
	w.runJob(context.Background(), lease(1, "/media/v.mp4", 0))

	settled := dj.results()
	if len(settled) != 1 {
		t.Fatalf("an unusable lease was not reported back")
	}
	if settled[0].Error == "" {
		t.Error("no error reported")
	}
	if settled[0].Retryable {
		t.Error("a malformed lease was reported as retryable; it will never succeed")
	}
}

func TestNoFramesAreFetchedForAnInvalidLease(t *testing.T) {
	cf := newFakeCloudflare(t)
	dj := newFakeDjango(t)
	w := newWorker(t, cf, dj, 4, 6, 1000)

	w.runJob(context.Background(), lease(1, "not-a-url", 0, 1000))

	if n := cf.frameCalls.Load(); n != 0 {
		t.Errorf("made %d frame calls for a lease that could never work", n)
	}
}

// ─── The credential ──────────────────────────────────────────────────────────

func TestTheCloudflareTokenIsNeverReportedBack(t *testing.T) {
	// `SettleRequest.Error` is stored in `last_error` on a database row a
	// moderator can read. A provider message echoing a bearer token would put
	// a credential in Postgres.
	const token = "cf-token-NEVER-REAL-0000"

	limiter := NewRateLimiter(100, 100)
	defer limiter.Close()

	// A server that echoes the Authorization header back in its error body —
	// the worst-case provider behaviour.
	echo := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
		fmt.Fprintf(w, "rejected: %s", r.Header.Get("Authorization"))
	}))
	defer echo.Close()

	cf := NewCloudflare("acct", token, echo.URL, 5*time.Second, limiter).WithAPIRoot(echo.URL)
	_, err := cf.ClassifyFrame(context.Background(), "@cf/test/vision", "p", []byte{1, 2, 3})
	if err == nil {
		t.Fatal("expected an error")
	}
	if strings.Contains(err.Error(), token) {
		t.Errorf("the token leaked into an error message: %v", err)
	}
	if !strings.Contains(err.Error(), "[redacted]") {
		t.Errorf("expected the token to be redacted, got: %v", err)
	}
}

// ─── The poll loop ───────────────────────────────────────────────────────────

func TestRunLeasesAndSettles(t *testing.T) {
	cf := newFakeCloudflare(t)
	dj := newFakeDjango(t)
	dj.offer(lease(1, "https://cdn.example/a.mp4", 0, 1000))
	dj.offer(lease(2, "https://cdn.example/b.mp4", 0, 1000))
	w := newWorker(t, cf, dj, 4, 6, 1000)

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	w.Run(ctx, 20*time.Millisecond)

	if got := len(dj.results()); got != 2 {
		t.Errorf("settled %d jobs, want 2", got)
	}
}

func TestRunSurvivesDjangoBeingDown(t *testing.T) {
	// Django restarts on every deploy. A worker that died on a failed lease
	// would need supervision to come back; one that logs and retries does not.
	cf := newFakeCloudflare(t)
	dj := newFakeDjango(t)
	dj.server.Close() // every lease call now fails

	w := newWorker(t, cf, dj, 4, 6, 1000)
	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()

	w.Run(ctx, 20*time.Millisecond) // must return on ctx, not panic or hang

	if w.Snapshot().Leased != 0 {
		t.Error("leased something from a server that is not running")
	}
}
