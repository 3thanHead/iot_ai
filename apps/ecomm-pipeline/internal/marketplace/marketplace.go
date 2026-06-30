// Package marketplace abstracts the sales channel where finished products are
// listed. eBay is the first target; only a stub exists today.
package marketplace

import (
	"context"
	"crypto/sha1"
	"fmt"
)

// Listing is the channel-agnostic listing payload.
type Listing struct {
	Title        string
	Description  string
	PriceUSD     float64
	ImageURLs    []string
	PODProductID string
}

// Result is what the channel returns once the listing is live.
type Result struct {
	ListingID string
	URL       string
}

// Channel publishes a Listing to a marketplace.
type Channel interface {
	CreateListing(ctx context.Context, l Listing) (Result, error)
	Name() string
}

// New returns the configured channel. Today that is always the eBay stub; wire
// the real eBay Sell API client here once OAuth credentials exist.
func New() Channel { return &ebayStub{} }

// ---- eBay stub ----

type ebayStub struct{}

func (e *ebayStub) Name() string { return "ebay-stub" }

// CreateListing fakes publishing to eBay and returns a stable fake id + URL.
//
// To implement the real eBay channel:
//   - Obtain a user OAuth token (Sell scopes); refresh as needed.
//   - Create/replace an inventory item, then publish an offer via the Sell
//     Inventory API; map our Listing fields to the offer + item aspects.
//   - Keep client id/secret and the refresh token in the environment.
func (e *ebayStub) CreateListing(_ context.Context, l Listing) (Result, error) {
	id := fmt.Sprintf("%x", sha1.Sum([]byte(l.Title+l.PODProductID)))[:12]
	return Result{
		ListingID: id,
		URL:       "https://example.invalid/ebay/itm/" + id,
	}, nil
}
