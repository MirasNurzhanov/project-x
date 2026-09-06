package main

// Talking to Cloudflare: frames out of Media Transformations, classification
// out of Workers AI.
//
// A near-line-for-line port of `moderation/cloudflare_ai.py`'s *transport*
// layer, and nothing else. Everything that module does beyond moving bytes —
// parsing the model's answer, normalising confidences, mapping categories to
// report reasons — stays in Python. See the note on `FrameResult`.

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"
)

// defaultAPIRoot is where Workers AI lives. Overridable per-instance rather
// than referenced directly, because a package constant is not a seam: with the
// URL baked in, every test either hits the real API or tests nothing. Both
// happened here — the suite was making live calls to api.cloudflare.com, and
// the "token never leaks" test was asserting against a response it never saw.
const defaultAPIRoot = "https://api.cloudflare.com/client/v4"

// Cloudflare wraps the two endpoints this service uses.
type Cloudflare struct {
	accountID string
	token     string

	// transformBase is the origin serving /cdn-cgi/media/. Media
	// Transformations are enabled per *zone*, so this is a custom domain in
	// front of R2 rather than the bucket's own r2.dev address — the same
	// constraint `moderation/checks.py` warns about at Django's startup.
	transformBase string

	// apiRoot is the Workers AI base. Defaults to `defaultAPIRoot`; only tests
	// set it to anything else, via `WithAPIRoot`.
	apiRoot string

	http  *http.Client
	limit *RateLimiter
}

func NewCloudflare(accountID, token, transformBase string, timeout time.Duration, limit *RateLimiter) *Cloudflare {
	return &Cloudflare{
		accountID:     accountID,
		token:         token,
		transformBase: strings.TrimRight(transformBase, "/"),
		apiRoot:       defaultAPIRoot,
		http: &http.Client{
			Timeout: timeout,
			Transport: &http.Transport{
				// This service makes many concurrent calls to two hosts. Go's
				// default of 2 idle connections per host would put most of
				// those goroutines in a TLS handshake instead of in flight.
				MaxIdleConns:        200,
				MaxIdleConnsPerHost: 64,
				IdleConnTimeout:     90 * time.Second,
			},
		},
		limit: limit,
	}
}

// WithAPIRoot points Workers AI calls somewhere else. For tests only.
//
// Returns the receiver so it can be chained onto the constructor. There is no
// environment variable for this on purpose: a production deployment that could
// be pointed at a different inference endpoint by a config change is a
// production deployment that can be pointed at an attacker's.
func (c *Cloudflare) WithAPIRoot(root string) *Cloudflare {
	c.apiRoot = strings.TrimRight(root, "/")
	return c
}

// errRetryable marks failures worth another attempt.
//
// The distinction matters more here than in most services: a retry costs a
// paid inference call. A 403 for a token without Workers AI permission will
// say the same thing forever, and retrying it three times is three times the
// bill for the same answer.
var errRetryable = errors.New("retryable")

// Retryable reports whether an error is worth trying again. Used by the worker
// to fill in `SettleRequest.Retryable`, which is what Django's `_fail` reads to
// decide between backoff and giving up.
func Retryable(err error) bool { return errors.Is(err, errRetryable) }

// ─── Media Transformations ───────────────────────────────────────────────────

// FetchFrame pulls one still JPEG out of the video.
//
// No credentials: /cdn-cgi/media/ is a public edge path and the R2 object is
// public-read. That is worth keeping true — it means the highest-volume call
// this service makes carries no Authorization header at all, so there is one
// fewer place a token can leak.
func (c *Cloudflare) FetchFrame(ctx context.Context, source string, atMS, width int) ([]byte, error) {
	if c.transformBase == "" {
		return nil, fmt.Errorf("no transform origin configured")
	}
	if width <= 0 {
		width = 640
	}
	opts := fmt.Sprintf("mode=frame,time=%s,format=jpg,width=%d,fit=scale-down",
		formatTime(atMS), width)
	return c.getBytes(ctx, c.mediaURL(opts, source), "frame")
}

