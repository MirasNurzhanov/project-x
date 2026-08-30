package main

// The contract with Django, in one file.
//
// ─── Where the boundary is, and why it is there ──────────────────────────────
//
// This service is deliberately ignorant. It does not know what a Swift is, it
// has no opinion on what counts as unsafe, and it cannot change anybody's
// moderation state. Django hands it a list of timestamps and a video URL; it
// hands back what a vision model said about each frame.
//
// That line is not arbitrary. Everything on Django's side of it —
// `moderation/policy.py`'s thresholds, the conditional `PENDING -> OK` update,
// the `get_or_create` that stops a retry filing a second report — is logic
// with subtle race guards and its own test suite. Reimplementing any of it
// here would mean two copies of a *safety* decision in two languages, and the
// second copy drifts silently.
//
// What is left for Go is the part Python was actually bad at: fetching a dozen
// frames and classifying them, concurrently, under a shared rate limit.

import (
	"errors"
	"fmt"
	"strings"
)

// Lease is one unit of work, as Django hands it over.
//
// Django has already claimed the job by the time this arrives — the atomic
// `UPDATE ... WHERE state='queued'` in `swifts.ai_moderation.claim` ran on its
// side. So possession of a Lease means the job is ours and nobody else has it,
// which is why there is no claim protocol in this service at all.
type Lease struct {
	JobID   int64 `json:"job_id"`
	SwiftID int64 `json:"swift_id"`

	// SourceURL is the video, already resolved by `swifts.media.playback_url`.
	// Absolute, because Cloudflare has to fetch it.
	SourceURL string `json:"source_url"`

	// TimestampsMS is the sampling plan, computed by
	// `moderation.policy.sample_timestamps` on Django's side. Deliberately not
	// computed here: "which frames represent this video" is a policy question,
	// and it is already tested over there.
	TimestampsMS []int `json:"timestamps_ms"`

	// DurationMS is the trimmed duration, needed only for the audio track.
	DurationMS int `json:"duration_ms"`

	// WantAudio mirrors SWIFT_AI_AUDIO_MODERATION_ENABLED. A flag rather than
	// a decision this service makes, so one setting still governs it.
	WantAudio bool `json:"want_audio"`

	// FrameWidth is the downscale applied before classification. Passed
	// through because it is a cost control Django owns.
	FrameWidth int `json:"frame_width"`

	// Prompt is the classifier instruction, from
	// `moderation.cloudflare_ai.VISION_PROMPT`. Sent down rather than
	// duplicated here: a filter prompt is a specification for getting past the
	// filter, and it should have exactly one home — one that is not also
	// copied into a second repository's git history.
	Prompt string `json:"prompt"`

	// The Workers AI models to call. Django's settings own these
	// (`CLOUDFLARE_AI_VISION_MODEL`, `CLOUDFLARE_AI_TRANSCRIBE_MODEL`), so
	// swapping a model stays a Django environment change and does not need
	// this service redeployed.
	VisionModel     string `json:"vision_model"`
	TranscribeModel string `json:"transcribe_model"`
}

// Validate rejects a lease this service cannot act on.
//
// Worth doing even though Django built the payload: a service that trusts its
// caller completely is one that panics on a nil field the day somebody changes
// a serializer. The failure mode here is a clean "return it unworked", which
// Django turns into a retry.
func (l Lease) Validate() error {
	if l.JobID == 0 {
		return errors.New("lease has no job_id")
	}
	if !strings.HasPrefix(l.SourceURL, "http://") && !strings.HasPrefix(l.SourceURL, "https://") {
		return fmt.Errorf("source_url %q is not absolute", truncate(l.SourceURL, 40))
	}
	if len(l.TimestampsMS) == 0 {
		return errors.New("lease has no timestamps to sample")
	}
	return nil
}

