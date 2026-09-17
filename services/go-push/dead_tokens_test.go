package main

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestTicketCleanupIsPositionalAndLogsNoPayload(t *testing.T) {
	var logs bytes.Buffer
	c := &ExpoClient{log: slog.New(slog.NewJSONHandler(&logs, nil))}
	var cleared []string
	c.OnDeadToken(func(token string) { cleared = append(cleared, token) })
	var response expoResponse
	json.Unmarshal([]byte(`{"data":[{"status":"ok"},{"status":"error","message":"private provider message","details":{"error":"DeviceNotRegistered"}}]}`), &response)
	sent := []Message{message("live"), message("dead")}
	c.inspectTickets(response, sent)
	if len(cleared) != 1 || cleared[0] != sent[1].To {
		t.Fatalf("wrong token selected: %d", len(cleared))
	}
	for _, private := range []string{"private provider message", sent[0].To, sent[1].To} {
		if strings.Contains(logs.String(), private) {
			t.Fatal("private payload logged")
		}
	}
	cleared = nil
	c.inspectTickets(response, sent[:1])
	if len(cleared) != 0 {
		t.Fatal("mismatched ticket count must not invalidate tokens")
	}
}

func TestDeadTokenCallbackRetriesAndAuthenticates(t *testing.T) {
	calls := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.Header.Get("Authorization") != "Bearer test-secret" {
			t.Error("missing shared authentication")
		}
		var body map[string][]string
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if len(body["tokens"]) != 1 || body["tokens"][0] != message("dead").To {
			t.Error("wrong callback body")
		}
		if calls == 1 {
			w.WriteHeader(503)
			return
		}
		w.WriteHeader(200)
	}))
	defer srv.Close()
	if err := reportDeadToken(context.Background(), srv.Client(), srv.URL, "test-secret", message("dead").To); err != nil {
		t.Fatal(err)
	}
	if calls != 2 {
		t.Fatalf("expected one retry, got %d calls", calls)
	}
}

func TestDeadTokenCallbackDoesNotRetryForbiddenOrForwardRedirect(t *testing.T) {
	for _, status := range []int{403, 307} {
		t.Run(http.StatusText(status), func(t *testing.T) {
			calls := 0
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls++
				w.Header().Set("Location", "/other")
				w.WriteHeader(status)
			}))
			defer srv.Close()
			client := &http.Client{Timeout: time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
			if reportDeadToken(context.Background(), client, srv.URL, "test-secret", message("dead").To) == nil {
				t.Fatal("expected failure")
			}
			if calls != 1 {
				t.Fatalf("unexpected retries/redirects: %d", calls)
			}
		})
	}
}

func TestExpoFailureDoesNotEchoProviderBody(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(400)
		io.WriteString(w, "secret-token private notification body")
	}))
	defer srv.Close()
	limit := NewRateLimiter(100, 100)
	defer limit.Close()
	c := NewExpoClient(srv.URL, time.Second, discardLogger(), limit, 1)
	err := c.Send(context.Background(), []Message{message("dead")})
	if err == nil || strings.Contains(err.Error(), "secret-token") || strings.Contains(err.Error(), "private notification") {
		t.Fatalf("unsafe or missing error: %v", err)
	}
}
