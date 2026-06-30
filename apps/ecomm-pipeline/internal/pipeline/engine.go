// Package pipeline contains the continuous engine that drives Jobs through the
// stages, plus the human-gate actions (approve / regenerate / reject) the web
// dashboard invokes.
package pipeline

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"log"
	"sync"
	"time"

	"github.com/3thanHead/iot_ai/ecomm-pipeline/internal/config"
	"github.com/3thanHead/iot_ai/ecomm-pipeline/internal/model"
	"github.com/3thanHead/iot_ai/ecomm-pipeline/internal/store"
)

// Engine runs the pipeline forever: it seeds new work, advances running jobs,
// and parks jobs at human gates until the dashboard resolves them.
type Engine struct {
	cfg config.Config
	st  store.Store
	pl  *Pipeline
	log *log.Logger

	// mu serialises all Job state transitions (engine commits + dashboard
	// actions) so they never clobber each other. Slow stage work runs OUTSIDE
	// this lock; commits re-check the job hasn't changed before saving.
	mu sync.Mutex
}

func NewEngine(cfg config.Config, st store.Store, pl *Pipeline, lg *log.Logger) *Engine {
	return &Engine{cfg: cfg, st: st, pl: pl, log: lg}
}

// Run blocks, ticking until ctx is cancelled.
func (e *Engine) Run(ctx context.Context) {
	e.log.Printf("engine started (tick=%s, wip=%d)", e.cfg.TickInterval, e.cfg.WIPLimit)
	t := time.NewTicker(e.cfg.TickInterval)
	defer t.Stop()
	e.tick(ctx) // run once immediately
	for {
		select {
		case <-ctx.Done():
			e.log.Printf("engine stopping")
			return
		case <-t.C:
			e.tick(ctx)
		}
	}
}

func (e *Engine) tick(ctx context.Context) {
	e.seed()

	jobs, err := e.st.List()
	if err != nil {
		e.log.Printf("tick: list jobs: %v", err)
		return
	}
	for _, j := range jobs {
		if ctx.Err() != nil {
			return
		}
		switch j.Status {
		case model.StatusRunning, model.StatusRegenerate:
			e.process(ctx, j)
		case model.StatusError:
			if time.Since(j.UpdatedAt) >= backoff(j.Attempts) {
				e.process(ctx, j)
			}
		}
	}
}

// seed introduces one fresh job per tick while we're under the WIP limit, so
// the pipeline keeps producing without bursting.
func (e *Engine) seed() {
	e.mu.Lock()
	defer e.mu.Unlock()
	n, err := e.st.CountActive()
	if err != nil {
		e.log.Printf("seed: count: %v", err)
		return
	}
	if n >= e.cfg.WIPLimit {
		return
	}
	now := time.Now()
	j := &model.Job{
		ID:        newID(),
		Stage:     model.StageDiscoverNiche,
		Status:    model.StatusRunning,
		CreatedAt: now,
		UpdatedAt: now,
	}
	if err := e.st.Save(j); err != nil {
		e.log.Printf("seed: save: %v", err)
		return
	}
	e.log.Printf("seeded job %s", j.ID)
}

// process runs one producing stage for a job. The slow work happens without the
// lock; the result is only committed if a dashboard action hasn't moved the job
// in the meantime (optimistic concurrency).
func (e *Engine) process(ctx context.Context, j *model.Job) {
	origStage, origStatus := j.Stage, j.Status
	e.log.Printf("job %s: running %s", j.ID, origStage)

	runErr := e.pl.run(ctx, j)

	e.mu.Lock()
	defer e.mu.Unlock()

	current, err := e.st.Get(j.ID)
	if err != nil {
		return // job vanished
	}
	if current.Stage != origStage || current.Status != origStatus {
		e.log.Printf("job %s: result discarded, state changed under us", j.ID)
		return
	}

	now := time.Now()
	j.UpdatedAt = now
	if runErr != nil {
		j.Status = model.StatusError
		j.Attempts++
		j.LastErr = runErr.Error()
		e.log.Printf("job %s: stage %s failed (attempt %d): %v", j.ID, origStage, j.Attempts, runErr)
		_ = e.st.Save(j)
		return
	}
	e.advanceFrom(j, origStage)
	if err := e.st.Save(j); err != nil {
		e.log.Printf("job %s: save: %v", j.ID, err)
		return
	}
	e.log.Printf("job %s: %s -> %s (%s)", j.ID, origStage, j.Stage, j.Status)
}