// FetchAudio pulls the audio track as m4a.
func (c *Cloudflare) FetchAudio(ctx context.Context, source string, durationMS int) ([]byte, error) {
	seconds := durationMS / 1000
	if seconds < 1 {
		seconds = 1
	}
	if seconds > 60 { // the service's own ceiling, and also a Swift's
		seconds = 60
	}
	return c.getBytes(ctx, c.mediaURL(fmt.Sprintf("mode=audio,duration=%ds", seconds), source), "audio")
}

func (c *Cloudflare) mediaURL(opts, source string) string {
	// The source stays a whole absolute URL inside the path — that is the
	// format the service defines. It is escaped so that a `,` in the source
	// cannot be read as the end of the options list.
	return c.transformBase + "/cdn-cgi/media/" + opts + "/" + url.PathEscape(source)
}

// formatTime renders milliseconds as the `time=` option wants: seconds, one
// decimal. Whole seconds would collapse six sample points on a short clip into
// two or three identical frames — the same money for less of the video.
func formatTime(ms int) string {
	if ms < 0 {
		ms = 0
	}
	return fmt.Sprintf("%.1fs", float64(ms)/1000)
}

func (c *Cloudflare) getBytes(ctx context.Context, u, what string) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return nil, fmt.Errorf("building %s request: %w", what, err)
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("%w: fetching %s: %v", errRetryable, what, err)
	}
	defer resp.Body.Close()

	// 8 MiB ceiling. A downscaled JPEG is tens of KB and a 60s m4a is a few
	// hundred; anything near this limit means something is wrong upstream, and
	// an unbounded read is an allocation somebody else controls.
	body, err := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if err != nil {
		return nil, fmt.Errorf("%w: reading %s: %v", errRetryable, what, err)
	}

	if resp.StatusCode != http.StatusOK {
		// A 4xx here is usually configuration, not weather: transformations
		// not enabled on the zone, or the source origin not on the allowed
		// list. Retrying will not enable them.
		if resp.StatusCode >= 500 || resp.StatusCode == http.StatusTooManyRequests {
			return nil, fmt.Errorf("%w: %s returned %d", errRetryable, what, resp.StatusCode)
		}
		// Redacted like every other provider string. No Authorization header is
		// sent on this path, so there should be nothing to scrub — but "should
		// be nothing" is not a property worth relying on for a value that ends
		// up in `last_error`, which is a database column a moderator reads.
		return nil, fmt.Errorf("%s returned %d: %s", what, resp.StatusCode, redact(string(body)))
	}
	if len(body) == 0 {
		return nil, fmt.Errorf("%w: %s was empty", errRetryable, what)
	}
	return body, nil
}

// ─── Workers AI ──────────────────────────────────────────────────────────────

// ClassifyFrame sends one frame to a vision model and returns its answer
// verbatim. Parsing is Django's job.
func (c *Cloudflare) ClassifyFrame(ctx context.Context, model, prompt string, image []byte, options ...VisionOptions) (string, error) {
	// Workers AI's image-to-text schema takes the image as an array of byte
	// values. `[]byte` would marshal to base64 under encoding/json's default
	// rules, which is not what the API expects — so the bytes are widened to
	// ints first. This is a real gotcha and the reason for the explicit loop.
	pixels := make([]int, len(image))
	for i, b := range image {
		pixels[i] = int(b)
	}

	payload := map[string]any{
		"image":  pixels,
		"prompt": prompt,
		// Legacy leases omit the budget. Current leases override this with
		// room for the scene description and evidence required by Django.
		"max_tokens": 128,
		// Classification, not creative writing — the lowest useful setting, so
		// the same frame does not get two verdicts on two runs.
		"temperature": 0.1,
	}
	if len(options) > 0 {
		if options[0].MaxTokens > 0 {
			payload["max_tokens"] = options[0].MaxTokens
		}
		if len(options[0].ResponseFormat) > 0 && string(options[0].ResponseFormat) != "null" {
			payload["response_format"] = options[0].ResponseFormat
		}
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return "", fmt.Errorf("encoding vision request: %w", err)
	}

	result, err := c.run(ctx, model, body)
	if err != nil {
		return "", err
	}
	raw := bytes.TrimSpace(result.Result.Response)
	if len(raw) == 0 {
		// Missing answers must reach Django as invalid verdicts, not fetch
		// failures which may be tolerated by the frame coverage rule.
		return "null", nil
	}
	if raw[0] == '"' {
		var answer string
		if err := json.Unmarshal(raw, &answer); err != nil {
			return "", fmt.Errorf("%w: invalid vision response encoding", errRetryable)
		}
		if strings.TrimSpace(answer) != "" {
			return answer, nil
		}
	}
	// JSON Mode may return an object. Preserve other types and empty strings
	// too: Django must reject them without approving from the remaining frames.
	return string(raw), nil
}

