package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func testConfig() Config {
	return Config{MaxNotional: 1_000_000, MaxQuantity: 10_000, AllowedSymbols: map[string]bool{}}
}

func TestEvaluate(t *testing.T) {
	cfg := testConfig()
	cases := []struct {
		name     string
		req      CheckRequest
		approved bool
		reason   string
	}{
		{"ok", CheckRequest{"a1", "INFY", "BUY", 10, 1500}, true, "within limits"},
		{"no account", CheckRequest{"", "INFY", "BUY", 10, 1500}, false, "missing account_id"},
		{"bad side", CheckRequest{"a1", "INFY", "HOLD", 10, 1500}, false, "side must be"},
		{"zero qty", CheckRequest{"a1", "INFY", "BUY", 0, 1500}, false, "must be positive"},
		{"qty limit", CheckRequest{"a1", "INFY", "SELL", 20_000, 1}, false, "quantity above limit"},
		{"notional limit", CheckRequest{"a1", "INFY", "BUY", 1000, 1500}, false, "notional above limit"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			d := Evaluate(cfg, tc.req)
			if d.Approved != tc.approved || !strings.Contains(d.Reason, tc.reason) {
				t.Fatalf("got %+v, want approved=%v reason~%q", d, tc.approved, tc.reason)
			}
		})
	}
}

func TestAllowedSymbols(t *testing.T) {
	cfg := testConfig()
	cfg.AllowedSymbols = map[string]bool{"TCS": true}
	if d := Evaluate(cfg, CheckRequest{"a1", "INFY", "BUY", 1, 1}); d.Approved {
		t.Fatal("INFY should be rejected when only TCS is allowed")
	}
}

func TestCheckEndpointAndMetrics(t *testing.T) {
	m := newMetrics()
	srv := httptest.NewServer(newMux(testConfig(), m))
	defer srv.Close()

	body, _ := json.Marshal(CheckRequest{"a1", "INFY", "BUY", 10, 1500})
	resp, err := http.Post(srv.URL+"/v1/check", "application/json", bytes.NewReader(body))
	if err != nil || resp.StatusCode != http.StatusOK {
		t.Fatalf("check failed: %v %v", err, resp.Status)
	}
	var d Decision
	_ = json.NewDecoder(resp.Body).Decode(&d)
	if !d.Approved {
		t.Fatalf("expected approval, got %+v", d)
	}

	bad, _ := http.Post(srv.URL+"/v1/check", "application/json", strings.NewReader("{"))
	if bad.StatusCode != http.StatusBadRequest {
		t.Fatalf("invalid JSON should be 400, got %d", bad.StatusCode)
	}

	mresp, _ := http.Get(srv.URL + "/metrics")
	buf := new(bytes.Buffer)
	_, _ = buf.ReadFrom(mresp.Body)
	for _, want := range []string{`code="200"} 1`, `code="400"} 1`, "http_request_duration_seconds_count"} {
		if !strings.Contains(buf.String(), want) {
			t.Fatalf("metrics missing %q:\n%s", want, buf.String())
		}
	}
}
