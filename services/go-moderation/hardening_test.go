package main

// Two properties that are easy to hold by accident and easy to lose the same
// way, plus a load test for the shared rate limit.
//
//   1. The Cloudflare token never reaches anything a moderator can read.
//      `SettleRequest.Error` and `FrameResult.Error` both land in
//      `SwiftModerationJob.last_error`, which is a database column shown in the
//      Django admin. Redaction on the happy path is not the property; redaction
//      on *every* path is.
//
//   2. The lease contract fails loudly when Django's payload moves. JSON
//      decoding ignores unknown fields by default, so a renamed field is a
//      silent behaviour change — the worst kind, because the service keeps
//      running and does the wrong amount of work.

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// theToken is deliberately shaped like a real Cloudflare API token: long,
// alphanumeric with hyphens and underscores. A short "secret" would pass tests
// that a realistic one fails, because the redaction pattern is character-class
// based.
const theToken = "v1.0-a1b2c3d4e5f6_NEVER-REAL-7890abcdef1234567890"

// ─── 1. The token never escapes ──────────────────────────────────────────────

// hostileServer answers every request in a way designed to leak the caller's
// own credential back at it. Real providers do not do this; the point is that
// the property must not depend on their good behaviour.
func hostileServer(t *testing.T, mode string) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		auth := r.Header.Get("Authorization")
		switch mode {
		case "400":
			w.WriteHeader(http.StatusBadRequest)
			fmt.Fprintf(w, `{"errors":[{"message":"bad request with %s"}]}`, auth)
		case "500":
			w.WriteHeader(http.StatusInternalServerError)
			fmt.Fprintf(w, "upstream exploded, sent %s", auth)
		case "success-false":
			// A 200 whose body says the call failed — Cloudflare's own shape.
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprintf(w, `{"success":false,"errors":[{"message":"denied for %s"}]}`, auth)
		case "malformed":
			// A 200 that is not JSON at all.
			fmt.Fprintf(w, "<html>gateway error for %s</html>", auth)
		case "empty-response":
			// Valid envelope, no text — the "model said nothing" path.
			fmt.Fprint(w, `{"success":true,"result":{"response":""}}`)
		case "hang":
			// Answers late enough that the *client* times out first, which is
			// the path under test — but bounded, so the handler always returns.
			//
			// An unbounded `<-r.Context().Done()` here wedged the entire suite:
			// `httptest.Server.Close` waits for outstanding handlers, and
			// `t.Cleanup` calls it. A test server that can block forever is a
			// test binary that can block forever.
			select {
			case <-r.Context().Done():
			case <-time.After(2 * time.Second):
			}
		}
	}))
	t.Cleanup(srv.Close)
	return srv
}

func TestTheTokenNeverLeaksOnAnyErrorPath(t *testing.T) {
	// Every failure mode the client can hit, not just the one that was easy to
	// write. Each returns an error that ends up in `last_error` in Postgres.
	for _, mode := range []string{
		"400", "500", "success-false", "malformed", "empty-response", "hang",
	} {
		t.Run(mode, func(t *testing.T) {
			limiter := NewRateLimiter(1000, 1000)
			defer limiter.Close()

			srv := hostileServer(t, mode)
			timeout := 5 * time.Second
			if mode == "hang" {
				timeout = 150 * time.Millisecond // so the test does not wait 5s
			}
			cf := NewCloudflare("acct-id", theToken, srv.URL, timeout, limiter).
				WithAPIRoot(srv.URL)

			raw, err := cf.ClassifyFrame(
				context.Background(), "@cf/test/vision", "prompt", []byte{1, 2, 3})
			if mode == "empty-response" {
				// Preserve the invalid answer for Django's validation gate.
				if err != nil || raw != `""` {
					t.Fatalf("empty response was not preserved: raw=%q err=%v", raw, err)
				}
				assertNoToken(t, raw)
				return
			}
			if err == nil {
				t.Fatalf("mode %q produced no error; nothing to check", mode)
			}
			assertNoToken(t, err.Error())
		})
	}
}

