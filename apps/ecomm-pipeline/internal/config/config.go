// Package config loads all runtime settings from the environment, matching the
// monorepo convention that each app is configured entirely through its .env.
package config

import (
	"os"
	"strconv"
	"strings"
	"time"
)

// Config is the fully-resolved runtime configuration.
type Config struct {
	// LLM cluster (HAProxy endpoint, Ollama native API — same as every app).
	LLMBaseURL string
	LLMModel   string

	// Stable Diffusion (Automatic1111-compatible txt2img). Empty => use the
	// placeholder stub generator so the pipeline still runs end-to-end.
	SDBaseURL string
	SDSteps   int

	// Brand identity injected into prompts so output stays on-brand.
	BrandName       string
	BrandVoice      string
	BrandGuidelines string

	// Pipeline behaviour.
	TickInterval time.Duration // how often the engine wakes up
	WIPLimit     int           // max concurrent in-flight (non-terminal) jobs
	GateNiche    bool          // pause for niche approval before product selection

	// Server + storage.
	Addr    string // internal listen address, e.g. ":8810" (compose maps the host port)
	DataDir string // bbolt DB + generated artwork live here
}

// Load reads the environment and applies sensible defaults.
func Load() Config {
	return Config{
		LLMBaseURL: env("LLM_BASE_URL", "http://localhost:11434"),
		LLMModel:   env("LLM_MODEL", "llama3.2:3b"),

		SDBaseURL: env("SD_BASE_URL", ""),
		SDSteps:   envInt("SD_STEPS", 28),

		BrandName:       env("BRAND_NAME", "Driftwell"),
		BrandVoice:      env("BRAND_VOICE", "playful, minimalist, a little nerdy"),
		BrandGuidelines: env("BRAND_GUIDELINES", "Clean vector-style designs, bold readable typography, limited 2-3 colour palettes, no photorealism, no busy backgrounds."),

		TickInterval: envDur("TICK_INTERVAL", 15*time.Second),
		WIPLimit:     envInt("WIP_LIMIT", 3),
		GateNiche:    envBool("GATE_NICHE", true),

		Addr:    env("LISTEN_ADDR", ":8810"),
		DataDir: env("DATA_DIR", "/data"),
	}
}

func env(k, def string) string {
	if v := strings.TrimSpace(os.Getenv(k)); v != "" {
		return v
	}
	return def
}

func envInt(k string, def int) int {
	if v := strings.TrimSpace(os.Getenv(k)); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

func envBool(k string, def bool) bool {
	if v := strings.TrimSpace(os.Getenv(k)); v != "" {
		if b, err := strconv.ParseBool(v); err == nil {
			return b
		}
	}
	return def
}

func envDur(k string, def time.Duration) time.Duration {
	if v := strings.TrimSpace(os.Getenv(k)); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			return d
		}
	}
	return def
}
