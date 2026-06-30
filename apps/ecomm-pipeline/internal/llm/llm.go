// Package llm wraps langchaingo's Ollama provider so the pipeline stages can
// ask the shared home LLM cluster (via HAProxy) for text and JSON, without
// caring which node actually answers.
package llm

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"

	"github.com/tmc/langchaingo/llms"
	"github.com/tmc/langchaingo/llms/ollama"
)

// Client is a thin, JSON-friendly wrapper over a langchaingo model.
type Client struct {
	model llms.Model
	name  string
}

// New connects to the cluster's Ollama endpoint. baseURL is the HAProxy URL
// (e.g. http://edge-master:11434 or whatever `edge` injects from fleet.json);
// model is an Ollama model tag.
func New(baseURL, model string) (*Client, error) {
	m, err := ollama.New(
		ollama.WithServerURL(baseURL),
		ollama.WithModel(model),
	)
	if err != nil {
		return nil, fmt.Errorf("ollama init: %w", err)
	}
	return &Client{model: m, name: model}, nil
}

// Complete returns the model's free-text answer to a single prompt.
func (c *Client) Complete(ctx context.Context, prompt string, temperature float64) (string, error) {
	return llms.GenerateFromSinglePrompt(ctx, c.model, prompt, llms.WithTemperature(temperature))
}

// CompleteJSON asks the model to answer as JSON and unmarshals it into out.
// It appends a strict instruction and tolerates models that wrap JSON in
// Markdown code fences or add chatter around it.
func (c *Client) CompleteJSON(ctx context.Context, prompt string, temperature float64, out any) error {
	full := prompt + "\n\nRespond with ONLY a single valid JSON object. No prose, no Markdown, no code fences."
	raw, err := c.Complete(ctx, full, temperature)
	if err != nil {
		return err
	}
	clean := extractJSON(raw)
	if err := json.Unmarshal([]byte(clean), out); err != nil {
		return fmt.Errorf("parse JSON from model: %w\n--- raw ---\n%s", err, raw)
	}
	return nil
}

// extractJSON pulls the first {...} object out of a model response, stripping
// code fences and surrounding chatter.
func extractJSON(s string) string {
	s = strings.TrimSpace(s)
	if i := strings.Index(s, "```"); i >= 0 {
		s = s[i+3:]
		if j := strings.IndexByte(s, '\n'); j >= 0 { // drop an optional ```json language tag
			s = s[j+1:]
		}
		if k := strings.Index(s, "```"); k >= 0 {
			s = s[:k]
		}
	}
	start := strings.IndexByte(s, '{')
	end := strings.LastIndexByte(s, '}')
	if start >= 0 && end > start {
		return s[start : end+1]
	}
	return strings.TrimSpace(s)
}
