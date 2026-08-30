package main

// The inbound half: the HTTP surface Django talks to.

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"time"
)

// Server holds what the handlers need.
//
// ─── Where Django would use module-level globals ─────────────────────────────
//
// A Django view reaches for `settings.FOO` and imported module state. The Go
// convention is to put shared dependencies on a struct and hang the handlers
// off it as methods, so what a handler can touch is visible in its receiver.
// The practical payoff is testing: a test builds a `Server` with a fake
// dispatcher and calls the handler directly, with no global to patch and no
// import-order surprises.
type Server struct {
	dispatcher *Dispatcher
	log        *slog.Logger
	token      string // shared secret; empty disables the check
	maxBody    int64
	maxBatch   int
}

// Routes returns the HTTP handler for the whole service.
//
// `http.ServeMux` is the standard library's router. Since Go 1.22 it
// understands method and wildcard patterns ("POST /v1/push", "/things/{id}"),
// which is most of what a small service wanted a third-party router for. This
// is why the dependency list is empty.
func (s *Server) Routes() http.Handler {
	mux := http.NewServeMux()

	mux.HandleFunc("POST /v1/push", s.handlePush)
	mux.HandleFunc("GET /v1/stats", s.handleStats)

	// Two probes, not one, and they mean different things — Kubernetes uses
	// them for opposite purposes:
	//
	//   liveness  "is this process wedged?" A failure gets the pod *killed*.
	//   readiness "should this pod receive traffic right now?" A failure just
	//             takes it out of the Service's endpoint list.
	//
	// Wiring liveness to anything that can fail transiently — a dependency
	// check, queue depth — is a classic way to turn a slow downstream into a
	// crash loop. So liveness here is deliberately trivial, and readiness is
	// the one that can say no.
	mux.HandleFunc("GET /healthz", s.handleHealth)
	mux.HandleFunc("GET /readyz", s.handleReady)

	return s.withLogging(mux)
}

// handlePush is the endpoint Django calls.
//
// The contract, and the whole reason this is worth building: it does the least
// possible work and returns. Parse, validate, hand to the queue, answer 202.
// Everything expensive — the network calls to Expo — happens on a goroutine
// this request never waits for.
func (s *Server) handlePush(w http.ResponseWriter, r *http.Request) {
	if !s.authorised(r) {
		s.writeError(w, http.StatusUnauthorized, "invalid or missing bearer token")
		return
	}

	// `MaxBytesReader` caps the body *and* makes the failure legible: reading
	// past the limit returns an error rather than silently truncating. A bare
	// `io.ReadAll(r.Body)` on a public endpoint is an unbounded allocation
	// chosen by whoever is calling you.
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, s.maxBody))
	if err != nil {
		s.writeError(w, http.StatusRequestEntityTooLarge,
			fmt.Sprintf("body exceeds %d bytes", s.maxBody))
		return
	}

	var req PushRequest
	if err := decodeJSON(body, &req); err != nil {
		s.writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	if len(req.Messages) == 0 {
		s.writeError(w, http.StatusBadRequest, "messages must not be empty")
		return
	}
	if len(req.Messages) > s.maxBatch {
		s.writeError(w, http.StatusBadRequest,
			fmt.Sprintf("at most %d messages per request", s.maxBatch))
		return
	}

	// Validate per message and keep the good ones.
	//
	// Partial acceptance rather than all-or-nothing: one stale token in a
	// club-wide announcement should not cost the other 59 members their push.
	// The response says how many were dropped and why, so the caller can act
	// if it wants to.
	valid := make([]Message, 0, len(req.Messages))
	var problems []string
	for i, m := range req.Messages {
		if err := m.Validate(); err != nil {
			problems = append(problems, fmt.Sprintf("messages[%d]: %v", i, err))
			continue
		}
		valid = append(valid, m)
	}

	resp := PushResponse{Rejected: len(problems), Errors: problems}

	if len(valid) > 0 {
		accepted := s.dispatcher.Enqueue(Job{
			Messages: valid,
			Key:      req.IdempotencyKey,
			Received: time.Now(),
		})
		if !accepted {
			// 503 with Retry-After, not 500: this is "come back later", and
			// saying so precisely is what lets Django's client distinguish a
			// full queue from a broken service. A 500 would invite a retry
			// storm against a service already shedding load.
			w.Header().Set("Retry-After", "1")
			s.writeError(w, http.StatusServiceUnavailable, "queue full")
			return
		}
		resp.Queued = len(valid)
	}

	// 202 Accepted, not 200 OK. The distinction is real and worth respecting:
	// 200 would claim the pushes were sent, and at this instant they are
	// sitting in a channel. 202 says "I have taken responsibility for this",
	// which is exactly true.
	s.writeJSON(w, http.StatusAccepted, resp)
}

