package main

// The data this service moves around.
//
// ─── Structs, coming from Python ─────────────────────────────────────────────
//
// A Go struct is *not* a class. It is closer to a `@dataclass` with no methods
// attached to the definition, or to a C struct: a fixed set of typed fields,
// laid out in memory in that order. Three differences that matter early:
//
//   1. **No inheritance.** There is no base class to extend. Go composes
//      instead — you embed one struct inside another. Django's model
//      inheritance has no equivalent here and you will not miss it as much as
//      you expect.
//
//   2. **Methods live outside the type.** You write `func (m Message) Valid()`
//      rather than putting `def valid(self)` inside a class body. The `(m
//      Message)` part is called the *receiver* and it is Go's `self`. You can
//      hang methods on any type you defined, in any file of the same package.
//
//   3. **Fields have a visibility rule based on capitalisation.** A field
//      starting with a capital letter is exported (public); lowercase is
//      package-private. This is not style — it is enforced by the compiler,
//      and it has a consequence that bites every newcomer exactly once:
//      `encoding/json` can only see exported fields. A lowercase field is
//      silently omitted from JSON. Every field below is capitalised for that
//      reason.
//
// The `json:"..."` strings are **struct tags** — metadata read at runtime by
// the json package, the same way Django reads `db_column=` off a Field. They
// are how you get snake_case JSON out of CamelCase Go fields.

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
)

// Message is one push, addressed to one device.
//
// The field set is Expo's, not ours: this service forwards these to
// https://exp.host/--/api/v2/push/send unchanged, so the shape has to match
// what Expo documents. Django already builds exactly this dict in
// `notifications/push.py::_envelope`.
type Message struct {
	// To is an Expo push token, e.g. "ExponentPushToken[xxxxxxxx]".
	To string `json:"to"`

	Title string `json:"title"`
	Body  string `json:"body"`

	// Sound is "default" or empty. `omitempty` drops the key entirely when the
	// value is the type's zero value — for a string that is "". Without it,
	// every message would carry `"sound": ""`, which is a different thing to
	// Expo than "no sound key at all".
	Sound string `json:"sound,omitempty"`

	// ChannelID is the Android notification channel. Note the Go name
	// (ChannelID, idiomatic capitalisation for initialisms) differs from the
	// wire name (channelId, which is what Expo wants). The tag is what
	// reconciles them, which is exactly what tags are for.
	ChannelID string `json:"channelId,omitempty"`

	// Data is the routing payload the app reads when the notification is
	// tapped. `map[string]any` is Go's dict-with-unknown-values — `any` is an
	// alias for `interface{}`, and it is the closest thing Go has to Python's
	// "could be anything". Deliberately not typed further: this service does
	// not care what is in it, and giving it a shape would mean editing Go
	// every time Django adds a field.
	Data map[string]any `json:"data,omitempty"`
}

// Validate reports whether this message is worth sending.
//
// ─── Errors are values ───────────────────────────────────────────────────────
//
// Go has no exceptions. A function that can fail returns an `error` as its
// last return value, and the caller checks it. That is the whole mechanism.
//
// It feels verbose coming from `raise`/`except` — you will write `if err !=
// nil` a great deal — but it buys one property that is hard to get in Python:
// every failure is visible at the call site. There is no such thing as an
// exception propagating up through four frames of code that never mentioned
// it. When you are reading Go and you do not see an error check, there is no
// error to check.
//
// `errors.New` makes a simple one. `fmt.Errorf` with the `%w` verb *wraps*
// another error, keeping the original inspectable via `errors.Is` /
// `errors.As` — that is Go's version of `raise X from Y`.
func (m Message) Validate() error {
	if !strings.HasPrefix(m.To, "ExponentPushToken[") &&
		!strings.HasPrefix(m.To, "ExpoPushToken[") {
		// Rejected here rather than at Expo, for a reason worth stating: a
		// batch containing one malformed token is not partially delivered by
		// Expo, and finding out at the far end costs a whole round trip for
		// every message that shared the batch with it.
		return fmt.Errorf("token %q is not an Expo push token", truncate(m.To, 24))
	}
	if m.Title == "" && m.Body == "" {
		return errors.New("message has neither title nor body")
	}
	return nil
}