// Transcribe sends the audio track to a speech-to-text model.
func (c *Cloudflare) Transcribe(ctx context.Context, model string, audio []byte) (string, error) {
	samples := make([]int, len(audio))
	for i, b := range audio {
		samples[i] = int(b)
	}
	body, err := json.Marshal(map[string]any{"audio": samples})
	if err != nil {
		return "", fmt.Errorf("encoding audio request: %w", err)
	}
	result, err := c.run(ctx, model, body)
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(result.Result.Text), nil
}

// run posts to /accounts/{id}/ai/run/{model}.
//
// The one place an Authorization header is built in this service.
func (c *Cloudflare) run(ctx context.Context, model string, body []byte) (*workersAIResponse, error) {
	if c.accountID == "" || c.token == "" {
		// Not retryable, and deliberately loud. "Moderation is switched on and
		// cannot run" has to fail closed — Django leaves the Swift PENDING,
		// which means invisible, which is the only acceptable direction.
		return nil, errors.New("cloudflare credentials are not configured")
	}
	if err := c.limit.Wait(ctx); err != nil {
		return nil, fmt.Errorf("waiting for rate limit: %w", err)
	}

	u := fmt.Sprintf("%s/accounts/%s/ai/run/%s", c.apiRoot, c.accountID, model)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, u, bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("building request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	req.Header.Set("Content-Type", "application/json")

	resp, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("%w: workers ai unreachable: %v", errRetryable, err)
	}
	defer resp.Body.Close()

	raw, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return nil, fmt.Errorf("%w: reading response: %v", errRetryable, err)
	}

	if resp.StatusCode != http.StatusOK {
		if resp.StatusCode >= 500 || resp.StatusCode == http.StatusTooManyRequests {
			return nil, fmt.Errorf("%w: workers ai returned %d", errRetryable, resp.StatusCode)
		}
		return nil, fmt.Errorf("workers ai returned %d: %s",
			resp.StatusCode, redact(string(raw)))
	}

	var decoded workersAIResponse
	if err := json.Unmarshal(raw, &decoded); err != nil {
		return nil, fmt.Errorf("%w: decoding response: %v", errRetryable, err)
	}
	if !decoded.Success {
		msg := ""
		if len(decoded.Errors) > 0 {
			msg = decoded.Errors[0].Message
		}
		return nil, fmt.Errorf("%w: workers ai reported failure: %s", errRetryable, redact(msg))
	}
	return &decoded, nil
}

// bearerPattern matches anything shaped like a token in a provider message.
var bearerPattern = regexp.MustCompile(`(?i)bearer\s+[\w.\-]+`)

// redact makes a provider string safe to log or to store on a job row.
//
// Cloudflare does not echo the Authorization header back, so this is belt and
// braces — but `SettleRequest.Error` ends up in `last_error` on a database row
// a moderator can read, and the cost of being wrong once is a credential
// sitting in Postgres.
func redact(s string) string {
	return truncate(bearerPattern.ReplaceAllString(s, "Bearer [redacted]"), 240)
}
