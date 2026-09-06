package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestWorkerForwardsStructuredVisionLeaseAndObjectAnswer(t *testing.T) {
	for _, inputFormat := range []string{"", "image", "messages"} {
		name := inputFormat
		if name == "" {
			name = "legacy lease without input format"
		}
		t.Run(name, func(t *testing.T) {
			testWorkerVisionInput(t, inputFormat)
		})
	}
}

func testWorkerVisionInput(t *testing.T, inputFormat string) {
	t.Helper()
	seen := make(chan map[string]any, 1)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasPrefix(r.URL.Path, "/cdn-cgi/media/") {
			_, _ = w.Write([]byte{255, 216, 0, 1})
			return
		}
		var payload map[string]any
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Error(err)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		seen <- payload
		fmt.Fprint(w, `{"success":true,"result":{"response":{"description":"A desk.","evidence":"","unsafe":false,"categories":[],"confidence":0.9}}}`)
	}))
	defer srv.Close()
	limiter := NewRateLimiter(1000, 1000)
	defer limiter.Close()
	cf := NewCloudflare("test-account", "test-token", srv.URL, time.Second, limiter).WithAPIRoot(srv.URL)
	var lease Lease
	formatField := ""
	if inputFormat != "" {
		formatField = fmt.Sprintf(`,"vision_input_format":%q`, inputFormat)
	}
	err := json.Unmarshal([]byte(fmt.Sprintf(`{
		"job_id":1,"swift_id":22,"source_url":%q,"timestamps_ms":[1500],
		"prompt":"Inspect this frame.","vision_model":"@cf/test/vision",
		"vision_max_tokens":384,
		"vision_response_format":{"type":"json_schema","json_schema":{"type":"object","properties":{"unsafe":{"type":"boolean"}},"required":["unsafe"]}}%s
	}`, srv.URL+"/video.mp4", formatField)), &lease)
	if err != nil {
		t.Fatal(err)
	}
	worker := &Worker{cf: cf}
	result := worker.classifyOne(context.Background(), lease, 1500)
	if result.Error != "" {
		t.Fatal(result.Error)
	}
	request := <-seen
	if request["max_tokens"] != float64(384) || request["temperature"] != 0.1 || request["stream"] != false {
		t.Fatalf("lease options were not forwarded: %v", request)
	}
	var wantFormat map[string]any
	if err := json.Unmarshal(lease.VisionResponseFormat, &wantFormat); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(request["response_format"], wantFormat) {
		t.Fatalf("schema changed in transit: %v", request["response_format"])
	}
	if inputFormat == "messages" {
		assertMessageVisionInput(t, request, lease.Prompt, []byte{255, 216, 0, 1})
	} else {
		if request["prompt"] != lease.Prompt {
			t.Fatal("legacy prompt was not forwarded")
		}
		if _, exists := request["messages"]; exists {
			t.Fatal("legacy image request unexpectedly contains messages")
		}
		if !reflect.DeepEqual(request["image"], []any{float64(255), float64(216), float64(0), float64(1)}) {
			t.Fatal("vision request did not contain the fetched bytes")
		}
	}
	var answer map[string]any
	if err := json.Unmarshal([]byte(result.Raw), &answer); err != nil {
		t.Fatal(err)
	}
	if answer["description"] != "A desk." || answer["unsafe"] != false || result.AtMS != 1500 {
		t.Fatalf("object answer or timestamp changed: %+v", result)
	}
}

