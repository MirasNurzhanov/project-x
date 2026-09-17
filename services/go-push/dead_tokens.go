package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"time"
)

// Runs inside the bounded dispatcher worker, never in an extra goroutine.
// Clearing a token is idempotent; retry transport errors and server failures.
func reportDeadToken(ctx context.Context, client *http.Client, url, secret, token string) error {
	body, _ := json.Marshal(map[string][]string{"tokens": {token}})
	for attempt := 0; attempt < 3; attempt++ {
		if attempt > 0 {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(time.Duration(attempt) * 100 * time.Millisecond):
			}
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
		if err != nil {
			return errors.New("invalid cleanup endpoint")
		}
		req.Header.Set("Authorization", "Bearer "+secret)
		req.Header.Set("Content-Type", "application/json")
		resp, err := client.Do(req)
		if err != nil {
			continue
		}
		io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
		resp.Body.Close()
		if resp.StatusCode >= 200 && resp.StatusCode < 300 {
			return nil
		}
		if resp.StatusCode < 500 && resp.StatusCode != 429 {
			break
		}
	}
	return errors.New("dead token cleanup failed")
}
