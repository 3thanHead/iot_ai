// Package store persists Jobs in a single-file, pure-Go embedded database
// (bbolt) so pipeline state survives restarts with no external service.
package store

import (
	"encoding/json"
	"fmt"
	"sort"
	"sync"

	"go.etcd.io/bbolt"

	"github.com/3thanHead/iot_ai/ecomm-pipeline/internal/model"
)

var bucket = []byte("jobs")

// Store is the Job repository used by the engine and web server.
type Store interface {
	Save(j *model.Job) error
	Get(id string) (*model.Job, error)
	List() ([]*model.Job, error)
	ListByStatus(s model.Status) ([]*model.Job, error)
	CountActive() (int, error) // non-terminal jobs (the WIP count)
	Close() error
}

type boltStore struct {
	db *bbolt.DB
	mu sync.Mutex // serialise multi-step read-modify-write from the engine
}

// Open creates/opens the bbolt database at path.
func Open(path string) (Store, error) {
	db, err := bbolt.Open(path, 0o600, nil)
	if err != nil {
		return nil, fmt.Errorf("open bolt: %w", err)
	}
	err = db.Update(func(tx *bbolt.Tx) error {
		_, e := tx.CreateBucketIfNotExists(bucket)
		return e
	})
	if err != nil {
		db.Close()
		return nil, err
	}
	return &boltStore{db: db}, nil
}

func (s *boltStore) Save(j *model.Job) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	data, err := json.Marshal(j)
	if err != nil {
		return err
	}
	return s.db.Update(func(tx *bbolt.Tx) error {
		return tx.Bucket(bucket).Put([]byte(j.ID), data)
	})
}

func (s *boltStore) Get(id string) (*model.Job, error) {
	var j model.Job
	err := s.db.View(func(tx *bbolt.Tx) error {
		v := tx.Bucket(bucket).Get([]byte(id))
		if v == nil {
			return fmt.Errorf("job %q not found", id)
		}
		return json.Unmarshal(v, &j)
	})
	if err != nil {
		return nil, err
	}
	return &j, nil
}

func (s *boltStore) List() ([]*model.Job, error) {
	var jobs []*model.Job
	err := s.db.View(func(tx *bbolt.Tx) error {
		return tx.Bucket(bucket).ForEach(func(_, v []byte) error {
			var j model.Job
			if e := json.Unmarshal(v, &j); e != nil {
				return e
			}
			jobs = append(jobs, &j)
			return nil
		})
	})
	if err != nil {
		return nil, err
	}
	// Newest first — convenient for the dashboard.
	sort.Slice(jobs, func(i, k int) bool {
		return jobs[i].CreatedAt.After(jobs[k].CreatedAt)
	})
	return jobs, nil
}

func (s *boltStore) ListByStatus(status model.Status) ([]*model.Job, error) {
	all, err := s.List()
	if err != nil {
		return nil, err
	}
	var out []*model.Job
	for _, j := range all {
		if j.Status == status {
			out = append(out, j)
		}
	}
	return out, nil
}

func (s *boltStore) CountActive() (int, error) {
	all, err := s.List()
	if err != nil {
		return 0, err
	}
	n := 0
	for _, j := range all {
		if !j.Status.Terminal() {
			n++
		}
	}
	return n, nil
}

func (s *boltStore) Close() error { return s.db.Close() }
