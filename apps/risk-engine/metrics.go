package main

// A tiny Prometheus text-format exporter using only the standard library: request counts by
// status and a latency histogram. Enough for SLOs and canary analysis without extra dependencies.

import (
	"fmt"
	"net/http"
	"sort"
	"strconv"
	"sync"
	"time"
)

var buckets = []float64{0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5}

type metrics struct {
	mu       sync.Mutex
	requests map[string]map[int]uint64 // route -> status -> count
	hist     map[string][]uint64       // route -> cumulative bucket counts (+Inf last)
	sum      map[string]float64
	count    map[string]uint64
}

func newMetrics() *metrics {
	return &metrics{
		requests: map[string]map[int]uint64{},
		hist:     map[string][]uint64{},
		sum:      map[string]float64{},
		count:    map[string]uint64{},
	}
}

type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (s *statusRecorder) WriteHeader(code int) { s.status = code; s.ResponseWriter.WriteHeader(code) }

func (m *metrics) instrument(route string, next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		next(rec, r)
		m.observe(route, rec.status, time.Since(start).Seconds())
	}
}

func (m *metrics) observe(route string, status int, seconds float64) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.requests[route] == nil {
		m.requests[route] = map[int]uint64{}
		m.hist[route] = make([]uint64, len(buckets)+1)
	}
	m.requests[route][status]++
	for i, b := range buckets {
		if seconds <= b {
			m.hist[route][i]++
		}
	}
	m.hist[route][len(buckets)]++
	m.sum[route] += seconds
	m.count[route]++
}

func (m *metrics) ServeHTTP(w http.ResponseWriter, _ *http.Request) {
	m.mu.Lock()
	defer m.mu.Unlock()
	w.Header().Set("Content-Type", "text/plain; version=0.0.4")
	fmt.Fprintln(w, "# HELP http_requests_total Total HTTP requests by route and status.")
	fmt.Fprintln(w, "# TYPE http_requests_total counter")
	routes := make([]string, 0, len(m.requests))
	for r := range m.requests {
		routes = append(routes, r)
	}
	sort.Strings(routes)
	for _, route := range routes {
		for status, n := range m.requests[route] {
			fmt.Fprintf(w, "http_requests_total{handler=%q,status=\"%dxx\",code=\"%d\"} %d\n",
				route, status/100, status, n)
		}
	}
	fmt.Fprintln(w, "# HELP http_request_duration_seconds Request latency.")
	fmt.Fprintln(w, "# TYPE http_request_duration_seconds histogram")
	for _, route := range routes {
		for i, b := range buckets {
			fmt.Fprintf(w, "http_request_duration_seconds_bucket{handler=%q,le=%q} %d\n",
				route, strconv.FormatFloat(b, 'f', -1, 64), m.hist[route][i])
		}
		fmt.Fprintf(w, "http_request_duration_seconds_bucket{handler=%q,le=\"+Inf\"} %d\n",
			route, m.hist[route][len(buckets)])
		fmt.Fprintf(w, "http_request_duration_seconds_sum{handler=%q} %g\n", route, m.sum[route])
		fmt.Fprintf(w, "http_request_duration_seconds_count{handler=%q} %d\n", route, m.count[route])
	}
}
