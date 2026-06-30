# Bringing iot_ai to the edge: Jetson Orin Nano deployment log

How the iot_ai gateway went from running on a dev laptop (WSL) to running
entirely on a Jetson Orin Nano as a self-contained, auto-restarting edge service
— and the decisions made along the way.

---

## The goal

Take the existing pipeline — **ESP32 camera → gateway (YOLO detection + VLM
narration) → browser** — and make the *gateway* run on a **Jetson Orin Nano** as
the always-on edge box, instead of the laptop. Same code, same behavior, just
moved onto cheap edge hardware. Bonus goal that emerged: package it so the
**same image/compose runs on both the laptop and the Nano**.

## The final architecture (what's running now)

```
ESP32 camera (192.168.1.50)        Jetson Orin Nano (192.168.1.20)            Browser
┌────────────────────────┐     ┌──────────────────────────────────────┐   ┌──────────┐
│ OV2640 + WROOM-32E      │     │  Docker container (iot_ai-gateway)     │   │ live view│
│ MJPEG over HTTP/Wi-Fi   │ ──▶ │   pull stream → YOLOv8m (601-class) ───├─▶ │  + boxes │
│ /stream                 │     │   → moondream VLM (periodic narrate)   │   │ + caption│
└────────────────────────┘     │   served on :8000                      │   └──────────┘
                                │  Ollama (host, GPU) ◀── localhost:11434│
                                └──────────────────────────────────────┘
```

- **Detection:** YOLOv8m trained on Open Images V7 (601 classes), GPU-accelerated.
- **Narration:** `moondream` (~1.8B VLM) served by Ollama on the Nano's GPU.
- **Packaging:** one Docker image, launched by `docker compose up`, auto-restarts
  on boot. Runs entirely on-device — no laptop in the loop.

---

## The hardware

| Thing | Detail |
|-------|--------|
| Board | **Jetson Orin Nano 8 GB** dev kit (sm_87 / compute 8.7), boots from NVMe (~98 GB free) |
| OS / stack | **L4T R39.2 / JetPack 7.2 / CUDA 13.2 / Ubuntu 24.04 / Python 3.12** (a freshly-flashed, very new release) |
| Reached at | `youruser@192.168.1.20` |
| Camera | SunFounder ESP32 Camera Pro (WROOM-32E + OV2640) at `192.168.1.50` |
| Dev/inference laptop | Windows + WSL, RTX 5070 Ti (Blackwell sm_120) |

---

## The journey & key decisions

### Decision 1 — Container vs. native install
**Chose: containers.** Running AI on Jetson the "NVIDIA-typical" way means
containers — they bundle a CUDA/cuDNN/TensorRT/PyTorch stack matched to the
board, avoiding manual version-matching. The native venv route was attempted
first and proved why: see below.

### Discovery — this is a bleeding-edge JetPack 7 board
Probing the Nano showed it was **not** the expected JetPack 6 system. It was a
**minimal L4T R39.2 / JetPack 7.2** rootfs (Ubuntu 24.04, Python 3.12, CUDA 13.2)
with *only* `nvidia-l4t-core` installed — no CUDA toolkit, cuDNN, TensorRT, or
OpenCV. Brand new, ~2 weeks old. This is the root cause of most of the friction
that followed.

### Dead end 1 — native pip PyTorch doesn't work on this board
A plain `pip install torch` pulled a generic aarch64/server build
(`2.12.1+cu130`). It reported `cuda.is_available() == True` but was built "except
{8.7}" — i.e. **no kernel for the Orin's sm_87**, so it would not actually run.
The jetson-ai-lab wheel index only publishes `jp6` and a generic `sbsa/cu130`
(the same broken one) — **no JetPack 7 wheel exists yet**. Native install ruled
out.

### Dead end 2 — `jetson-containers build ultralytics`
Tried dusty-nv's `jetson-containers`. `autotag` found **no prebuilt image** for
this new L4T, so it offered to build from source (multi-hour). The build then
failed with "couldn't find package: ultralytics" anyway. Abandoned.