func TestTheTokenNeverLeaksThroughTheFrameFetchPath(t *testing.T) {
	// Media Transformations sends no Authorization header, so there should be
	// nothing to leak — but the response body is echoed into the error, and
	// "there should be nothing" is not a property worth resting a credential on.
	limiter := NewRateLimiter(1000, 1000)
	defer limiter.Close()

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		// A caching layer that helpfully reflects headers is not hypothetical.
		fmt.Fprintf(w, "denied; upstream said Bearer %s", theToken)
	}))
	defer srv.Close()

	cf := NewCloudflare("acct-id", theToken, srv.URL, 5*time.Second, limiter)
	_, err := cf.FetchFrame(context.Background(), "https://cdn.example/v.mp4", 0, 640)
	if err == nil {
		t.Fatal("expected an error")
	}
	assertNoToken(t, err.Error())
}

func TestTheTokenNeverReachesTheSettlePayload(t *testing.T) {
	// The end-to-end version: run a whole job against a hostile provider and
	// inspect the JSON that would be POSTed to Django — which is the exact
	// bytes that become `last_error`.
	cfSrv := hostileServer(t, "500")
	dj := newFakeDjango(t)

	limiter := NewRateLimiter(1000, 1000)
	defer limiter.Close()
	cf := NewCloudflare("acct-id", theToken, cfSrv.URL, 5*time.Second, limiter).
		WithAPIRoot(cfSrv.URL)
	django := NewDjangoClient(dj.server.URL, "worker-token", 5*time.Second, discardLogger())
	w := NewWorker(cf, django, discardLogger(), 2, 4, 20*time.Second)

	w.runJob(context.Background(), lease(1, "https://cdn.example/v.mp4", 0, 1000))

	settled := dj.results()
	if len(settled) != 1 {
		t.Fatalf("settled %d jobs, want 1", len(settled))
	}
	// Marshal the whole struct — not just the fields we remembered to check.
	// A future field carrying an unredacted provider string fails here.
	raw, err := json.Marshal(settled[0])
	if err != nil {
		t.Fatalf("marshalling settle payload: %v", err)
	}
	assertNoToken(t, string(raw))
}

// assertNoToken is the single assertion, so a new path cannot check a weaker
// version of the property by accident.
func assertNoToken(t *testing.T, s string) {
	t.Helper()
	if strings.Contains(s, theToken) {
		t.Fatalf("the API token leaked:\n%s", s)
	}
	// Substrings too: a truncation that cut the token in half would still
	// publish most of it.
	if len(theToken) > 16 && strings.Contains(s, theToken[:16]) {
		t.Fatalf("a prefix of the API token leaked:\n%s", s)
	}
}

// ─── 2. The lease contract canary ────────────────────────────────────────────

// TestLeaseDecodingRejectsAnUnknownField is the canary.
//
// ─── When this test fails, read this ─────────────────────────────────────────
//
// It fails because Django's lease payload gained or renamed a field and this
// service has not been updated. That is the point: without it, an added field
// is ignored silently and a *renamed* one silently becomes a zero value —
// `timestamps_ms` renamed to `sample_points` would leave `TimestampsMS` empty
// and every job would report zero frames classified.
//
// The fix is to add the field to `Lease` in types.go and extend
// `canaryLeasePayload` below. Do not delete the test.
func TestLeaseDecodingRejectsAnUnknownField(t *testing.T) {
	payload := []byte(`{"leases":[{
		"job_id": 1, "swift_id": 2,
		"source_url": "https://cdn.example/v.mp4",
		"timestamps_ms": [0, 1000],
		"duration_ms": 15000, "want_audio": false, "frame_width": 640,
		"prompt": "p", "vision_model": "v", "transcribe_model": "t",
		"a_field_this_build_has_never_heard_of": true
	}]}`)

	var into leaseResponse
	err := strictDecode(payload, &into)
	if err == nil {
		t.Fatal("strictDecode accepted an unknown field; contract drift would be silent")
	}
	if !strings.Contains(err.Error(), "unknown field") {
		t.Errorf("expected an unknown-field error, got: %v", err)
	}
}

