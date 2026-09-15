package main

// The client for Django's two internal endpoints.
//
// This is the whole integration. There is no database driver, no shared model
// definition, and no migration this service has to stay in step with — which
// is the payoff of putting the boundary where it is. Django owns every write;
// this owns every Cloudflare call.

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"sync/atomic"
	"time"
)

type DjangoClient struct {
	baseURL string
	token   string
	http    *http.Client
	log     *slog.Logger

	// drift counts lease payloads that failed a strict decode. Exposed on
	// /v1/stats so contract drift is monitorable rather than merely logged —
	// see decodeLeases.
	drift atomic.Int64
}

func NewDjangoClient(baseURL, token string, timeout time.Duration, log *slog.Logger) *DjangoClient {
	if log == nil {
		log = slog.Default()
	}
	return &DjangoClient{
		baseURL: strings.TrimRight(baseURL, "/"),
		token:   token,
		http:    &http.Client{Timeout: timeout},
		log:     log,
	}
}

// Drift reports how many lease payloads carried fields this build does not
// know about.
func (c *DjangoClient) Drift() int64 { return c.drift.Load() }

// errUnknownField is returned by strictDecode when the payload carries a field
// this build has never heard of.
var errUnknownField = errors.New("unknown field in payload")

// strictDecode refuses unknown fields.
func strictDecode(data []byte, dst any) error {
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		if strings.Contains(err.Error(), "unknown field") {
			return fmt.Errorf("%w: %v", errUnknownField, err)
		}
		return err
	}
	return nil
}

// decodeLeases parses a lease payload strictly, then leniently.
//
// ─── Why not simply strict ───────────────────────────────────────────────────
//
// `DisallowUnknownFields` alone would turn a *backwards-compatible* Django
// change into an outage: Django adds a field this worker does not need, every
// lease is rejected, and moderation stops until Go is redeployed. That is a
// worse failure than the one it prevents.
//
// `json.Unmarshal` alone is the opposite problem: Django *renames*
// `timestamps_ms` and this worker silently receives an empty slice.
//
// So: strict first, and if the only complaint is an unknown field, log loudly,
// count it, and decode again leniently so the work still happens. CI gets the
// hard failure (`TestLeaseDecodingRejectsAnUnknownField` asserts strictDecode
// rejects a canary), production gets a warning and a metric.
//
// The rename case is caught separately and hard, by `Lease.Validate` — a
// renamed `timestamps_ms` produces an empty slice, which is refused as
// non-retryable rather than silently classifying zero frames.
func (c *DjangoClient) decodeLeases(body []byte) ([]Lease, error) {
	var decoded leaseResponse
	err := strictDecode(body, &decoded)
	if err == nil {
		return decoded.Leases, nil
	}
	if !errors.Is(err, errUnknownField) {
		return nil, fmt.Errorf("decoding lease response: %w", err)
	}

	c.drift.Add(1)
	c.log.Warn("lease payload carries fields this build does not know; " +
		"the Django contract has changed and services/go-moderation is behind")

	var lenient leaseResponse
	if err := json.Unmarshal(body, &lenient); err != nil {
		return nil, fmt.Errorf("decoding lease response: %w", err)
	}
	return lenient.Leases, nil
}

// Lease asks Django for up to `n` claimed jobs.
//
// Django does the claiming — the atomic `UPDATE ... WHERE state='queued'` in
// `swifts.ai_moderation.claim`. That protocol already exists, is already
// tested, and already arbitrates between the in-process daemon thread and the
// `moderate_swifts` sweeper. Adding this service as a third consumer of it
// needed no new concurrency design at all, which is the main argument for
// having picked this workload.
func (c *DjangoClient) Lease(ctx context.Context, n int) ([]Lease, error) {
	u := fmt.Sprintf("%s/api/internal/moderation/lease/?limit=%d", c.baseURL, n)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return nil, fmt.Errorf("building lease request: %w", err)
	}
	// Django refuses message-only models before claiming work for old builds
	// that would otherwise ignore the new input-format field.
	req.Header.Set("X-Moderation-Vision-Formats", "image,messages")
	c.authorise(req)

	resp, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("lease: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if err != nil {
		return nil, fmt.Errorf("reading lease response: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("lease returned %d: %s", resp.StatusCode, truncate(string(body), 200))
	}

	return c.decodeLeases(body)
}

// Settle hands the results back.
//
// Django turns them into a decision with `policy.combine_frames` and applies it
// through the existing `ai_moderation.settle` / `_fail` — so every race guard
// that code already has (the conditional PENDING update, the idempotent
// report) applies unchanged to work this service did.
func (c *DjangoClient) Settle(ctx context.Context, result SettleRequest) error {
	body, err := json.Marshal(result)
	if err != nil {
		return fmt.Errorf("encoding settle: %w", err)
	}
	u := c.baseURL + "/api/internal/moderation/settle/"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, u, bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("building settle request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	c.authorise(req)

	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("settle: %w", err)
	}
	defer resp.Body.Close()
	// Drain before closing. An unread body means the connection cannot be
	// reused, and on a loop that settles a job every few seconds that quietly
	// turns into a new TCP handshake every time.
	drained, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))

	if resp.StatusCode >= 300 {
		return fmt.Errorf("settle returned %d: %s", resp.StatusCode, truncate(string(drained), 200))
	}
	return nil
}

// authorise adds the shared secret.
//
// The same value Django reads as `MODERATION_WORKER_TOKEN`. A bearer token
// rather than mTLS because of where this sits: two processes in one private
// network, one client, and the thing being defended is "nothing else can lease
// moderation jobs or forge verdicts". A shared secret in an env var is
// proportionate to that, and it is what the existing `PUSH_DISPATCHER_TOKEN`
// in services/go-push already does — one pattern for both, rather than two.
func (c *DjangoClient) authorise(req *http.Request) {
	if c.token != "" {
		req.Header.Set("Authorization", "Bearer "+c.token)
	}
}