### The fix — official Ultralytics JetPack image (JP6 on a JP7 host)
The clean path turned out to be **Ultralytics' own prebuilt Jetson image**. The
docs mention a `jetpack7` tag, but Docker Hub only actually has up to
`latest-jetson-jetpack6`. Key insight: **a JetPack 6 container runs fine on this
JetPack 7 host** thanks to NVIDIA driver backward-compatibility (the CUDA 13.2
driver runs the container's CUDA 12.x runtime), and its PyTorch is built for
Orin's sm_87. Verified:
```
cuda True Orin        ← GPU works inside the container, no sm_87 warning
```

### Decision 2 — one Dockerfile/compose for both platforms
Rather than separate setups, we use **one Dockerfile + one `docker-compose.yml`**.
The only per-machine difference is the GPU base image (there's no single base
with working GPU torch for both x86 and Jetson), selected via a `BASE_IMAGE`
build arg in `.env`:
- Laptop (x86 NVIDIA): `ultralytics/ultralytics:latest`
- Jetson: `ultralytics/ultralytics:latest-jetson-jetpack6`

Everything the image adds on top (flask + requests + the gateway app) is
identical. Weights auto-download into a persisted `models` volume.

### Decision 3 — the VLM: Ollama + moondream, on-device
Considered three options:
- **Ollama + moondream (chosen).** Simplest, portable, and the gateway already
  speaks Ollama's API — zero code change. moondream (~1.8B) runs on the Nano's
  GPU. Confirmed the board is 8 GB (`free -h` → 7.3 GiB), so YOLOv8m + moondream
  fit comfortably together — no need to delegate.
- **NanoVLM (dusty-nv).** Faster on Orin (TensorRT/MLC), but it's a *runtime*,
  not a model — switching replaces both Ollama and moondream and requires
  rewriting the gateway's `call_vlm()`. Deferred to a "phase 2" if we ever want
  real-time *per-frame* on-device VLM.
- **Delegate to laptop.** The fallback if the board were 4 GB. Not needed.

### Decision 4 — restore the viewer size
The browser viewer had been upscaled to a large "demo" format. Reverted it to
the original compact sizing (`#stream` 800px, smaller headings/captions).

---

## What was added to the repo

| File | Purpose |
|------|---------|
| `Dockerfile` | One image; `FROM ${BASE_IMAGE}` + flask/requests + the app |
| `docker-compose.yml` | Same file for laptop + Jetson; nvidia runtime, host networking, `restart: unless-stopped`, persisted `models` volume |
| `.dockerignore` | Keeps the build context tiny (only `gateway/` is copied) |
| `.env.example` | Documents `ESP32_HOST` / `BASE_IMAGE` / overrides; one `.env` feeds both iotctl and compose |
| `README.md` | New "Run with Docker" section; in-container TensorRT note |
| `gateway/static/index.html` | Viewer restored to original size |

---

## Running it (demo commands)

On the Nano (`youruser@192.168.1.20`):
```bash
cd ~/iot_ai
docker compose up --build -d     # build + run, detached (auto-restarts on boot)
docker compose logs -f           # watch
docker compose ps                # status
# open http://192.168.1.20:8000
```
The Nano's `.env` has `BASE_IMAGE=ultralytics/ultralytics:latest-jetson-jetpack6`
and `ESP32_HOST=192.168.1.50`. Ollama runs as a host service with `moondream`
pulled; the container reaches it at `localhost:11434` via host networking.

Live endpoints to show in the demo:
```bash
curl -s http://localhost:8000/config        # model + VLM config
curl -s http://localhost:8000/detections    # current YOLO labels
curl -s http://localhost:8000/description    # latest moondream caption
```

## Gotchas worth knowing (for the demo / next deploy)
- **First VLM caption is slow** (~30–60 s) — moondream cold-loads into memory on
  the first request, then it's warm.
- **A fresh copy to the Jetson needs the `-jetson-jetpack6` base** set in `.env`
  (the committed default is the x86 image, correct for the laptop).
- **Blackwell laptop (sm_120):** the stock x86 `ultralytics:latest` base may lag
  on the newest GPU — the laptop dev path stays the native cu128 venv; Docker is
  for the edge box.
- **TensorRT speedup is optional** — YOLO runs on the GPU from the `.pt` already;
  build a `.engine` inside the container later if desired.

## Possible next steps
- Build a TensorRT `.engine` on the Nano for lower-power, faster detection.
- Phase-2 VLM: swap Ollama/moondream for NanoVLM for real-time per-frame
  narration on-device.
- Production WSGI server instead of Flask's dev server.
