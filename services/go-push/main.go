// Command approx-push is a push-notification dispatcher for the Approx Django
// backend.
//
// Django decides *who* should get a notification — preferences, blocks,
// cooldowns, all the real business logic in notifications/utils.py. This
// service decides *how fast the bytes reach Expo*: it accepts a batch over
// HTTP, answers immediately, and delivers it from a pool of workers with a
// rate limit and retries in front of the provider.
//
// That split is the point. Nothing here knows what a Swift or a club is, and
// nothing here needs the database.
//
// ─── Reading this file ───────────────────────────────────────────────────────
//
// `package main` plus `func main()` is what makes a Go module an executable
// rather than a library. There is one entry point, it is explicit, and there
// is no equivalent of Django's settings-module import graph deciding what runs
// — everything this process does is reachable by reading downward from here.
package main

import (
	"context"
	"crypto/subtle"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"
)

// Config is every knob, in one struct, read once at startup.
//
// The equivalent of core/settings.py, minus the import-time magic. Reading the
// environment in exactly one place means a misconfiguration is a startup
// failure with a clear message, rather than a `KeyError` from inside a request
// three hours later.
type Config struct {
	Addr         string
	ExpoURL      string
	Token        string
	DeadTokenURL string
	Workers      int
	QueueSize    int
	BatchSize    int
	RatePerSec   int
	RateBurst    int
	Tries        int
	HTTPTimeout  time.Duration
	DrainWait    time.Duration
	MaxBody      int64
	MaxBatch     int
	Debug        bool
}

func loadConfig() Config {
	return Config{
		// Defaults chosen so `go run .` works with no environment at all.
		// A service you cannot start without a wiki page is a service nobody
		// runs locally, and one nobody runs locally is one that breaks.
		Addr:    env("PUSH_ADDR", ":8080"),
		ExpoURL: env("EXPO_PUSH_URL", "https://exp.host/--/api/v2/push/send"),

		// No default. An empty token disables auth, which is right for local
		// development and wrong everywhere else — main() warns loudly about it
		// rather than inventing a secret that would be identical in every
		// deployment.
		Token:        env("PUSH_AUTH_TOKEN", ""),
		DeadTokenURL: env("PUSH_DEAD_TOKEN_URL", ""),

		// 8 workers against one provider is a starting point, not a
		// measurement. The number that actually matters is the rate limit
		// below; workers only need to be enough to keep it saturated.
		Workers:   envInt("PUSH_WORKERS", 8),
		QueueSize: envInt("PUSH_QUEUE_SIZE", 4096),
		BatchSize: envInt("PUSH_BATCH_SIZE", 100), // Expo's documented ceiling

		// Expo publishes no hard number; 30/s with a burst of 60 is deliberately
		// conservative. Being throttled costs more than being slightly slow.
		RatePerSec: envInt("PUSH_RATE_PER_SEC", 30),
		RateBurst:  envInt("PUSH_RATE_BURST", 60),

		Tries:       envInt("PUSH_MAX_ATTEMPTS", 3),
		HTTPTimeout: time.Duration(envInt("PUSH_HTTP_TIMEOUT_S", 10)) * time.Second,

		// Must be comfortably under the pod's terminationGracePeriodSeconds,
		// or Kubernetes SIGKILLs the process mid-drain and the queue is lost
		// anyway. See k8s/deployment.yaml, where the two are set together.
		DrainWait: time.Duration(envInt("PUSH_DRAIN_WAIT_S", 20)) * time.Second,

		MaxBody:  int64(envInt("PUSH_MAX_BODY_BYTES", 1<<20)), // 1 MiB
		MaxBatch: envInt("PUSH_MAX_BATCH", 1000),
		Debug:    env("PUSH_DEBUG", "") != "",
	}
}

