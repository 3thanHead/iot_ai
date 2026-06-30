# LLM cluster — one always-up Llama endpoint for the whole house

Native **Ollama** runs on each AI machine (each using its own GPU/accelerator). A
**HAProxy** load balancer on the non-AI **Mini PC** fronts them, so every app on the
LAN talks to **one** endpoint that is load-balanced and auto-fails-over between nodes.

```
                          Mini PC master (192.168.1.10)
                        ┌──────────────────────────────┐
   any app on the LAN ─▶│  HAProxy  :11434  (+ :8404)   │
   (camera, chat,       │  balance first + checks       │
    ecomm, your laptop) └───────┬───────────┬──────────┘
                                │           │           │
                 ┌──────────────┘     ┌─────┘     └──────────────┐
                 ▼                     ▼                          ▼
        Jetson Orin Nano        MacBook Air M1            Windows PC
        192.168.1.11           192.168.1.12             192.168.1.13
        Ollama (CUDA)           Ollama (Metal)           Ollama (NVIDIA)
                         all serving  llama3.2:3b
```

> IPs shown throughout this doc are **examples**. Set your real master + node IPs
> with **`./edge fleet`** — it writes `fleet.json` (gitignored), the single source of
> truth. Node **order** in `fleet.json` is the routing priority (first = preferred).

### Why this shape (not k3s)
k3s is Linux-only — a MacBook (Metal) and a Windows PC can't be real k3s nodes, and a
k3s pod can't reach their GPUs. Running **native Ollama per machine** is the only way to
actually use each accelerator; a thin HAProxy in front gives the same "always-up,
load-balanced, one endpoint" outcome without Kubernetes. Because every node serves the
**same** model, HAProxy just routes in fleet (priority) order — the first healthy node
wins, overflowing to the next only when it's saturated or down.

## Setup

### One-shot deploy from your machine (recommended)
`edge deploy` pushes the whole cluster over SSH — no logging into each box. It reads
`fleet.json` (the **single source of truth** for hosts + SSH targets — create it with
`./edge fleet`), installs/refreshes Ollama on each node, **renders `haproxy.cfg` from
the fleet**, ships it to the Mini PC, and starts the load balancer.

```bash
./edge fleet                # set the master + node IPs interactively (-> fleet.json)
./edge deploy --dry-run     # preview every rsync/ssh + the generated haproxy.cfg
./edge deploy               # do it: nodes + master
./edge deploy nodes         # just the Ollama nodes
./edge deploy master        # just (re)ship haproxy.cfg + restart the LB
./edge deploy jetson        # just one node
./edge cluster              # watch them come UP
```
Requirements: key-based **SSH** from your machine to each host (`ssh`/`rsync`
installed), Python 3 on each box, and Docker on the Mini PC. The node install may
prompt once for `sudo` (it uses `ssh -t`). **Windows has no SSH by default**, so it's
skipped — run `\.edge.ps1 install-node` on it directly (its only job). Add or remove a
node by editing `fleet.json` and re-running `./edge deploy master` — the LB config
regenerates to match.

### Manual setup (per machine)

**1. Each AI node** (run the matching script on each machine):
| Node | IP | Setup |
|------|----|-------|
| Jetson Orin Nano Super | 192.168.1.11 | [`nodes/linux/`](nodes/linux/) — `bash setup.sh` |
| MacBook Air M1 | 192.168.1.12 | [`nodes/macos/`](nodes/macos/) — `bash setup.sh` |
| Windows PC | 192.168.1.13 | [`nodes/windows/`](nodes/windows/) — `setup.ps1` (elevated) |

Each installs Ollama, binds it to `0.0.0.0:11434` (LAN-reachable), and pulls `llama3.2:3b`.
Or, from the repo root on each machine, just run **`./edge install-node`** (`.\edge.ps1 install-node`
on Windows) — it detects the OS and runs the right one. See the [root README](../../README.md#cli).

**2. The master** (on the Mini PC, 192.168.1.10 — needs only Docker):
```bash
cd master
docker compose up -d
```

## Use it
One endpoint for everything:
```bash
# native Ollama API
curl http://192.168.1.10:11434/api/generate \
  -d '{"model":"llama3.2:3b","prompt":"hello","stream":false}'

# OpenAI-compatible API (what LangChain / apps/ecomm-pipeline use)
curl http://192.168.1.10:11434/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"llama3.2:3b","messages":[{"role":"user","content":"hello"}]}'
```

## Operate
- **Health dashboard:** http://192.168.1.10:8404 — shows each node UP/DOWN, live.
- **Add/remove a node:** run `./edge fleet` to update the IPs, then `./edge deploy master`
  — `haproxy.cfg` is regenerated from `fleet.json` (don't hand-edit it).
- **Failover test:** stop Ollama on one node → requests keep succeeding (served by the
  others); the dashboard shows that node DOWN, and it rejoins automatically when back.

## Models & model-aware routing
By default every node runs the same model (`llama3.2:3b`) so any node can serve any
request. But nodes can also serve **different** models, and HAProxy routes each request
to a node that actually has the requested one.

```bash
./edge model set llama3.1:8b              # pull on EVERY node + make it the cluster default
./edge model set qwen2.5:14b --node macbook   # give just one node an extra (bigger) model
```

How the routing works (all generated into `haproxy.cfg` from `fleet.json`):
- HAProxy buffers the request body and reads the JSON `"model"` field.
- It routes to a **per-model backend** containing the nodes that serve that model.
- Each backend uses `http-check expect rstring <model>` — a node only receives a
  model's traffic if its `/api/tags` actually lists the model, so routing self-corrects
  even if `fleet.json` is stale. Requests with no/unknown model fall to a liveness backend.

Ollama itself does **not** route across nodes (each instance is standalone) — this
model-awareness lives entirely in the HAProxy layer. `edge model set` re-renders and
ships the config automatically, so routing always matches what's pulled where.

> Note: body inspection assumes text-sized requests (`tune.bufsize 65536`). The image-
> heavy VLM path stays local to the camera app; it doesn't go through this router.
