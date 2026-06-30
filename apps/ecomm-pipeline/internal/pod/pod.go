// Package pod abstracts the print-on-demand provider (Printful, Printify, …).
// Only a stub is implemented today; real adapters slot in behind Provider
// without touching the pipeline.
package pod

import (
	"context"
	"crypto/sha1"
	"fmt"
)

// Product is what we publish: a chosen blank + the artwork to print + copy.
type Product struct {
	Title       string
	ProductType string // e.g. "unisex t-shirt"
	Description string
	PriceUSD    float64
	ArtPath     string // local path to the print-ready PNG
}

// Provider previews mockups and publishes products to a POD service.
type Provider interface {
	// Mockups renders preview images for artwork without publishing — used at
	// the listing-review gate so the human sees the product before it goes live.
	Mockups(ctx context.Context, artPath string) ([]string, error)
	// CreateProduct publishes a sellable product and returns its provider id.
	CreateProduct(ctx context.Context, p Product) (productID string, err error)
	Name() string
}

// New returns the configured provider. Today that is always the stub; when a
// real key/account exists, branch here on an env-selected provider name and
// return a Printful/Printify client implementing Provider.
func New() Provider { return &stub{} }

// ---- stub ----

type stub struct{}

func (s *stub) Name() string { return "stub-pod" }

// Mockups fakes the provider's mockup generator with placeholder URLs derived
// from the artwork path.
//
// Real provider (e.g. Printful): upload the file, call the mockup-generator
// task endpoint, poll until the mockups are ready, return their URLs.
func (s *stub) Mockups(_ context.Context, artPath string) ([]string, error) {
	id := hash(artPath)
	return []string{
		"https://example.invalid/mockups/" + id + "_front.png",
		"https://example.invalid/mockups/" + id + "_lifestyle.png",
	}, nil
}

// CreateProduct fakes publishing and returns a stable fake product id.
//
// Real provider: create a sync product with the chosen variant ids + the
// uploaded artwork; read the API key from the environment (never source).
func (s *stub) CreateProduct(_ context.Context, p Product) (string, error) {
	return "pod_" + hash(p.Title+p.ArtPath), nil
}

func hash(s string) string {
	return fmt.Sprintf("%x", sha1.Sum([]byte(s)))[:16]
}
