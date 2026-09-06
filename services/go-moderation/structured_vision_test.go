package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
	"time"
)

func TestWorkerForwardsStructuredVisionLeaseAndObjectAnswer(t *testing.T) {
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
	err := json.Unmarshal([]byte(fmt.Sprintf(`{
		"job_id":1,"swift_id":22,"source_url":%q,"timestamps_ms":[1500],
		"prompt":"Inspect this frame.","vision_model":"@cf/test/vision",
		"vision_max_tokens":384,
		"vision_response_format":{"type":"json_schema","json_schema":{"type":"object","properties":{"unsafe":{"type":"boolean"}},"required":["unsafe"]}}
	}`, srv.URL+"/video.mp4")), &lease)
	if err != nil {
		t.Fatal(err)
	}
	worker := &Worker{cf: cf}
	result := worker.classifyOne(context.Background(), lease, 1500)
	if result.Error != "" {
		t.Fatal(result.Error)
	}
	request := <-seen
	if request["max_tokens"] != float64(384) || request["prompt"] != lease.Prompt {
		t.Fatalf("lease options were not forwarded: %v", request)
	}
	var wantFormat map[string]any
	if err := json.Unmarshal(lease.VisionResponseFormat, &wantFormat); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(request["response_format"], wantFormat) {
		t.Fatalf("schema changed in transit: %v", request["response_format"])
	}
	if !reflect.DeepEqual(request["image"], []any{float64(255), float64(216), float64(0), float64(1)}) {
		t.Fatal("vision request did not contain the fetched bytes")
	}
	var answer map[string]any
	if err := json.Unmarshal([]byte(result.Raw), &answer); err != nil {
		t.Fatal(err)
	}
	if answer["description"] != "A desk." || answer["unsafe"] != false || result.AtMS != 1500 {
		t.Fatalf("object answer or timestamp changed: %+v", result)
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
