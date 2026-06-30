// Package web serves the approval dashboard: a board of jobs, per-job detail,
// and the Approve / Regenerate / Reject actions for jobs parked at a gate.
package web

import (
	"embed"
	"html/template"
	"log"
	"net/http"
	"path/filepath"
	"time"

	"github.com/3thanHead/iot_ai/ecomm-pipeline/internal/config"
	"github.com/3thanHead/iot_ai/ecomm-pipeline/internal/model"
	"github.com/3thanHead/iot_ai/ecomm-pipeline/internal/store"
)

//go:embed templates/*.html
var tmplFS embed.FS

// Actions is the subset of the engine the dashboard needs.
type Actions interface {
	Approve(id string) error
	Regenerate(id, comment string) error
	Reject(id, comment string) error
}

// Server wires HTTP handlers to the store (reads) and engine (gate actions).
type Server struct {
	cfg  config.Config
	st   store.Store
	act  Actions
	log  *log.Logger
	tmpl *template.Template
}

func NewServer(cfg config.Config, st store.Store, act Actions, lg *log.Logger) (*Server, error) {
	t, err := template.New("").Funcs(template.FuncMap{
		"shortTime": func(tm time.Time) string { return tm.Format("Jan 2 15:04") },
	}).ParseFS(tmplFS, "templates/*.html")
	if err != nil {
		return nil, err
	}
	return &Server{cfg: cfg, st: st, act: act, log: lg, tmpl: t}, nil
}

// Handler returns the configured router.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /", s.dashboard)
	mux.HandleFunc("GET /jobs/{id}", s.jobDetail)
	mux.HandleFunc("POST /jobs/{id}/approve", s.approve)
	mux.HandleFunc("POST /jobs/{id}/regenerate", s.regenerate)
	mux.HandleFunc("POST /jobs/{id}/reject", s.reject)
	mux.HandleFunc("GET /art/{file}", s.art)
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.Write([]byte("ok"))
	})
	return mux
}

type dashboardData struct {
	Brand    string
	Pending  []*model.Job // awaiting approval
	Active   []*model.Job // running / regenerate / error
	Finished []*model.Job // done / rejected
}

func (s *Server) dashboard(w http.ResponseWriter, _ *http.Request) {
	jobs, err := s.st.List()
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	data := dashboardData{Brand: s.cfg.BrandName}
	for _, j := range jobs {
		switch {
		case j.Status == model.StatusAwaiting:
			data.Pending = append(data.Pending, j)
		case j.Status.Terminal():
			data.Finished = append(data.Finished, j)
		default:
			data.Active = append(data.Active, j)
		}
	}
	s.render(w, "dashboard.html", data)
}

func (s *Server) jobDetail(w http.ResponseWriter, r *http.Request) {
	j, err := s.st.Get(r.PathValue("id"))
	if err != nil {
		http.Error(w, "job not found", http.StatusNotFound)
		return
	}
	s.render(w, "job.html", j)
}

func (s *Server) approve(w http.ResponseWriter, r *http.Request) {
	s.doAction(w, r, func(id string) error { return s.act.Approve(id) })
}

func (s *Server) regenerate(w http.ResponseWriter, r *http.Request) {
	comment := r.FormValue("feedback")
	s.doAction(w, r, func(id string) error { return s.act.Regenerate(id, comment) })
}

func (s *Server) reject(w http.ResponseWriter, r *http.Request) {
	comment := r.FormValue("feedback")
	s.doAction(w, r, func(id string) error { return s.act.Reject(id, comment) })
}

// doAction runs a gate action then redirects back to the dashboard.
func (s *Server) doAction(w http.ResponseWriter, r *http.Request, fn func(string) error) {
	id := r.PathValue("id")
	if err := fn(id); err != nil {
		s.log.Printf("action on %s failed: %v", id, err)
		http.Error(w, err.Error(), http.StatusConflict)
		return
	}
	http.Redirect(w, r, "/", http.StatusSeeOther)
}

// art serves a generated artwork PNG from the data dir (filename only, no paths).
func (s *Server) art(w http.ResponseWriter, r *http.Request) {
	name := filepath.Base(r.PathValue("file"))
	http.ServeFile(w, r, filepath.Join(s.cfg.DataDir, "art", name))
}

func (s *Server) render(w http.ResponseWriter, name string, data any) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	if err := s.tmpl.ExecuteTemplate(w, name, data); err != nil {
		s.log.Printf("render %s: %v", name, err)
		http.Error(w, err.Error(), http.StatusInternalServerError)
	}
}
