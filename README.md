# iot_ai

A small, end-to-end **edge AI + IoT** pipeline: a Wi-Fi camera sensor streams
video to a gateway that runs **real-time object detection** and **on-device
vision-language narration**, and serves an annotated live view to the browser.

The whole thing is built to run on cheap hardware and to move, unchanged, from a
dev laptop onto an edge box.

## Architecture

```
   ESP32 camera (sensor)          Gateway (any host)                  Browser
 ┌──────────────────────┐     ┌──────────────────────────┐        ┌───────────┐
 │ OV2640 + WROOM-32E    │     │  pull MJPEG  ─┐          │        │  live view │
 │ MJPEG over HTTP/Wi-Fi │ ──▶ │  YOLO detect  ├─ annotate├─ /stream ─▶ + boxes  │
 │ /stream  /snapshot    │     │  VLM narrate ─┘          │        │  + caption │
 └──────────────────────┘     │  (Ollama, periodic)      │        └───────────┘
                               └──────────────────────────┘
```

- **Sensor (IoT):** an ESP32 with an OV2640 camera. It does one job — capture
  frames and stream them as MJPEG over Wi-Fi. Tiny, cheap, low-power.
- **Gateway (AI):** a host-agnostic Python service. A single background worker
  pulls the camera stream, runs the AI **once per frame regardless of how many
  browsers are watching**, and publishes annotated frames + a text caption.
- **The AI is a two-stage cascade:**
  - **YOLO** — fast, real-time object *detection* (bounding boxes + labels).
  - **VLM (moondream via Ollama)** — open-ended *narration* ("what is this?"),
    run periodically in the background, not on every frame.
- **Display:** the browser only ever talks to the gateway, which relays the
  annotated stream. The gateway is where future AI (better models, tracking,
  alerts) slots in without touching the firmware or the frontend.

### Why this shape