// PushRequest is what Django POSTs to /v1/push.
//
// A wrapper object rather than a bare JSON array, because a top-level array is
// a shape you can never add a field to. Wrapping it means "add a `priority`
// later" is a compatible change instead of a new endpoint.
type PushRequest struct {
	Messages []Message `json:"messages"`

	// IdempotencyKey is optional and currently only logged. It is here because
	// the moment you add retries on the Django side you will want it, and
	// adding a field to a request struct after clients exist is more work than
	// reserving it now.
	IdempotencyKey string `json:"idempotency_key,omitempty"`
}

// PushResponse is what /v1/push answers with.
//
// Note what it does *not* contain: any statement about delivery. The service
// answers 202 Accepted as soon as the work is queued, so the only honest thing
// it can report is how much it took. Saying "sent" here would be a lie the
// caller might act on.
type PushResponse struct {
	Queued   int `json:"queued"`
	Rejected int `json:"rejected"`
	// Errors explains the rejected ones. A nil slice marshals to `null` and an
	// empty one to `[]`; `omitempty` drops the key when there is nothing to
	// say, which keeps the common response small.
	Errors []string `json:"errors,omitempty"`
}

// Stats is the /v1/stats payload — a cheap window into what the queue is doing.
//
// Worth having from day one on anything with a queue in it: "is the backlog
// growing" is the first question you will ask in production, and it is
// miserable to answer by adding instrumentation to a service that is already
// misbehaving.
type Stats struct {
	Queued     int64 `json:"queued_total"`
	Sent       int64 `json:"sent_total"`
	Failed     int64 `json:"failed_total"`
	Dropped    int64 `json:"dropped_total"`
	QueueDepth int   `json:"queue_depth"`
	QueueCap   int   `json:"queue_capacity"`
	Workers    int   `json:"workers"`
}

// expoRequest is the body Expo itself expects: a bare array of messages.
//
// Lowercase, so it is unexported — nothing outside this package can construct
// one. That is deliberate: it is an implementation detail of how we talk to
// Expo, not part of this service's own contract.
type expoRequest []Message

// expoResponse is the sliver of Expo's reply this service acts on.
//
// Expo returns far more than this. Decoding only what you use is normal and
// good in Go: `encoding/json` ignores unknown fields by default, so a provider
// adding keys cannot break you.
type expoResponse struct {
	Data []struct {
		Status  string `json:"status"` // "ok" or "error"
		Message string `json:"message,omitempty"`
		Details struct {
			Error string `json:"error,omitempty"`
		} `json:"details,omitempty"`
	} `json:"data"`
	Errors []struct {
		Code    string `json:"code"`
		Message string `json:"message"`
	} `json:"errors,omitempty"`
}

// deviceNotRegistered is Expo's code for a token that will never work again —
// the app was uninstalled, or the token was rotated.
//
// Called out because it is the one Expo failure that is *not* transient and
// that Django genuinely wants to know about: the row in `users.Device` should
// be deleted. Retrying it forever is the mistake this constant exists to
// prevent.
const deviceNotRegistered = "DeviceNotRegistered"

// truncate shortens a string for safe logging.
//
// Push tokens are device identifiers. They are not secrets in the way an API
// key is, but there is no reason for a full one to sit in a log aggregator, so
// nothing in this service logs one whole.
func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "…"
}

// decodeJSON reads a JSON body into `dst`, refusing anything oversized or
// unexpected.
//
// A generic function — the `[T any]` is Go's type parameter list, added in
// 1.18. Before generics this would have taken an `any` and returned an `any`,
// and the caller would have type-asserted. This version is checked at compile
// time.
//
// `DisallowUnknownFields` is a deliberate strictness: a typo in Django's
// payload ("titel") would otherwise be silently dropped and produce a push
// with no title, which is a bug you find in the App Store reviews rather than
// in a 400.
func decodeJSON[T any](data []byte, dst *T) error {
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		return fmt.Errorf("malformed JSON body: %w", err)
	}
	return nil
}
