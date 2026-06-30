package pipeline

import (
	"testing"

	"github.com/3thanHead/iot_ai/ecomm-pipeline/internal/config"
	"github.com/3thanHead/iot_ai/ecomm-pipeline/internal/model"
)

func TestStageAfter(t *testing.T) {
	cases := map[model.Stage]model.Stage{
		model.StageDiscoverNiche: model.StageReviewNiche,
		model.StageReviewArtwork: model.StageListing,
		model.StageListMarket:    model.StageComplete,
		model.StageComplete:      model.StageComplete, // terminal stays put
	}
	for in, want := range cases {
		if got := stageAfter(in); got != want {
			t.Errorf("stageAfter(%s) = %s, want %s", in, got, want)
		}
	}
}

func TestProducerOf(t *testing.T) {
	cases := map[model.Stage]model.Stage{
		model.StageReviewNiche:   model.StageDiscoverNiche,
		model.StageReviewArtwork: model.StageArtwork,
		model.StageReviewListing: model.StageListing,
	}
	for gate, want := range cases {
		got, ok := producerOf(gate)
		if !ok || got != want {
			t.Errorf("producerOf(%s) = %s,%v want %s,true", gate, got, ok, want)
		}
	}
	if _, ok := producerOf(model.StageArtwork); ok {
		t.Errorf("producerOf(non-gate) should be false")
	}
}

func TestAdvanceFrom_NicheGate(t *testing.T) {
	// Gate on: discovery parks at the niche review gate.
	e := &Engine{cfg: config.Config{GateNiche: true}}
	j := &model.Job{Stage: model.StageDiscoverNiche, Status: model.StatusRunning}
	e.advanceFrom(j, model.StageDiscoverNiche)
	if j.Stage != model.StageReviewNiche || j.Status != model.StatusAwaiting {
		t.Fatalf("gate on: got %s/%s, want review_niche/awaiting", j.Stage, j.Status)
	}

	// Gate off: discovery skips straight to product selection and keeps running.
	e = &Engine{cfg: config.Config{GateNiche: false}}
	j = &model.Job{Stage: model.StageDiscoverNiche, Status: model.StatusRunning}
	e.advanceFrom(j, model.StageDiscoverNiche)
	if j.Stage != model.StageSelectProduct || j.Status != model.StatusRunning {
		t.Fatalf("gate off: got %s/%s, want select_product/running", j.Stage, j.Status)
	}
}

func TestAdvanceFrom_Transitions(t *testing.T) {
	e := &Engine{cfg: config.Config{GateNiche: true}}

	// Artwork producer -> artwork review gate.
	j := &model.Job{Stage: model.StageArtwork}
	e.advanceFrom(j, model.StageArtwork)
	if j.Stage != model.StageReviewArtwork || j.Status != model.StatusAwaiting {
		t.Fatalf("artwork: got %s/%s", j.Stage, j.Status)
	}

	// Final marketplace stage -> complete/done.
	j = &model.Job{Stage: model.StageListMarket}
	e.advanceFrom(j, model.StageListMarket)
	if j.Stage != model.StageComplete || j.Status != model.StatusDone {
		t.Fatalf("final: got %s/%s, want complete/done", j.Stage, j.Status)
	}
}
