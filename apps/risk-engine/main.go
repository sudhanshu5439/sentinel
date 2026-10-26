// risk-engine approves or rejects an order using simple pre-trade risk rules.
//
// POST /v1/check  {account_id, symbol, side, quantity, price} -> {approved, reason}
// GET  /healthz, /readyz, /metrics
package main

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"
)

// Config holds the risk limits and fault-injection knobs, all read from the environment.
type Config struct {
	Addr           string
	MaxNotional    float64         // max quantity*price for a single order
	MaxQuantity    int             // max shares in a single order
	AllowedSymbols map[string]bool // empty = all symbols allowed
	FaultLatency   time.Duration   // added delay per check (chaos scenario: slow release)
	Version        string
}

func loadConfig() Config {
	cfg := Config{
		Addr:        envOr("ADDR", ":8080"),
		MaxNotional: envFloat("MAX_NOTIONAL", 1_000_000),
		MaxQuantity: int(envFloat("MAX_QUANTITY", 10_000)),
		Version:     envOr("SERVICE_VERSION", "dev"),
	}
	cfg.FaultLatency = time.Duration(envFloat("FAULT_LATENCY_MS", 0)) * time.Millisecond
	cfg.AllowedSymbols = map[string]bool{}
	for _, s := range strings.Split(os.Getenv("ALLOWED_SYMBOLS"), ",") {
		if s = strings.TrimSpace(strings.ToUpper(s)); s != "" {
			cfg.AllowedSymbols[s] = true
		}
	}
	return cfg
}

// CheckRequest mirrors order-api's OrderRequest.
type CheckRequest struct {
	AccountID string  `json:"account_id"`
	Symbol    string  `json:"symbol"`
	Side      string  `json:"side"`
	Quantity  int     `json:"quantity"`
	Price     float64 `json:"price"`
}

type Decision struct {
	Approved bool   `json:"approved"`
	Reason   string `json:"reason"`
}

// Evaluate is a pure function so the rules are easy to unit test.
func Evaluate(cfg Config, r CheckRequest) Decision {
	switch {
	case r.AccountID == "":
		return Decision{false, "missing account_id"}
	case r.Side != "BUY" && r.Side != "SELL":
		return Decision{false, "side must be BUY or SELL"}
	case r.Quantity <= 0 || r.Price <= 0:
		return Decision{false, "quantity and price must be positive"}
	case len(cfg.AllowedSymbols) > 0 && !cfg.AllowedSymbols[r.Symbol]:
		return Decision{false, "symbol not tradable"}
	case r.Quantity > cfg.MaxQuantity:
		return Decision{false, "quantity above limit " + strconv.Itoa(cfg.MaxQuantity)}
	case float64(r.Quantity)*r.Price > cfg.MaxNotional:
		return Decision{false, "notional above limit " + strconv.FormatFloat(cfg.MaxNotional, 'f', 0, 64)}
	}
	return Decision{true, "within limits"}
}

func newMux(cfg Config, m *metrics) *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	mux.HandleFunc("GET /readyz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]bool{"ready": true}) // no dependencies
	})
	mux.Handle("GET /metrics", m)
	mux.HandleFunc("POST /v1/check", m.instrument("/v1/check", func(w http.ResponseWriter, r *http.Request) {
		if cfg.FaultLatency > 0 {
			time.Sleep(cfg.FaultLatency)
		}
		var req CheckRequest
		r.Body = http.MaxBytesReader(w, r.Body, 1<<16)
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"detail": "invalid JSON"})
			return
		}
		d := Evaluate(cfg, req)
		slog.Info("risk check", "account", req.AccountID, "symbol", req.Symbol,
			"qty", req.Quantity, "approved", d.Approved, "reason", d.Reason)
		writeJSON(w, http.StatusOK, d)
	}))
	return mux
}

func main() {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, nil)).With(
		"service", "risk-engine", "version", envOr("SERVICE_VERSION", "dev")))
	cfg := loadConfig()
	srv := &http.Server{
		Addr:              cfg.Addr,
		Handler:           newMux(cfg, newMetrics()),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       10 * time.Second,
		WriteTimeout:      15 * time.Second,
	}

	// Graceful shutdown: on SIGTERM (pod termination) finish in-flight requests first.
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	go func() {
		slog.Info("listening", "addr", cfg.Addr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			slog.Error("server failed", "err", err)
			os.Exit(1)
		}
	}()
	<-ctx.Done()
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_ = srv.Shutdown(shutdownCtx)
	slog.Info("stopped")
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func envFloat(key string, def float64) float64 {
	if v, err := strconv.ParseFloat(os.Getenv(key), 64); err == nil {
		return v
	}
	return def
}
