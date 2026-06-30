package pipeline

import (
	"context"
	"fmt"
	"path/filepath"
	"strings"
	"time"

	"github.com/3thanHead/iot_ai/ecomm-pipeline/internal/config"
	"github.com/3thanHead/iot_ai/ecomm-pipeline/internal/imagegen"
	"github.com/3thanHead/iot_ai/ecomm-pipeline/internal/llm"
	"github.com/3thanHead/iot_ai/ecomm-pipeline/internal/marketplace"
	"github.com/3thanHead/iot_ai/ecomm-pipeline/internal/model"
	"github.com/3thanHead/iot_ai/ecomm-pipeline/internal/pod"
)

// Pipeline holds the stage dependencies and implements the per-stage work.
// Each run* method mutates only the Job's payload fields; the engine owns
// stage/status transitions.
type Pipeline struct {
	cfg    config.Config
	llm    *llm.Client
	img    imagegen.Generator
	pod    pod.Provider
	market marketplace.Channel
}

func NewPipeline(cfg config.Config, l *llm.Client, img imagegen.Generator, p pod.Provider, m marketplace.Channel) *Pipeline {
	return &Pipeline{cfg: cfg, llm: l, img: img, pod: p, market: m}
}

// run dispatches to the producing stage for the job's current stage. Gate
// stages are never passed here (the engine parks them).
func (p *Pipeline) run(ctx context.Context, j *model.Job) error {
	switch j.Stage {
	case model.StageDiscoverNiche:
		return p.discoverNiche(ctx, j)
	case model.StageSelectProduct:
		return p.selectProduct(ctx, j)
	case model.StageArtwork:
		return p.generateArtwork(ctx, j)
	case model.StageListing:
		return p.generateListing(ctx, j)
	case model.StageUploadPOD:
		return p.uploadPOD(ctx, j)
	case model.StageListMarket:
		return p.listMarketplace(ctx, j)
	default:
		return fmt.Errorf("no producer for stage %q", j.Stage)
	}
}

// feedback returns the human's regenerate note when the job is being re-run,
// otherwise "". Stages weave it into their prompt to honour the correction.
func feedback(j *model.Job) string {
	if j.Status == model.StatusRegenerate {
		return j.PendingFeedback()
	}
	return ""
}

func (p *Pipeline) brandBlock() string {
	return fmt.Sprintf("Brand: %s\nVoice: %s\nDesign guidelines: %s",
		p.cfg.BrandName, p.cfg.BrandVoice, p.cfg.BrandGuidelines)
}

// ---- Stage 1: discover a low-competition niche ----

func (p *Pipeline) discoverNiche(ctx context.Context, j *model.Job) error {
	avoid := ""
	if fb := feedback(j); fb != "" {
		avoid = "\nThe previous suggestion was rejected with this feedback — address it: " + fb
		if j.Niche != nil {
			avoid += fmt.Sprintf("\nDo not repeat the previous niche %q.", j.Niche.Name)
		}
	}
	prompt := fmt.Sprintf(`You are a print-on-demand market researcher.
%s

Propose ONE specific, low-competition niche for print-on-demand apparel/merch.
Favour passionate but underserved micro-audiences over broad saturated topics.%s

JSON shape:
{"name": str, "audience": str, "rationale": str, "competition": "low"|"medium"|"high", "keywords": [str, ...]}`,
		p.brandBlock(), avoid)

	var n model.Niche
	if err := p.llm.CompleteJSON(ctx, prompt, 0.9, &n); err != nil {
		return err
	}
	if strings.TrimSpace(n.Name) == "" {
		return fmt.Errorf("model returned an empty niche")
	}
	j.Niche = &n
	return nil
}

// ---- Stage 2: select a product + design concept ----

func (p *Pipeline) selectProduct(ctx context.Context, j *model.Job) error {
	if j.Niche == nil {
		return fmt.Errorf("selectProduct: no niche on job")
	}
	avoid := ""
	if fb := feedback(j); fb != "" {
		avoid = "\nIncorporate this feedback from the reviewer: " + fb
	}
	prompt := fmt.Sprintf(`%s

Niche: %s
Audience: %s
Keywords: %s

Pick ONE print-on-demand product and a single strong design concept for it that
fits the brand and would appeal to this audience. The art_prompt must be a
detailed text-to-image prompt suitable for Stable Diffusion, consistent with the
design guidelines above (vector/flat style, limited palette, no photorealism).%s

JSON shape:
{"type": str, "theme": str, "brand_angle": str, "art_prompt": str}`,
		p.brandBlock(), j.Niche.Name, j.Niche.Audience, strings.Join(j.Niche.Keywords, ", "), avoid)

	var pr model.Product
	if err := p.llm.CompleteJSON(ctx, prompt, 0.8, &pr); err != nil {
		return err
	}
	if strings.TrimSpace(pr.ArtPrompt) == "" {
		return fmt.Errorf("model returned an empty art prompt")
	}
	j.Product = &pr
	return nil
}