// advanceFrom moves a job to the stage following `from`, skipping the optional
// niche gate when disabled, and sets the resulting status.
func (e *Engine) advanceFrom(j *model.Job, from model.Stage) {
	next := stageAfter(from)
	if next == model.StageReviewNiche && !e.cfg.GateNiche {
		next = stageAfter(next)
	}
	j.Stage = next
	j.Attempts = 0
	j.LastErr = ""
	switch {
	case next == model.StageComplete:
		j.Status = model.StatusDone
	case next.IsGate():
		j.Status = model.StatusAwaiting
	default:
		j.Status = model.StatusRunning
	}
}

// ---- dashboard gate actions ----

// Approve advances a job parked at a gate to the next stage.
func (e *Engine) Approve(id string) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	j, err := e.gated(id)
	if err != nil {
		return err
	}
	gate := j.Stage
	e.advanceFrom(j, gate)
	j.UpdatedAt = time.Now()
	e.log.Printf("job %s: approved at %s -> %s", j.ID, gate, j.Stage)
	return e.st.Save(j)
}

// Regenerate sends a gated job back to the stage that produced the output, with
// a feedback note the stage will honour on the re-run.
func (e *Engine) Regenerate(id, comment string) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	j, err := e.gated(id)
	if err != nil {
		return err
	}
	gate := j.Stage
	producer, ok := producerOf(gate)
	if !ok {
		return fmt.Errorf("no producer for gate %q", gate)
	}
	j.Feedback = append(j.Feedback, model.FeedbackNote{
		Stage: gate, Action: "regenerate", Comment: comment, At: time.Now(),
	})
	j.Stage = producer
	j.Status = model.StatusRegenerate
	j.Attempts = 0
	j.LastErr = ""
	j.UpdatedAt = time.Now()
	e.log.Printf("job %s: regenerate from %s -> rerun %s", j.ID, gate, producer)
	return e.st.Save(j)
}

// Reject permanently kills a gated job.
func (e *Engine) Reject(id, comment string) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	j, err := e.gated(id)
	if err != nil {
		return err
	}
	if comment != "" {
		j.Feedback = append(j.Feedback, model.FeedbackNote{
			Stage: j.Stage, Action: "reject", Comment: comment, At: time.Now(),
		})
	}
	j.Status = model.StatusRejected
	j.UpdatedAt = time.Now()
	e.log.Printf("job %s: rejected at %s", j.ID, j.Stage)
	return e.st.Save(j)
}

// gated loads a job and verifies it is currently parked at a gate awaiting a human.
func (e *Engine) gated(id string) (*model.Job, error) {
	j, err := e.st.Get(id)
	if err != nil {
		return nil, err
	}
	if j.Status != model.StatusAwaiting || !j.Stage.IsGate() {
		return nil, fmt.Errorf("job %s is not awaiting approval", id)
	}
	return j, nil
}

// ---- helpers ----

func stageAfter(s model.Stage) model.Stage {
	for i, st := range model.Order {
		if st == s && i+1 < len(model.Order) {
			return model.Order[i+1]
		}
	}
	return model.StageComplete
}

func producerOf(gate model.Stage) (model.Stage, bool) {
	switch gate {
	case model.StageReviewNiche:
		return model.StageDiscoverNiche, true
	case model.StageReviewArtwork:
		return model.StageArtwork, true
	case model.StageReviewListing:
		return model.StageListing, true
	}
	return "", false
}

func backoff(attempts int) time.Duration {
	d := time.Duration(attempts) * 30 * time.Second
	if d > 5*time.Minute {
		return 5 * time.Minute
	}
	return d
}

func newID() string {
	var b [5]byte
	_, _ = rand.Read(b[:])
	return "job_" + hex.EncodeToString(b[:])
}
