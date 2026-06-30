# Edge AI - Intelligence for IoT and Home Lab Automation

A general-purpose **AI + IoT platform** for the home network — a monorepo of
independent apps that share a common, fault-tolerant LLM backend. Each app can be
enabled or disabled on its own; the camera vision pipeline is just the first one.

## Layout

```
apps/
  camera-vision/     ESP32 camera -> YOLO detection + VLM narration -> live browser view
  chat/              streaming chatbot UI on top of the LLM cluster
  ecomm-pipeline/    Go + LangChain print-on-demand pipeline w/ human-approval gates
infra/
  llm-cluster/       distributed Ollama (Jetson / MacBook / Windows) behind a HAProxy
                     load balancer on the Mini PC -> one always-up, load-balanced endpoint
tools/
  iotctl/          ESP32 firmware deploy CLI: build + flash over USB, track versions
  labctl/          control CLI for the LLM cluster + apps (the brain behind `edge`)
.env / .env.example  root: ESP32 Wi-Fi creds only (firmware build). Each app has its own .env.
```

## The apps

| App | What it is | Status |
|-----|-----------|--------|
| [**camera-vision**](apps/camera-vision/) | Wi-Fi camera → real-time object detection + on-device vision-language narration, served as an annotated live view. Runs on a laptop or a Jetson edge box (Docker). | working |
| [**chat**](apps/chat/) | Streaming chatbot UI (FastAPI + WebSockets) on top of the LLM cluster. | working |
| [**ecomm-pipeline**](apps/ecomm-pipeline/) | Go + LangChain automated print-on-demand pipeline: discover niches → design → mockups/copy → POD upload → list on eBay, pausing at human-approval gates (web dashboard). | working |

## The shared infrastructure

[**infra/llm-cluster**](infra/llm-cluster/) — native **Ollama** on each AI machine
(each uses its own GPU/Metal), fronted by a **HAProxy** load balancer on the non-AI
**Mini PC**. The whole house gets **one** endpoint that is load-balanced and
auto-fails-over across nodes:

```
http://192.168.1.10:11434      # Ollama native API + OpenAI /v1 — used by any app
http://192.168.1.10:8404       # live health dashboard
```

| Node | IP | Accelerator |
|------|----|-------------|
| Jetson Orin Nano Super | 192.168.1.11 | CUDA |
| MacBook Air M1 | 192.168.1.12 | Metal |
| Windows PC | 192.168.1.13 | NVIDIA |
| Mini PC (master, load balancer) | 192.168.1.10 | — |

> IPs above are **examples**. Set your real master + node IPs with **`./edge fleet`**
> (stored in `infra/llm-cluster/fleet.json`, which is gitignored — never committed).

All nodes serve the same model (`llama3.2:3b`) so any node can answer any request.
See [infra/llm-cluster/README.md](infra/llm-cluster/) for setup and the failover test.

## CLI

One control tool, run from the repo root, that detects the OS (Linux / macOS /
Windows) and drives every app + the infrastructure. It's a stdlib-only Python
brain ([`tools/labctl/`](tools/labctl/)) that also fronts the ESP32 firmware tool
([`tools/iotctl/`](tools/iotctl/)) via `edge iot …`, behind native launchers — **`./edge`** on
Linux/macOS, **`.\edge.ps1`** on Windows. (Needs Python 3; on Windows:
`winget install Python.Python.3`.)

```bash
./edge doctor                 # check this machine: OS, docker, ollama, role
./edge fleet                  # set the master + node IPs interactively (-> fleet.json)
./edge list                   # discoverable apps/infra + the LLM nodes
./edge install-node           # set THIS machine up as an Ollama LLM node (OS-sensed)
./edge up camera-vision       # start an app   (docker compose up -d; add --build)
./edge down camera-vision     # stop it
./edge up all                 # start everything with a compose file
./edge status                 # running containers + machine role
./edge cluster                # live health of every LLM node + the load balancer
./edge model pull             # pull the cluster model on this node
./edge model set llama3.1:8b  # switch the WHOLE cluster: pull on every node + save it
./edge iot devices            # ESP32 firmware CLI: list boards / build / versions
./edge flash --board sunfounder --version 1.0.0   # shortcut for `edge iot flash …`
```

`install-node` is the "install on anything" path: run it on each AI machine and it
runs the right native Ollama setup (systemd / launchd / Windows service), binds the
LAN, and pulls the model — no per-OS steps to remember.

### Enable / disable an app
Each app is self-contained, so just start/stop it by name:
```bash
./edge up camera-vision       # enable the camera
./edge down camera-vision     # disable it
./edge up llm-cluster         # bring up the HAProxy load balancer (on the Mini PC)
```

## Conventions

- **Per-app config:** each app/infra dir has its own `.env` (gitignored) + `.env.example`.
  The **root** `.env` holds only the ESP32 Wi-Fi creds that `iotctl` injects into the
  firmware build.
- **One LLM endpoint:** apps point at the cluster (`http://192.168.1.10:11434`) rather
  than at any single machine, so inference is load-balanced and survives a node going down.
