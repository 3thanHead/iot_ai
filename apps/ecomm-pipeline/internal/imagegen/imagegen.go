// Package imagegen renders product artwork. The default backend calls a local
// Stable Diffusion service over the Automatic1111-compatible txt2img HTTP API;
// when no SD endpoint is configured it falls back to a placeholder generator so
// the rest of the pipeline still runs end-to-end.
package imagegen

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"image"
	"image/color"
	"image/png"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"time"
)

// Generator renders a text prompt to a PNG written at outPath. It returns a
// short backend identifier for display ("stable-diffusion" or "stub").
type Generator interface {
	Generate(ctx context.Context, prompt, outPath string) (backend string, err error)
}

// New returns the Stable Diffusion client when sdBaseURL is set, otherwise the
// placeholder stub.
func New(sdBaseURL string, steps int) Generator {
	if sdBaseURL == "" {
		return &stub{}
	}
	return &a1111{baseURL: sdBaseURL, steps: steps, http: &http.Client{Timeout: 5 * time.Minute}}
}

// ---- Stable Diffusion (Automatic1111 / ComfyUI txt2img) ----

type a1111 struct {
	baseURL string
	steps   int
	http    *http.Client
}

type txt2imgReq struct {
	Prompt         string `json:"prompt"`
	NegativePrompt string `json:"negative_prompt"`
	Steps          int    `json:"steps"`
	Width          int    `json:"width"`
	Height         int    `json:"height"`
	CFGScale       int    `json:"cfg_scale"`
}

type txt2imgResp struct {
	Images []string `json:"images"` // base64-encoded PNGs
}

func (a *a1111) Generate(ctx context.Context, prompt, outPath string) (string, error) {
	body, _ := json.Marshal(txt2imgReq{
		Prompt:         prompt,
		NegativePrompt: "photorealistic, busy background, watermark, text artifacts, low quality, blurry",
		Steps:          a.steps,
		Width:          1024,
		Height:         1024,
		CFGScale:       7,
	})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, a.baseURL+"/sdapi/v1/txt2img", bytes.NewReader(body))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := a.http.Do(req)
	if err != nil {
		return "", fmt.Errorf("call stable-diffusion: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		msg, _ := io.ReadAll(io.LimitReader(resp.Body, 2048))
		return "", fmt.Errorf("stable-diffusion %s: %s", resp.Status, msg)
	}

	var out txt2imgResp
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return "", fmt.Errorf("decode sd response: %w", err)
	}
	if len(out.Images) == 0 {
		return "", fmt.Errorf("stable-diffusion returned no images")
	}
	raw, err := base64.StdEncoding.DecodeString(out.Images[0])
	if err != nil {
		return "", fmt.Errorf("decode sd image: %w", err)
	}
	if err := writeFile(outPath, raw); err != nil {
		return "", err
	}
	return "stable-diffusion", nil
}

// ---- Placeholder stub ----

type stub struct{}

// Generate writes a deterministic coloured placeholder PNG so reviewers see a
// distinct image per prompt even without a real image model.
func (s *stub) Generate(_ context.Context, prompt, outPath string) (string, error) {
	const size = 1024
	img := image.NewRGBA(image.Rect(0, 0, size, size))
	h := sha256.Sum256([]byte(prompt))
	bg := color.RGBA{R: h[0], G: h[1], B: h[2], A: 255}
	fg := color.RGBA{R: 255 - h[0], G: 255 - h[1], B: 255 - h[2], A: 255}
	for y := 0; y < size; y++ {
		for x := 0; x < size; x++ {
			// Simple diagonal-stripe motif keyed off the prompt hash.
			if ((x/64)+(y/64)+int(h[3]))%2 == 0 {
				img.Set(x, y, bg)
			} else {
				img.Set(x, y, fg)
			}
		}
	}
	var buf bytes.Buffer
	if err := png.Encode(&buf, img); err != nil {
		return "", err
	}
	if err := writeFile(outPath, buf.Bytes()); err != nil {
		return "", err
	}
	return "stub", nil
}

func writeFile(path string, data []byte) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	return os.WriteFile(path, data, 0o644)
}