func main() {
	cfg := loadConfig()

	// ─── Structured logging, stdlib ──────────────────────────────────────────
	//
	// `log/slog` landed in Go 1.21 and removed the last common reason to add a
	// logging dependency. JSON to stdout is what a container should emit:
	// the platform collects stdout, so writing files or configuring handlers —
	// everything Django's LOGGING dict does — is not this process's job.
	level := slog.LevelInfo
	if cfg.Debug {
		level = slog.LevelDebug
	}
	log := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: level}))

	if cfg.Token == "" {
		log.Warn("PUSH_AUTH_TOKEN is not set — the push endpoint is unauthenticated")
	}

	// ─── Context and signals ─────────────────────────────────────────────────
	//
	// A `context.Context` is a cancellation signal that flows down a call
	// tree. It has no Python equivalent you would reach for by habit — the
	// nearest thing is passing a `threading.Event` into everything and
	// checking it, which nobody does, which is why Python services are harder
	// to shut down cleanly.
	//
	// `signal.NotifyContext` cancels this one on SIGINT or SIGTERM. Because
	// the context reaches the workers and every outbound HTTP request, one
	// Ctrl-C or one `kubectl delete pod` unwinds the whole tree.
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	limiter := NewRateLimiter(cfg.RatePerSec, cfg.RateBurst)
	defer limiter.Close()

	expo := NewExpoClient(cfg.ExpoURL, cfg.HTTPTimeout, log, limiter, cfg.Tries)
	if cfg.DeadTokenURL != "" && cfg.Token != "" {
		cleanupClient := &http.Client{Timeout: 2 * time.Second,
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
		expo.OnDeadToken(func(token string) {
			if err := reportDeadToken(ctx, cleanupClient, cfg.DeadTokenURL, cfg.Token, token); err != nil {
				log.Warn("push_dead_token_cleanup_failed")
			} else {
				log.Info("push_dead_token_cleared")
			}
		})
	} else {
		log.Warn("push_dead_token_cleanup_not_configured")
	}

	dispatcher := NewDispatcher(expo, log, cfg.Workers, cfg.QueueSize, cfg.BatchSize)
	dispatcher.Start(ctx)

	srv := &http.Server{
		Addr: cfg.Addr,
		Handler: (&Server{
			dispatcher: dispatcher,
			log:        log,
			token:      cfg.Token,
			maxBody:    cfg.MaxBody,
			maxBatch:   cfg.MaxBatch,
		}).Routes(),

		// Timeouts nobody sets until they have been bitten. Without them a
		// client that opens a connection and sends nothing holds a goroutine
		// and a file descriptor indefinitely — the cheapest denial of service
		// there is. Go's zero value for these is "no timeout", so leaving them
		// out is a decision even when it does not feel like one.
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      15 * time.Second,
		IdleTimeout:       60 * time.Second,
	}

	// ListenAndServe blocks, so it goes on its own goroutine and reports back
	// through a channel. This is the standard shape for "run a server and also
	// wait for a signal".
	serverErr := make(chan error, 1) // buffered, so this send never blocks
	go func() {
		log.Info("listening", "addr", cfg.Addr,
			"workers", cfg.Workers, "queue", cfg.QueueSize,
			"rate_per_sec", cfg.RatePerSec)
		serverErr <- srv.ListenAndServe()
	}()

	// Wait for whichever comes first: the server falling over, or a signal.
	select {
	case err := <-serverErr:
		if err != nil && !isServerClosed(err) {
			log.Error("server failed", "error", err)
			os.Exit(1)
		}
	case <-ctx.Done():
		log.Info("shutdown signal received")
	}

	// ─── Draining, in the right order ────────────────────────────────────────
	//
	// The order is the whole trick, and getting it backwards is the usual bug:
	//
	//   1. Stop accepting new HTTP requests, but let in-flight ones finish.
	//      Anything already in a handler still gets to enqueue.
	//   2. *Then* close the queue and let the workers finish what is in it.
	//
	// Draining the workers first would leave the HTTP server accepting
	// requests it can no longer service. Stopping the listener first means the
	// only thing left to wait for is work already accepted.
	shutdownCtx, cancel := context.WithTimeout(context.Background(), cfg.DrainWait)
	defer cancel()

	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Error("http shutdown", "error", err)
	}
	if err := dispatcher.Shutdown(cfg.DrainWait); err != nil && !errors.Is(err, context.DeadlineExceeded) {
		log.Error("dispatcher shutdown", "error", err)
	}
	log.Info("stopped", "stats", dispatcher.Snapshot())
}

// ─── Small helpers ───────────────────────────────────────────────────────────

func env(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

// envInt reads an integer setting, falling back on anything unparseable.
//
// Deliberately forgiving rather than fatal: a typo'd worker count should start
// the service on its defaults, not refuse to boot a push dispatcher at 3am.
// The opposite call is right for a *credential*, which is why an empty
// PUSH_AUTH_TOKEN gets a loud warning instead of a silent default.
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

// constantTimeEqual compares two strings without leaking their contents
// through timing. See Server.authorised.
func constantTimeEqual(a, b string) bool {
	return subtle.ConstantTimeCompare([]byte(a), []byte(b)) == 1
}