func assertMessageVisionInput(t *testing.T, request map[string]any, prompt string, image []byte) {
	t.Helper()
	for _, key := range []string{"image", "prompt"} {
		if _, exists := request[key]; exists {
			t.Fatalf("messages request unexpectedly contains top-level %s", key)
		}
	}
	encoded, err := json.Marshal(request["messages"])
	if err != nil {
		t.Fatal(err)
	}
	var messages []struct {
		Role    string          `json:"role"`
		Content json.RawMessage `json:"content"`
	}
	if err := json.Unmarshal(encoded, &messages); err != nil {
		t.Fatal(err)
	}
	if len(messages) != 2 || messages[0].Role != "system" || messages[1].Role != "user" {
		t.Fatalf("unexpected messages: %s", encoded)
	}
	var policy string
	if err := json.Unmarshal(messages[0].Content, &policy); err != nil || policy != prompt {
		t.Fatalf("system prompt changed: %q error=%v", policy, err)
	}
	var content []struct {
		Type     string `json:"type"`
		Text     string `json:"text"`
		ImageURL struct {
			URL string `json:"url"`
		} `json:"image_url"`
	}
	if err := json.Unmarshal(messages[1].Content, &content); err != nil {
		t.Fatal(err)
	}
	if len(content) != 2 || content[0].Type != "text" || content[1].Type != "image_url" ||
		content[0].Text != "Classify only the supplied frame. Return the required JSON object." {
		t.Fatalf("unexpected image message: %s", messages[1].Content)
	}
	const prefix = "data:image/jpeg;base64,"
	if !strings.HasPrefix(content[1].ImageURL.URL, prefix) {
		t.Fatal("image is not an inline JPEG data URI")
	}
	decoded, err := base64.StdEncoding.DecodeString(strings.TrimPrefix(content[1].ImageURL.URL, prefix))
	if err != nil || !reflect.DeepEqual(decoded, image) {
		t.Fatalf("image bytes changed in transport: %v error=%v", decoded, err)
	}
}

func TestUnsupportedVisionInputFormatNeverCallsAPI(t *testing.T) {
	var calls atomic.Int64
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		fmt.Fprint(w, `{"success":true,"result":{"response":{"unsafe":false}}}`)
	}))
	defer srv.Close()
	limiter := NewRateLimiter(1000, 1000)
	defer limiter.Close()
	cf := NewCloudflare("test-account", "test-token", srv.URL, time.Second, limiter).WithAPIRoot(srv.URL)
	answer, err := cf.ClassifyFrame(context.Background(), "@cf/test/vision", "inspect", []byte{255, 216}, VisionOptions{InputFormat: "unsupported"})
	if err == nil || answer != "" || Retryable(err) {
		t.Fatalf("unsupported input format was not rejected: answer=%q error=%v", answer, err)
	}
	if calls.Load() != 0 {
		t.Fatal("unsupported input format reached the API")
	}
	lease := Lease{JobID: 1, SourceURL: "https://example.com/video.mp4", TimestampsMS: []int{1500}, VisionInputFormat: "unsupported"}
	if err := lease.Validate(); err == nil {
		t.Fatal("unsupported lease input format was accepted")
	}
}

func TestLeaseAdvertisesSupportedVisionInputFormats(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Moderation-Vision-Formats") != "image,messages" {
			t.Error("worker did not advertise image-message support")
			w.WriteHeader(http.StatusConflict)
			return
		}
		fmt.Fprint(w, `{"leases":[]}`)
	}))
	defer srv.Close()
	client := NewDjangoClient(srv.URL, "test-worker-token", time.Second, nil)
	if _, err := client.Lease(context.Background(), 1); err != nil {
		t.Fatal(err)
	}
}

func TestVisionResponseTypesArePreservedForDjangoValidation(t *testing.T) {
	for _, test := range []struct {
		name string
		raw  string
		want string
	}{
		{"text", `"{\"unsafe\":false}"`, `{"unsafe":false}`},
		{"object", `{"unsafe":false}`, `{"unsafe":false}`},
		{"null", `null`, `null`},
		{"missing", ``, `null`},
		{"array", `[{"unsafe":false}]`, `[{"unsafe":false}]`},
		{"boolean", `true`, `true`},
		{"number", `1`, `1`},
		{"empty", `""`, `""`},
		{"blank", `"   "`, `"   "`},
	} {
		t.Run(test.name, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if test.name == "missing" {
					fmt.Fprint(w, `{"success":true,"result":{}}`)
					return
				}
				fmt.Fprintf(w, `{"success":true,"result":{"response":%s}}`, test.raw)
			}))
			defer srv.Close()
			limiter := NewRateLimiter(1000, 1000)
			defer limiter.Close()
			cf := NewCloudflare("test-account", "test-token", srv.URL, time.Second, limiter).WithAPIRoot(srv.URL)
			answer, err := cf.ClassifyFrame(context.Background(), "@cf/test/vision", "inspect", []byte{255, 216})
			if err != nil || answer != test.want {
				t.Fatalf("response was not preserved: answer=%q error=%v", answer, err)
			}
		})
	}
}