// FrameResult is one frame's outcome: the model's answer, verbatim.
//
// `Raw` is the untouched text the vision model produced. This service does not
// parse it, and that is the last piece of the boundary argument.
//
// Parsing it looks harmless — pull out `unsafe`, `confidence`, `categories` —
// but `moderation.cloudflare_ai.parse_vision_response` is forty lines of
// hard-won leniency: models wrap the JSON in prose, answer `"high"` where a
// float was asked for, report confidence on a 0-100 scale, and invent category
// names. Every one of those behaviours has a test in
// `moderation/test_cloudflare_ai.py`. A second parser in Go would be a second
// set of those bugs, discovered separately.
//
// So Go carries bytes and Django reads them. The one rule this service does
// apply is the one it cannot avoid: an empty `Raw` with an `Error` set means
// the frame was never classified, which Django counts towards the
// "was enough of this video actually looked at" check in
// `policy.combine_frames`.
type FrameResult struct {
	AtMS int    `json:"at_ms"`
	Raw  string `json:"raw,omitempty"`

	// Error is set when this frame could not be classified — a fetch failure,
	// a 5xx, a timeout. Per-frame rather than fatal, because one bad timestamp
	// out of ten is survivable and Django already owns the rule for deciding
	// whether enough frames survived.
	Error string `json:"error,omitempty"`
}

// SettleRequest is what this service posts back when a job is finished.
//
// It reports observations, never a verdict. There is no "approved" field, and
// that absence is the design: `policy.combine_frames` on Django's side turns
// these into a decision, using thresholds this service never sees.
type SettleRequest struct {
	JobID int64 `json:"job_id"`

	// Frames is one entry per requested timestamp, in order, including the
	// ones that failed. Django needs the failures to count them.
	Frames []FrameResult `json:"frames"`

	// Transcript is what Whisper heard, when audio was requested and worked.
	// Empty otherwise. Django runs it through `moderation.text.classify` — the
	// same classifier the caption goes through, which is the whole point of
	// transcribing rather than having a second spoken-word filter.
	Transcript string `json:"transcript,omitempty"`

	// AudioChecked distinguishes "we transcribed and heard nothing" from "we
	// never got the audio". A stored fact about this run, not a restatement of
	// the setting.
	AudioChecked bool `json:"audio_checked"`

	// ModelUsed is recorded on the job row so a moderator reading the queue
	// knows which model produced the verdict.
	ModelUsed string `json:"model_used"`

	// Error is set when the whole job failed rather than individual frames —
	// missing credentials, an unusable lease. Django decides whether that is
	// retryable.
	Error string `json:"error,omitempty"`

	// Retryable tells Django whether trying again could produce a different
	// answer. A 403 from Workers AI will say the same thing forever; a timeout
	// will not. Django's `_fail` already branches on exactly this.
	Retryable bool `json:"retryable"`
}

// leaseResponse is the body of GET /lease. A wrapper rather than a bare array
// so "no work right now" is an empty list rather than an error.
type leaseResponse struct {
	Leases []Lease `json:"leases"`
}

// Stats is the /v1/stats payload.
type Stats struct {
	Leased        int64 `json:"leased_total"`
	Settled       int64 `json:"settled_total"`
	FramesOK      int64 `json:"frames_classified_total"`
	FramesFailed  int64 `json:"frames_failed_total"`
	JobsFailed    int64 `json:"jobs_failed_total"`
	InFlight      int64 `json:"jobs_in_flight"`
	MaxConcurrent int   `json:"max_concurrent_jobs"`

	// ContractDrift counts lease payloads carrying fields this build does not
	// know about. Non-zero means Django's contract moved and this service is
	// behind — alert on it. See DjangoClient.decodeLeases.
	ContractDrift int64 `json:"contract_drift_total"`
}

// ─── Cloudflare wire types ───────────────────────────────────────────────────
// Unexported: these are how we talk to a provider, not part of any contract.

type workersAIResponse struct {
	Success bool `json:"success"`
	Result  struct {
		Response string `json:"response"`
		// Whisper returns text here instead.
		Text string `json:"text"`
	} `json:"result"`
	Errors []struct {
		Message string `json:"message"`
	} `json:"errors"`
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "…"
}
