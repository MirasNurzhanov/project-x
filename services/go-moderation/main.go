// Command go-moderation classifies Swift video frames for the Approx Django
// backend.
//
// Django leases it a claimed job, it fetches and classifies the sample frames
// concurrently, and it hands the raw model answers back. Django decides what
// they mean.
//
// Run it alongside — or instead of — `manage.py moderate_swifts --loop`. The
// two are safe together: both lease through the same atomic claim, so a job
// goes to exactly one of them.
package main

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"
)

type Config struct {
	Addr          string
	DjangoURL     string
	DjangoToken   string
	AccountID     string
	CFToken       string
	TransformBase string
	PollInterval  time.Duration
	MaxJobs       int
	MaxFrames     int
	RatePerSec    int
	RateBurst     int
	HTTPTimeout   time.Duration
	JobBudget     time.Duration
	Debug         bool
}

func loadConfig() Config {
	return Config{
		Addr: env("MOD_ADDR", ":8081"),

		// Inside compose this is the Django service name; on Railway it is the
		// internal hostname. Defaulted so `go run .` against a local
		// `manage.py runserver` works with no environment at all.
		DjangoURL:   env("DJANGO_BASE_URL", "http://127.0.0.1:8000"),
		DjangoToken: env("MODERATION_WORKER_TOKEN", ""),

		// The same two variables Django reads. Shared deliberately: one secret,
		// one place to rotate it.
		AccountID: env("CLOUDFLARE_ACCOUNT_ID", ""),
		CFToken:   env("CLOUDFLARE_API_TOKEN", ""),

		// Blank falls back to whatever Django puts in the lease payload's
		// source URL host, which is wrong often enough to be worth setting
		// explicitly. See the README on why r2.dev will not work here.
		TransformBase: env("CLOUDFLARE_MEDIA_TRANSFORM_BASE", ""),

		PollInterval: time.Duration(envInt("MOD_POLL_INTERVAL_S", 5)) * time.Second,

		// Concurrency ceilings. The product of these two is the worst case in
		// flight at Cloudflare — 4 × 6 = 24 — but the rate limiter below is
		// what actually bounds the spend.
		MaxJobs:   envInt("MOD_MAX_JOBS", 4),
		MaxFrames: envInt("MOD_MAX_FRAMES", 6),

		// Shared across every job. Conservative on purpose: being throttled by
		// Workers AI costs more than being slightly slow.
		RatePerSec: envInt("MOD_RATE_PER_SEC", 8),
		RateBurst:  envInt("MOD_RATE_BURST", 16),

		HTTPTimeout: time.Duration(envInt("MOD_HTTP_TIMEOUT_S", 30)) * time.Second,

		// Must stay well under Django's SWIFT_AI_STALE_AFTER_S (900s default),
		// or a job this service is still working on gets re-leased to somebody
		// else and the work is done twice — and billed twice.
		JobBudget: time.Duration(envInt("MOD_JOB_BUDGET_S", 180)) * time.Second,

		Debug: env("MOD_DEBUG", "") != "",
	}
}

func main() {
	cfg := loadConfig()

	level := slog.LevelInfo
	if cfg.Debug {
		level = slog.LevelDebug
	}
	log := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: level}))

	// Warn rather than refuse to start. The failure this produces is safe —
	// every job fails, Django leaves the Swifts PENDING, and PENDING is
	// invisible — so a loud log line beats a crash loop that also takes out
	// the health endpoint you would use to diagnose it.
	if cfg.AccountID == "" || cfg.CFToken == "" {
		log.Warn("cloudflare credentials are not set; every job will fail closed")
	}
	if cfg.DjangoToken == "" {
		log.Warn("MODERATION_WORKER_TOKEN is not set; Django will reject the lease if it expects one")
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	limiter := NewRateLimiter(cfg.RatePerSec, cfg.RateBurst)
	defer limiter.Close()

	cf := NewCloudflare(cfg.AccountID, cfg.CFToken, cfg.TransformBase, cfg.HTTPTimeout, limiter)
	django := NewDjangoClient(cfg.DjangoURL, cfg.DjangoToken, 20*time.Second, log)
	worker := NewWorker(cf, django, log, cfg.MaxJobs, cfg.MaxFrames, cfg.JobBudget)

	// The worker loop is the actual service. The HTTP server below exists only
	// so a container platform can probe it and so you can read the counters —
	// which is the opposite of go-push, where HTTP *is* the service. Worth
	// noticing: a Go service does not have to be a web server.
	workerDone := make(chan struct{})
	go func() {
		defer close(workerDone)
		worker.Run(ctx, cfg.PollInterval)
	}()

	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	mux.HandleFunc("GET /readyz", func(w http.ResponseWriter, _ *http.Request) {
		// Ready means "polling". There is no dependency check here on purpose:
		// Django being briefly unreachable is a normal state this service
		// recovers from, and failing readiness for it would just remove the
		// thing that is going to retry.
		w.WriteHeader(http.StatusOK)
	})
	mux.HandleFunc("GET /v1/stats", func(w http.ResponseWriter, r *http.Request) {
		if cfg.DjangoToken != "" && !bearerMatches(r, cfg.DjangoToken) {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(worker.Snapshot())
	})

	srv := &http.Server{
		Addr:              cfg.Addr,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      15 * time.Second,
		IdleTimeout:       60 * time.Second,
	}
	go func() {
		log.Info("started",
			"addr", cfg.Addr, "django", cfg.DjangoURL,
			"max_jobs", cfg.MaxJobs, "max_frames", cfg.MaxFrames,
			"rate_per_sec", cfg.RatePerSec)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Error("probe server failed", "error", err)
		}
	}()

	<-ctx.Done()
	log.Info("shutdown signal received", "in_flight", worker.Snapshot().InFlight)

	// Order matters, as always: stop taking probes, then wait for the jobs.
	//
	// The wait is bounded by MOD_JOB_BUDGET_S rather than being unbounded,
	// because a job abandoned here is *not* lost — Django's row is still
	// `running`, and `due_jobs` re-queues stale running rows. That safety net
	// is what lets shutdown be impatient.
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_ = srv.Shutdown(shutdownCtx)

	select {
	case <-workerDone:
		log.Info("drained cleanly", "stats", worker.Snapshot())
	case <-time.After(cfg.JobBudget):
		log.Warn("gave up waiting for in-flight jobs; Django will re-queue them as stale",
			"stats", worker.Snapshot())
	}
}

// ─── helpers ─────────────────────────────────────────────────────────────────

func env(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func envInt(key string, fallback int) int {
	v := os.Getenv(key)
	if v == "" {
		return fallback
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return fallback
	}
	return n
}

func bearerMatches(r *http.Request, want string) bool {
	const prefix = "Bearer "
	h := r.Header.Get("Authorization")
	if len(h) <= len(prefix) || h[:len(prefix)] != prefix {
		return false
	}
	return subtle.ConstantTimeCompare([]byte(h[len(prefix):]), []byte(want)) == 1
}
