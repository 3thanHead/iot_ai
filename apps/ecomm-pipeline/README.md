# ecomm-pipeline

An **automated print-on-demand pipeline** that runs continuously. A Go service
(LangChain via [langchaingo]) drives product ideas from "niche" all the way to a
live marketplace listing, **pausing at human-approval gates** you resolve from a
small web dashboard. It uses the home [LLM cluster](../../infra/llm-cluster/) for
all text and a **local Stable Diffusion** server for artwork.

```
discover niche → [gate] → select product → generate artwork → [gate] →
mockups + listing copy → [gate] → upload to POD → list on eBay → done
```

At each gate you can **Approve**, **Regenerate** (with a feedback note that
steers the re-run), or **Reject**. The engine keeps several ideas moving at once
(WIP-limited), persists everything to an embedded DB, and resumes in place after
a restart.

## Run

```bash
cp .env.example .env          # defaults point at the cluster; SD is optional
./edge up ecomm-pipeline --build
# or, from this dir:  docker compose up --build
```

Then open **http://<host>:8810**. With no `SD_BASE_URL` set it uses a built-in
placeholder image generator, so the whole flow works immediately (the only hard
dependency is the LLM cluster for the text stages).

## How it works

| Stage | Backend | Gate after? |
|-------|---------|-------------|
| Discover niche | LLM (cluster) | yes *(optional, `GATE_NICHE`)* |
| Select product + design concept | LLM (cluster) | — |
| Generate artwork | Stable Diffusion *(or stub)* | **yes** |
| Mockups + listing copy | POD provider + LLM | **yes** |
| Upload to POD provider | POD provider | — |
| List on marketplace (eBay) | Marketplace | — |

- **LLM** — langchaingo's Ollama provider, pointed at the cluster's single
  HAProxy endpoint, so generation is load-balanced and survives a node going down
  (same pattern as every other app here).
- **Artwork** — an `ImageGenerator` interface. The default client calls a local
  **Automatic1111-compatible** `txt2img` API (A1111 / ComfyUI); set `SD_BASE_URL`
  to enable it. Empty ⇒ deterministic placeholder PNGs so the pipeline still runs.
- **POD provider & marketplace** — `pod.Provider` and `marketplace.Channel`
  interfaces with working **stubs** today (fake product/listing ids, placeholder
  mockups). Real Printful/Printify and the eBay Sell API slot in behind these
  interfaces without touching the pipeline — see the doc comments in
  [`internal/pod`](internal/pod/pod.go) and
  [`internal/marketplace`](internal/marketplace/marketplace.go).

## Configuration (`.env`)

| Var | Default | Meaning |
|-----|---------|---------|
| `LLM_BASE_URL` | `http://host.docker.internal:11434` | cluster endpoint; `edge up` injects the master from `fleet.json` |
| `LLM_MODEL` | `llama3.2:3b` | model tag served by the cluster |
| `SD_BASE_URL` | *(empty)* | local Stable Diffusion (A1111) URL; empty ⇒ stub art |
| `SD_STEPS` | `28` | sampling steps for SD |
| `BRAND_NAME` / `BRAND_VOICE` / `BRAND_GUIDELINES` | see `.env.example` | brand identity injected into every prompt |
| `WIP_LIMIT` | `3` | max concurrent in-flight jobs |
| `TICK_INTERVAL` | `15s` | how often the engine advances work |
| `GATE_NICHE` | `true` | also pause for niche approval before product selection |
| `ECOMM_PORT` | `8810` | host port (container always listens on 8810) |

## Layout

```
cmd/ecomm-pipeline/   main: config → store → engine + web server, graceful shutdown
internal/
  config/             env-driven configuration
  model/              Job + stage/status state machine + payloads
  store/              bbolt-backed Job repository (pure Go, no CGO)
  llm/                langchaingo Ollama wrapper (text + JSON helpers)
  imagegen/           ImageGenerator: local Stable Diffusion client + stub
  pod/                PODProvider interface + stub (Printful/Printify go here)
  marketplace/        Marketplace interface + eBay stub
  pipeline/           the continuous engine + per-stage logic
  web/                approval dashboard (embedded html/template)
```

## Develop / test (Go installed locally)

```bash
go test ./...                 # transition logic + template rendering
go run ./cmd/ecomm-pipeline   # needs LLM_BASE_URL reachable; DATA_DIR defaults to /data
```

State (job DB + generated artwork) lives in `DATA_DIR` (`/data` in the container,
backed by the `ecomm-data` volume so it survives rebuilds).

[langchaingo]: https://github.com/tmc/langchaingo