The gateway is deliberately **host-agnostic** so the same code runs on:
- a **dev laptop** (e.g. an NVIDIA RTX GPU) — fast iteration, big models, and
- a **[Jetson Orin Nano](https://developer.nvidia.com/embedded/jetson-orin-nano-developer-kit)** —
  the always-on edge gateway, running YOLO via TensorRT and a small VLM locally.

## Hardware

| Part | Used as | Link |
|------|---------|------|
| **SunFounder ESP32 Camera Pro Kit** (WROOM-32E + OV2640) | camera sensor | <https://www.sunfounder.com/products/sunfounder-esp32-camera-pro-kit> |
| **Jetson Orin Nano Developer Kit** (8 GB) | edge gateway | <https://developer.nvidia.com/embedded/jetson-orin-nano-developer-kit> |
| Dev laptop w/ NVIDIA GPU | dev + flashing + heavy models | — |

> Note: the camera board's OV2640 pinout is **not** the common AI-Thinker
> ESP32-CAM layout — the firmware uses the SunFounder mapping.

## Layout

```
firmware/                 ESP32 camera firmware (PlatformIO)
  platformio.ini          board envs: `sunfounder` (WROOM-32E), `esp32-s3` (stub)
  src/main.cpp            OV2640 init + MJPEG HTTP server (/stream, /snapshot)
gateway/                  the AI gateway web service (Python)
  app.py                  pull stream -> YOLO -> VLM -> serve annotated view
  static/index.html       large-format live viewer (built for demos)
tools/fleetctl/           deploy CLI: build + flash firmware over USB, track versions
.env.example              copy to .env; Wi-Fi creds injected into the firmware build
```

## Quick start (laptop)

**1. Flash the camera firmware** (one-time, board on USB):
```bash
pip install -r tools/fleetctl/requirements.txt
cp .env.example .env                       # set WIFI_SSID / WIFI_PASS (2.4 GHz)
python tools/fleetctl/fleetctl.py flash --board sunfounder --version 1.0.0
pio device monitor -d firmware -b 115200   # read the camera's IP, then Ctrl+C
#   [cam] OV2640 initialized
#   [wifi] connected, ip=192.168.x.y
#   [net] stream:   http://192.168.x.y/stream
```

**2. Set up the AI deps** (one-time):
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r gateway/requirements.txt
# GPU torch (e.g. Blackwell / CUDA 12.8):
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
# the VLM:
ollama pull moondream
```

**3. Run the gateway and open the viewer:**
```bash
YOLO_MODEL=yolov8m-oiv7.pt CONF=0.3 DETECT_FPS=8 ESP32_HOST=192.168.x.y \
  python gateway/app.py
# open http://localhost:8000
```

## Models

- **Detection:** `yolov8m-oiv7.pt` — YOLOv8 trained on **Open Images V7 (601
  classes)**, far broader than COCO's 80 (which mislabels anything it doesn't
  know — a pencil becomes a "baseball bat"). Use `CONF=0.3` (these weights output
  lower confidences). Lighter options: `yolov8s-oiv7.pt`, `yolov8n-oiv7.pt`.
- **Narration:** `moondream` (~1.8 B) — small enough to run on the 8 GB Orin
  Nano. Swap for a stronger VLM on the laptop with `VLM_MODEL=llava`.
- **Predefined list:** restrict detection to specific classes with
  `CLASSES="Coffee cup,Pen,Mobile phone"`. For objects no public model knows,
  train a custom YOLO model instead.

## Configuration (env vars)

| Var | Default | What |
|-----|---------|------|
| `ESP32_HOST` | `192.168.1.164` | camera IP |
| `YOLO_MODEL` | `yolo11n.pt` | detection weights (`.pt` or a TensorRT `.engine`) |
| `CONF` | `0.5` | detection confidence floor (use `0.3` for `-oiv7`) |
| `CLASSES` | *(all)* | comma-separated class names to keep |
| `DETECT_FPS` | `8` | cap on detection rate — the main power/smoothness lever; `0` = uncapped |
| `MIRROR` | `1` | horizontal flip (selfie view); flipped before detection so labels read correctly |
| `DETECT` | `1` | set `0` to relay raw video with no AI |
| `OLLAMA_HOST` | `localhost:11434` | Ollama endpoint (point at another host to delegate the VLM) |
| `VLM_MODEL` | `moondream` | any Ollama vision model; `0`-interval to disable |
| `VLM_INTERVAL` | `8` | seconds between background VLM narrations; `0` disables |
| `GATEWAY_PORT` | `8000` | web server port |

## Optimizations

The pipeline is tuned to stay light and not stall:

- **Detect once, serve many.** One background worker runs the model; every
  browser shares the same annotated frames. Inference cost is independent of
  viewer count.
- **Decoupled detection rate (`DETECT_FPS`).** The worker drains *every* camera
  frame (so the camera's TCP send never backs up and freezes), but runs YOLO
  only N times/sec. This is the biggest power lever and what keeps the Orin Nano
  cool — `0` (uncapped) on the laptop for max smoothness, `~8` on the edge.
- **Auto-reconnecting MJPEG relay.** If the camera stream blips, the gateway
  transparently reopens it so the browser never freezes on a dead frame.
- **Wi-Fi modem sleep disabled** in firmware (`WiFi.setSleep(false)`) —
  eliminates the periodic stalls that otherwise kill an MJPEG stream.
- **Resolution vs. framerate.** Frame size/quality are build flags
  (`CAM_FRAMESIZE`, `CAM_JPEG_QUALITY`); lower resolution = more fps over Wi-Fi.
  The ESP32's Wi-Fi throughput is the real framerate ceiling, not the gateway.
- **Conservative flash baud** (`upload_speed`) for reliable flashing over a
  `usbipd` USB/IP tunnel (high rates drop mid-write).

## Run with Docker (laptop or Jetson)

The gateway ships as **one image / one compose file** for both platforms. The
only per-machine difference is the GPU base image (x86 CUDA vs Jetson/L4T —
there's no single base with working GPU torch for both), selected by the
`BASE_IMAGE` build arg in `.env`. Everything the image adds on top (flask +
requests + the app) is identical.

```bash
cp .env.example .env            # set ESP32_HOST; on Jetson set BASE_IMAGE (below)
docker compose up --build       # build + run; auto-restarts on boot
# open http://<host-ip>:8000
```

- **Laptop (x86 + NVIDIA):** leave `BASE_IMAGE` at its default.
- **Jetson (JetPack 6/7):** set `BASE_IMAGE=ultralytics/ultralytics:latest-jetson-jetpack6`
  in `.env`. (A JetPack 6 image runs on a JetPack 7 host via CUDA driver
  backward-compat; its torch is built for Orin's sm_87.) Requires the NVIDIA
  container runtime: `sudo nvidia-ctk runtime configure --runtime=docker`.
- **Blackwell laptops (sm_120):** the stock `ultralytics:latest` base may lag
  on the newest GPUs — point `BASE_IMAGE` at a CUDA-12.8+ base if torch falls
  back to CPU.

Downloaded weights persist in the `models` volume, so they're fetched once.

### TensorRT on the Jetson (optional speedup)

YOLO runs on the GPU from the `.pt` model out of the box. For a lower-power,
faster engine, build a device-specific TensorRT `.engine` **inside the running
container** (TensorRT ships in the Jetson base image), then point `YOLO_MODEL`
at it:

```bash
docker compose exec gateway yolo export model=yolov8m-oiv7.pt format=engine half=True
# then set YOLO_MODEL=yolov8m-oiv7.engine in .env and `docker compose up -d`
```

For the VLM, either run `moondream` via Ollama on the Jetson, or set
`OLLAMA_HOST=<laptop-ip>:11434` in `.env` to **delegate** narration to a bigger
machine over the LAN — delegate first if you hit memory pressure.

## Credentials

Wi-Fi credentials are injected into the firmware **at build time** from env vars
(`WIFI_SSID` / `WIFI_PASS`) — no secrets file in the tree. `fleetctl` loads them
from a gitignored `.env` (`cp .env.example .env`). For a raw `pio run`:
`set -a; source .env; set +a` first.

## Notes

- Linux hosts see the board as `/dev/ttyUSB*`; add yourself to `dialout` for
  serial access (`sudo usermod -aG dialout $USER`).
- Flashing from WSL needs `usbipd-win` to forward the USB serial device into WSL.
- If a flash won't connect, hold the board's `BOOT`/`IO0` button while it starts.
- Dark scenes make the small VLM hallucinate — **light the scene** before blaming
  the model.
