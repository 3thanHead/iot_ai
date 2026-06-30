package web

import (
	"log"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/3thanHead/iot_ai/ecomm-pipeline/internal/config"
	"github.com/3thanHead/iot_ai/ecomm-pipeline/internal/model"
	"github.com/3thanHead/iot_ai/ecomm-pipeline/internal/store"
)

type noopActions struct{}

func (noopActions) Approve(string) error            { return nil }
func (noopActions) Regenerate(string, string) error { return nil }
func (noopActions) Reject(string, string) error     { return nil }

func newTestServer(t *testing.T) (*Server, store.Store) {
	t.Helper()
	st, err := store.Open(filepath.Join(t.TempDir(), "jobs.db"))
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	t.Cleanup(func() { st.Close() })
	s, err := NewServer(config.Config{BrandName: "TestBrand"}, st, noopActions{}, log.New(testWriter{t}, "", 0))
	if err != nil {
		t.Fatalf("new server: %v", err)
	}
	return s, st
}

type testWriter struct{ t *testing.T }

func (w testWriter) Write(p []byte) (int, error) {
	w.t.Log(strings.TrimRight(string(p), "\n"))
	return len(p), nil
}

// fullJob exercises every template branch (niche, product, artwork, listing,
// external ids, feedback) while parked at a gate.
func fullJob() *model.Job {
	now := time.Now()
	return &model.Job{
		ID: "job_test", Stage: model.StageReviewListing, Status: model.StatusAwaiting,
		Niche:     &model.Niche{Name: "Cottagecore Beekeepers", Audience: "hobby apiarists", Competition: "low", Keywords: []string{"bees", "honey"}},
		Product:   &model.Product{Type: "unisex t-shirt", Theme: "honeycomb geometry", BrandAngle: "cozy", ArtPrompt: "flat vector honeycomb"},
		Artwork:   &model.Artwork{File: "job_test-1.png", Prompt: "flat vector honeycomb", Backend: "stub"},
		Listing:   &model.Listing{Title: "Bee Happy Tee", Description: "Soft cotton.", Bullets: []string{"Unisex fit"}, Tags: []string{"bees"}, PriceUSD: 24.99, Mockups: []string{"https://example.invalid/m.png"}},
		External:  &model.External{PODProductID: "pod_x", ListingID: "lst_y", ListingURL: "https://example.invalid/itm/lst_y"},
		Feedback:  []model.FeedbackNote{{Stage: model.StageReviewArtwork, Action: "regenerate", Comment: "brighter", At: now}},
		CreatedAt: now, UpdatedAt: now,
	}
}

func TestDashboardRenders(t *testing.T) {
	s, st := newTestServer(t)
	if err := st.Save(fullJob()); err != nil {
		t.Fatal(err)
	}
	rec := httptest.NewRecorder()
	s.Handler().ServeHTTP(rec, httptest.NewRequest("GET", "/", nil))
	if rec.Code != 200 {
		t.Fatalf("dashboard status %d: %s", rec.Code, rec.Body.String())
	}
	body := rec.Body.String()
	for _, want := range []string{"TestBrand", "Cottagecore Beekeepers", "Needs your approval", "/jobs/job_test/regenerate"} {
		if !strings.Contains(body, want) {
			t.Errorf("dashboard missing %q", want)
		}
	}
}

func TestJobDetailRenders(t *testing.T) {
	s, st := newTestServer(t)
	if err := st.Save(fullJob()); err != nil {
		t.Fatal(err)
	}
	rec := httptest.NewRecorder()
	s.Handler().ServeHTTP(rec, httptest.NewRequest("GET", "/jobs/job_test", nil))
	if rec.Code != 200 {
		t.Fatalf("job detail status %d: %s", rec.Code, rec.Body.String())
	}
	body := rec.Body.String()
	for _, want := range []string{"Bee Happy Tee", "honeycomb geometry", "Your decision", "Feedback history", "brighter"} {
		if !strings.Contains(body, want) {
			t.Errorf("job detail missing %q", want)
		}
	}
}
