// Command ecomm-pipeline runs the automated print-on-demand pipeline: a
// long-lived engine that discovers niches, designs products, and lists them for
// sale, pausing at human-approval gates surfaced on a web dashboard.
package main

import (
	"context"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/3thanHead/iot_ai/ecomm-pipeline/internal/config"
	"github.com/3thanHead/iot_ai/ecomm-pipeline/internal/imagegen"
	"github.com/3thanHead/iot_ai/ecomm-pipeline/internal/llm"
	"github.com/3thanHead/iot_ai/ecomm-pipeline/internal/marketplace"
	"github.com/3thanHead/iot_ai/ecomm-pipeline/internal/pipeline"
	"github.com/3thanHead/iot_ai/ecomm-pipeline/internal/pod"
	"github.com/3thanHead/iot_ai/ecomm-pipeline/internal/store"
	"github.com/3thanHead/iot_ai/ecomm-pipeline/internal/web"
)

func main() {
	lg := log.New(os.Stdout, "", log.LstdFlags)
	cfg := config.Load()

	if err := os.MkdirAll(filepath.Join(cfg.DataDir, "art"), 0o755); err != nil {
		lg.Fatalf("create data dir: %v", err)
	}

	st, err := store.Open(filepath.Join(cfg.DataDir, "jobs.db"))
	if err != nil {
		lg.Fatalf("open store: %v", err)
	}
	defer st.Close()

	llmClient, err := llm.New(cfg.LLMBaseURL, cfg.LLMModel)
	if err != nil {
		lg.Fatalf("llm: %v", err)
	}
	img := imagegen.New(cfg.SDBaseURL, cfg.SDSteps)
	provider := pod.New()
	channel := marketplace.New()

	pl := pipeline.NewPipeline(cfg, llmClient, img, provider, channel)
	engine := pipeline.NewEngine(cfg, st, pl, lg)

	srv, err := web.NewServer(cfg, st, engine, lg)
	if err != nil {
		lg.Fatalf("web: %v", err)
	}

	// Cancel on SIGINT/SIGTERM so the engine + HTTP server shut down cleanly.
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	go engine.Run(ctx)

	httpSrv := &http.Server{Addr: cfg.Addr, Handler: srv.Handler()}
	go func() {
		lg.Printf("dashboard on %s  (LLM=%s model=%s, SD=%q)", cfg.Addr, cfg.LLMBaseURL, cfg.LLMModel, cfg.SDBaseURL)
		if err := httpSrv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			lg.Fatalf("http: %v", err)
		}
	}()

	<-ctx.Done()
	lg.Printf("shutting down")
	shutCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = httpSrv.Shutdown(shutCtx)
}