func (s *Server) handleStats(w http.ResponseWriter, r *http.Request) {
	if !s.authorised(r) {
		s.writeError(w, http.StatusUnauthorized, "invalid or missing bearer token")
		return
	}
	s.writeJSON(w, http.StatusOK, s.dispatcher.Snapshot())
}

// handleHealth answers "is this process alive". Nothing more.
func (s *Server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	w.WriteHeader(http.StatusOK)
	_, _ = io.WriteString(w, "ok\n")
}

// handleReady answers "should I be sent traffic".
//
// Says no when the queue is essentially full, which takes this pod out of the
// load balancer's rotation until it catches up. With more than one replica
// that is genuinely useful — traffic shifts to the pod that can take it. With
// one replica it just makes the outage visible, which is still better than
// silently shedding.
func (s *Server) handleReady(w http.ResponseWriter, _ *http.Request) {
	st := s.dispatcher.Snapshot()
	if st.QueueCap > 0 && st.QueueDepth >= st.QueueCap {
		s.writeJSON(w, http.StatusServiceUnavailable, map[string]any{
			"ready": false, "reason": "queue full", "depth": st.QueueDepth,
		})
		return
	}
	s.writeJSON(w, http.StatusOK, map[string]any{"ready": true})
}

// authorised checks the shared secret.
//
// A bearer token, not mTLS or a signature, because of where this sits: inside
// the cluster, reachable only through a ClusterIP Service, called by one
// client you control. The threat it defends against is "something else in the
// namespace can send your users push notifications", and a shared secret in an
// env var is proportionate to that.
//
// It is compared in constant time. Comparing secrets with `==` leaks their
// length and, in principle, their content through timing. It costs one import
// to not have that conversation.
func (s *Server) authorised(r *http.Request) bool {
	if s.token == "" {
		return true // explicitly disabled, e.g. for local development
	}
	const prefix = "Bearer "
	h := r.Header.Get("Authorization")
	if len(h) <= len(prefix) || h[:len(prefix)] != prefix {
		return false
	}
	return constantTimeEqual(h[len(prefix):], s.token)
}

// writeJSON is the one place a response body is produced.
func (s *Server) writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(payload); err != nil {
		// The header is already written, so there is nothing to tell the
		// client — the connection is the only thing left to fail. Log it and
		// move on; returning an error here would have nowhere to go.
		//
		// `errors.Is(err, syscall.EPIPE)` would let this ignore the ordinary
		// "client hung up" case; left simple deliberately.
		s.log.Error("writing response", "error", err)
	}
}

func (s *Server) writeError(w http.ResponseWriter, status int, msg string) {
	s.writeJSON(w, status, map[string]string{"error": msg})
}

// withLogging wraps a handler with one structured log line per request.
//
// ─── Middleware, coming from Django ──────────────────────────────────────────
//
// Django middleware is a class with `__call__` that takes `get_response`. Go
// middleware is a function taking an `http.Handler` and returning another one.
// Same idea, less ceremony: `http.Handler` is a one-method interface, so
// wrapping is just building a new one that calls the old one.
func (s *Server) withLogging(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// The probes fire every few seconds forever. Logging them buries
		// everything else and costs money in whatever ingests the logs.
		if r.URL.Path == "/healthz" || r.URL.Path == "/readyz" {
			next.ServeHTTP(w, r)
			return
		}

		start := time.Now()
		rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(rec, r)

		s.log.Info("request",
			"method", r.Method,
			"path", r.URL.Path,
			"status", rec.status,
			"duration_ms", time.Since(start).Milliseconds())
	})
}

// statusRecorder captures the status code on its way out.
//
// `http.ResponseWriter` does not expose what status was written, so the only
// way to log it is to wrap it. Embedding the interface (the field with no
// name) means this type gets every method of `ResponseWriter` for free and
// only overrides the one it cares about — Go's substitute for inheritance, and
// the one place it genuinely feels like it.
type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (r *statusRecorder) WriteHeader(code int) {
	r.status = code
	r.ResponseWriter.WriteHeader(code)
}

// errServerClosed re-exports the sentinel main.go checks for, so the shutdown
// path there reads without importing net/http for one constant.
var errServerClosed = http.ErrServerClosed

// isServerClosed reports whether an error is the ordinary "we called Shutdown"
// signal rather than a real failure.
func isServerClosed(err error) bool { return errors.Is(err, errServerClosed) }
