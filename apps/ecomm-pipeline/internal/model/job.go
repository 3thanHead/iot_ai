// Package model defines the data that flows through the print-on-demand
// pipeline: a Job and the stage-specific payloads it accumulates as it moves
// from "idea" to "listed for sale".
package model

import "time"

// Stage is where a Job currently sits in the pipeline. Stages run in order; a
// Job advances one stage at a time. The two "gate" stages
// (StageReviewArtwork / StageReviewListing, and optionally StageReviewNiche)
// park the Job until a human approves it from the dashboard.
type Stage string

const (
	StageDiscoverNiche Stage = "discover_niche"   // LLM brainstorms a low-competition niche
	StageReviewNiche   Stage = "review_niche"     // GATE: approve the niche + product direction
	StageSelectProduct Stage = "select_product"   // LLM picks a POD product + brand angle
	StageArtwork       Stage = "generate_artwork" // Stable Diffusion renders the design
	StageReviewArtwork Stage = "review_artwork"   // GATE: approve / regenerate the artwork
	StageListing       Stage = "generate_listing" // POD mockups + LLM listing copy
	StageReviewListing Stage = "review_listing"   // GATE: approve / regenerate the listing
	StageUploadPOD     Stage = "upload_pod"       // push product to the POD provider
	StageListMarket    Stage = "list_marketplace" // create the marketplace (eBay) listing
	StageComplete      Stage = "complete"         // terminal: listed for sale
)

// Order is the canonical stage sequence. The engine uses it to find the stage
// after a gate is approved.
var Order = []Stage{
	StageDiscoverNiche,
	StageReviewNiche,
	StageSelectProduct,
	StageArtwork,
	StageReviewArtwork,
	StageListing,
	StageReviewListing,
	StageUploadPOD,
	StageListMarket,
	StageComplete,
}

// IsGate reports whether a stage is a human-approval gate.
func (s Stage) IsGate() bool {
	switch s {
	case StageReviewNiche, StageReviewArtwork, StageReviewListing:
		return true
	}
	return false
}

// Label is a human-friendly name for the dashboard.
func (s Stage) Label() string {
	switch s {
	case StageDiscoverNiche:
		return "Discovering niche"
	case StageReviewNiche:
		return "Review niche"
	case StageSelectProduct:
		return "Selecting product"
	case StageArtwork:
		return "Generating artwork"
	case StageReviewArtwork:
		return "Review artwork"
	case StageListing:
		return "Generating listing"
	case StageReviewListing:
		return "Review listing"
	case StageUploadPOD:
		return "Uploading to POD"
	case StageListMarket:
		return "Listing on marketplace"
	case StageComplete:
		return "Complete"
	}
	return string(s)
}

// Status is the lifecycle state of a Job within its current stage.
type Status string

const (
	StatusRunning    Status = "running"              // engine is (or will be) working this stage
	StatusAwaiting   Status = "awaiting_approval"    // parked at a gate, waiting on a human
	StatusRegenerate Status = "regenerate_requested" // human sent it back; re-run the producing stage
	StatusRejected   Status = "rejected"             // human killed it; terminal
	StatusError      Status = "error"                // stage failed; engine will retry with backoff
	StatusDone       Status = "done"                 // reached StageComplete; terminal
)

// Terminal reports whether the Job will never change again on its own.
func (s Status) Terminal() bool { return s == StatusRejected || s == StatusDone }

// Niche is the output of the discovery stage.
type Niche struct {
	Name        string   `json:"name"`
	Audience    string   `json:"audience"`
	Rationale   string   `json:"rationale"`   // why it's low-competition / promising
	Competition string   `json:"competition"` // "low" | "medium" | "high"
	Keywords    []string `json:"keywords"`
}

// Product is the output of the product-selection stage.
type Product struct {
	Type       string `json:"type"`        // e.g. "unisex t-shirt", "ceramic mug"
	Theme      string `json:"theme"`       // the design concept for this product
	BrandAngle string `json:"brand_angle"` // how it fits the brand voice
	ArtPrompt  string `json:"art_prompt"`  // the text→image prompt for Stable Diffusion
}

// Artwork is the output of the artwork stage.
type Artwork struct {
	File    string `json:"file"`    // filename under the data/art dir
	Prompt  string `json:"prompt"`  // the prompt actually used (may include regen feedback)
	Backend string `json:"backend"` // "stable-diffusion" | "stub"
}

// Listing is the output of the listing stage.
type Listing struct {
	Title       string   `json:"title"`
	Description string   `json:"description"`
	Bullets     []string `json:"bullets"`
	Tags        []string `json:"tags"`
	PriceUSD    float64  `json:"price_usd"`
	Mockups     []string `json:"mockups"` // mockup image URLs from the POD provider
}

// FeedbackNote records a human's regenerate/reject comment at a gate.
type FeedbackNote struct {
	Stage   Stage     `json:"stage"`
	Action  string    `json:"action"` // "regenerate" | "reject"
	Comment string    `json:"comment"`
	At      time.Time `json:"at"`
}

// External holds IDs returned by external systems once the Job is published.
type External struct {
	PODProductID string `json:"pod_product_id"`
	ListingID    string `json:"listing_id"`
	ListingURL   string `json:"listing_url"`
}

// Job is one product idea moving through the pipeline.
type Job struct {
	ID     string `json:"id"`
	Stage  Stage  `json:"stage"`
	Status Status `json:"status"`

	Niche    *Niche    `json:"niche,omitempty"`
	Product  *Product  `json:"product,omitempty"`
	Artwork  *Artwork  `json:"artwork,omitempty"`
	Listing  *Listing  `json:"listing,omitempty"`
	External *External `json:"external,omitempty"`

	Feedback []FeedbackNote `json:"feedback,omitempty"`
	LastErr  string         `json:"last_err,omitempty"`
	Attempts int            `json:"attempts,omitempty"` // failures at the current stage

	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
}

// PendingFeedback returns the most recent unconsumed regenerate comment, if the
// Job was just sent back. The producing stage uses it to steer the retry.
func (j *Job) PendingFeedback() string {
	for i := len(j.Feedback) - 1; i >= 0; i-- {
		if j.Feedback[i].Action == "regenerate" {
			return j.Feedback[i].Comment
		}
	}
	return ""
}