// ---- Stage 3: generate artwork via Stable Diffusion ----

func (p *Pipeline) generateArtwork(ctx context.Context, j *model.Job) error {
	if j.Product == nil {
		return fmt.Errorf("generateArtwork: no product on job")
	}
	prompt := j.Product.ArtPrompt
	if fb := feedback(j); fb != "" {
		prompt = prompt + ". Reviewer feedback to apply: " + fb
	}
	// Fresh filename each attempt so the browser never shows a stale cached image.
	file := fmt.Sprintf("%s-%d.png", j.ID, time.Now().UnixNano())
	out := filepath.Join(p.cfg.DataDir, "art", file)

	backend, err := p.img.Generate(ctx, prompt, out)
	if err != nil {
		return err
	}
	j.Artwork = &model.Artwork{File: file, Prompt: prompt, Backend: backend}
	return nil
}

// ---- Stage 4: mockups + listing copy ----

func (p *Pipeline) generateListing(ctx context.Context, j *model.Job) error {
	if j.Product == nil || j.Artwork == nil {
		return fmt.Errorf("generateListing: missing product or artwork")
	}
	artPath := filepath.Join(p.cfg.DataDir, "art", j.Artwork.File)
	mockups, err := p.pod.Mockups(ctx, artPath)
	if err != nil {
		return fmt.Errorf("mockups: %w", err)
	}

	avoid := ""
	if fb := feedback(j); fb != "" {
		avoid = "\nRevise according to this reviewer feedback: " + fb
	}
	prompt := fmt.Sprintf(`%s

Write a marketplace listing (eBay) for this print-on-demand product.
Product: %s
Design concept: %s
Niche/audience: %s — %s

Return SEO-friendly, honest copy in the brand voice. Suggest a sensible retail
price in USD for a print-on-demand %s.%s

JSON shape:
{"title": str (<=80 chars), "description": str, "bullets": [str, ...],
 "tags": [str, ...], "price_usd": number}`,
		p.brandBlock(), j.Product.Type, j.Product.Theme,
		j.Niche.Name, j.Niche.Audience, j.Product.Type, avoid)

	var l model.Listing
	if err := p.llm.CompleteJSON(ctx, prompt, 0.7, &l); err != nil {
		return err
	}
	if strings.TrimSpace(l.Title) == "" {
		return fmt.Errorf("model returned an empty listing title")
	}
	l.Mockups = mockups
	j.Listing = &l
	return nil
}

// ---- Stage 5: publish to the POD provider ----

func (p *Pipeline) uploadPOD(ctx context.Context, j *model.Job) error {
	if j.Product == nil || j.Artwork == nil || j.Listing == nil {
		return fmt.Errorf("uploadPOD: incomplete job")
	}
	id, err := p.pod.CreateProduct(ctx, pod.Product{
		Title:       j.Listing.Title,
		ProductType: j.Product.Type,
		Description: j.Listing.Description,
		PriceUSD:    j.Listing.PriceUSD,
		ArtPath:     filepath.Join(p.cfg.DataDir, "art", j.Artwork.File),
	})
	if err != nil {
		return err
	}
	if j.External == nil {
		j.External = &model.External{}
	}
	j.External.PODProductID = id
	return nil
}

// ---- Stage 6: list on the marketplace (eBay) ----

func (p *Pipeline) listMarketplace(ctx context.Context, j *model.Job) error {
	if j.Listing == nil || j.External == nil || j.External.PODProductID == "" {
		return fmt.Errorf("listMarketplace: product not uploaded")
	}
	res, err := p.market.CreateListing(ctx, marketplace.Listing{
		Title:        j.Listing.Title,
		Description:  j.Listing.Description,
		PriceUSD:     j.Listing.PriceUSD,
		ImageURLs:    j.Listing.Mockups,
		PODProductID: j.External.PODProductID,
	})
	if err != nil {
		return err
	}
	j.External.ListingID = res.ListingID
	j.External.ListingURL = res.URL
	return nil
}