// canaryLeasePayload is the current contract, written out by hand.
//
// Hand-written rather than produced by marshalling `Lease`, deliberately: a
// round-trip through the struct would agree with itself no matter what the
// struct said, and prove nothing. This is a literal transcription of what
// `swifts/worker_views.py::ModerationLeaseView._describe` returns.
//
// **When the Django contract changes, change this string.**
const canaryLeasePayload = `{"leases":[{
	"job_id": 42,
	"swift_id": 7,
	"source_url": "https://cdn.example/v.mp4",
	"timestamps_ms": [750, 2250, 3750],
	"duration_ms": 15000,
	"want_audio": true,
	"frame_width": 640,
	"prompt": "classify this frame",
	"vision_model": "@cf/meta/llama-3.2-11b-vision-instruct",
	"transcribe_model": "@cf/openai/whisper"
}]}`

func TestTheCurrentContractDecodesStrictly(t *testing.T) {
	// The other half of the canary: today's payload must pass strict decoding.
	// If this fails, `Lease` has a field Django does not send, or vice versa.
	var into leaseResponse
	if err := strictDecode([]byte(canaryLeasePayload), &into); err != nil {
		t.Fatalf("the documented contract does not decode strictly: %v", err)
	}
	if len(into.Leases) != 1 {
		t.Fatalf("decoded %d leases, want 1", len(into.Leases))
	}

	got := into.Leases[0]
	// Every field asserted, so a *renamed* field — which strict decoding on its
	// own would catch, but a lenient fallback would not — still fails here.
	for _, c := range []struct {
		name string
		ok   bool
	}{
		{"job_id", got.JobID == 42},
		{"swift_id", got.SwiftID == 7},
		{"source_url", got.SourceURL == "https://cdn.example/v.mp4"},
		{"timestamps_ms", len(got.TimestampsMS) == 3 && got.TimestampsMS[0] == 750},
		{"duration_ms", got.DurationMS == 15000},
		{"want_audio", got.WantAudio},
		{"frame_width", got.FrameWidth == 640},
		{"prompt", got.Prompt == "classify this frame"},
		{"vision_model", got.VisionModel == "@cf/meta/llama-3.2-11b-vision-instruct"},
		{"transcribe_model", got.TranscribeModel == "@cf/openai/whisper"},
	} {
		if !c.ok {
			t.Errorf("field %q did not survive decoding — has Django renamed it?", c.name)
		}
	}
}

func TestAnUnknownFieldIsCountedAndTheWorkStillHappens(t *testing.T) {
	// The production behaviour: loud, counted, but not an outage. A
	// backwards-compatible Django addition must not stop moderation.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"leases":[{
			"job_id": 1, "swift_id": 1,
			"source_url": "https://cdn.example/v.mp4",
			"timestamps_ms": [0],
			"duration_ms": 1000, "want_audio": false, "frame_width": 640,
			"prompt": "p", "vision_model": "v", "transcribe_model": "t",
			"brand_new_django_field": "added in a later release"
		}]}`)
	}))
	defer srv.Close()

	c := NewDjangoClient(srv.URL, "t", 5*time.Second, discardLogger())
	leases, err := c.Lease(context.Background(), 1)
	if err != nil {
		t.Fatalf("an added field broke the lease entirely: %v", err)
	}
	if len(leases) != 1 {
		t.Fatalf("got %d leases, want 1 — the fallback decode did not run", len(leases))
	}
	if leases[0].JobID != 1 || len(leases[0].TimestampsMS) != 1 {
		t.Error("the fallback decode lost fields it should have kept")
	}
	if c.Drift() != 1 {
		t.Errorf("drift counter = %d, want 1 — drift would be invisible in metrics", c.Drift())
	}
}

func TestARenamedCriticalFieldFailsTheJobRatherThanSilentlyDoingNothing(t *testing.T) {
	// The failure mode the canary exists to prevent, verified end to end:
	// `timestamps_ms` renamed means no frames, and no frames must not look like
	// a clean video.
	renamed := Lease{
		JobID: 1, SwiftID: 1, SourceURL: "https://cdn.example/v.mp4",
		TimestampsMS: nil, // what a rename produces
	}
	if err := renamed.Validate(); err == nil {
		t.Fatal("a lease with no timestamps validated; zero frames would be reported as a finished job")
	}
}

// ─── 3. The shared token bucket, under production shape ──────────────────────

// TestSharedRateLimitHoldsUnderProductionConcurrency drives the limiter the way
// the service actually drives it: MOD_MAX_JOBS jobs, each fanning out to
// MOD_MAX_FRAMES frames, all passing one bucket.
//
// ─── The number this establishes ─────────────────────────────────────────────
//
// The limiter is deliberately **global, not per job**. So the aggregate rate is
// MOD_RATE_PER_SEC — *not* MOD_RATE_PER_SEC × MOD_MAX_JOBS. Four jobs each
// politely limiting themselves to 8/s would be 32/s at Cloudflare, and
// preventing exactly that multiplication is what the shared bucket is for.
func TestSharedRateLimitHoldsUnderProductionConcurrency(t *testing.T) {
	if testing.Short() {
		t.Skip("timing-sensitive; skipped under -short")
	}

	const (
		jobs      = 4  // MOD_MAX_JOBS
		frames    = 6  // MOD_MAX_FRAMES
		perSecond = 8  // MOD_RATE_PER_SEC
		burst     = 16 // MOD_RATE_BURST
		duration  = 3 * time.Second
	)

	rl := NewRateLimiter(perSecond, burst)
	defer rl.Close()

	ctx, cancel := context.WithTimeout(context.Background(), duration)
	defer cancel()

	var calls atomic.Int64
	var wg sync.WaitGroup

	start := time.Now()
	// `jobs` concurrent jobs, each with `frames` workers pulling from the one
	// shared bucket until the window closes. Long-lived workers rather than a
	// goroutine per call: it is what `classifyFrames` actually does once the
	// semaphore is saturated, and it keeps the test from spending its time in
	// the scheduler instead of in the limiter.
	for j := 0; j < jobs; j++ {
		for f := 0; f < frames; f++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				for {
					if err := rl.Wait(ctx); err != nil {
						return // window closed
					}
					calls.Add(1)
				}
			}()
		}
	}
	wg.Wait()
	elapsed := time.Since(start)

	total := calls.Load()
	observed := float64(total) / elapsed.Seconds()

	// The burst is handed out immediately, so short windows read high. Subtract
	// it to get the sustained rate, which is the number that matters for a bill.
	sustained := float64(total-burst) / elapsed.Seconds()

	t.Logf("aggregate: %d calls in %v", total, elapsed.Round(time.Millisecond))
	t.Logf("observed rate:  %.2f/s (including the initial burst of %d)", observed, burst)
	t.Logf("sustained rate: %.2f/s (target %d/s)", sustained, perSecond)
	t.Logf("shape: %d jobs x %d frames = %d goroutines competing for one bucket",
		jobs, frames, jobs*frames)

	// Generous bounds: this asserts the *ceiling holds*, not a stopwatch
	// reading. A tighter assertion is a flaky test on a loaded CI box.
	if sustained > float64(perSecond)*1.5 {
		t.Errorf("sustained rate %.2f/s exceeds the %d/s ceiling by more than 50%%; "+
			"the bucket is not shared across jobs", sustained, perSecond)
	}
	if sustained < float64(perSecond)*0.5 {
		t.Errorf("sustained rate %.2f/s is far under the %d/s target; "+
			"the limiter is over-throttling", sustained, perSecond)
	}
	// The multiplication the shared bucket exists to prevent.
	if observed > float64(perSecond*jobs) {
		t.Errorf("observed %.2f/s >= per-job rate x jobs (%d/s): "+
			"concurrency is multiplying the request rate", observed, perSecond*jobs)
	}
}
