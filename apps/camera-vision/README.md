# camera-vision

A small, end-to-end **edge AI + IoT** pipeline: a Wi-Fi camera sensor streams
video to a gateway that runs **real-time object detection** and **on-device
vision-language narration**, and serves an annotated live view to the browser.

The whole thing is built to run on cheap hardware and to move, unchanged, from a
dev laptop onto an edge box. This is one app in the [iot_ai platform](../../README.md).

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

- **Sensor (IoT):** an ESP32 with an OV2640 camera. It captures frames and streams
  them as MJPEG over Wi-Fi. Tiny, cheap, low-power.
- **Gateway (AI):** a host-agnostic Python service. A single background worker
  pulls the camera stream, runs the AI **once per frame regardless of how many
  browsers are watching**, and publishes annotated frames + a text caption.
- **The AI is a two-stage cascade:**
  - **YOLO** — fast, real-time object *detection* (bounding boxes + labels).
  - **VLM (moondream via Ollama)** — open-ended *narration* ("what is this?"),
    run periodically in the background, not on every frame.

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
apps/camera-vision/
  firmware/               ESP32 camera firmware (PlatformIO)
    platformio.ini        board envs: `sunfounder` (WROOM-32E), `esp32-s3` (stub)
    src/main.cpp          OV2640 init + MJPEG HTTP server (/stream, /snapshot)
  gateway/                the AI gateway web service (Python)
    app.py                pull stream -> YOLO -> VLM -> serve annotated view
    static/index.html     live viewer
  Dockerfile              one image for laptop or Jetson (base via BASE_IMAGE arg)
  docker-compose.yml      run the gateway; reads this app's .env
  docs/jetson-deployment.md   full Jetson (JetPack 7) bring-up + decisions log
  .env.example            copy to .env: ESP32_HOST, BASE_IMAGE, model overrides
```
The firmware deploy CLI lives at the repo root in [`tools/iotctl/`](../../tools/iotctl/)
and is run **from the repo root** (it reads Wi-Fi creds from the root `.env`).

## Quick start (laptop)

**1. Flash the camera firmware** (one-time, board on USB) — from the **repo root**:
```bash
pip install -r tools/iotctl/requirements.txt
cp .env.example .env                       # root .env: set WIFI_SSID / WIFI_PASS (2.4 GHz)
python tools/iotctl/iotctl.py flash --board sunfounder --version 1.0.0
pio device monitor -d apps/camera-vision/firmware -b 115200   # read the camera's IP, Ctrl+C
#   [wifi] connected, ip=192.168.x.y
#   [net] stream:   http://192.168.x.y/stream
```

**2. Run the gateway** — easiest is Docker (see below), or natively:
```bash
cd apps/camera-vision
python3 -m venv .venv && source .venv/bin/activate
pip install -r gateway/requirements.txt
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128  # GPU torch
ollama pull moondream                                                              # the VLM
YOLO_MODEL=yolov8m-oiv7.pt CONF=0.3 ESP32_HOST=192.168.x.y python gateway/app.py
# open http://localhost:8000
```

## Run with Docker (laptop or Jetson)

**One image / one compose file** for both platforms. The only per-machine
difference is the GPU base image (x86 CUDA vs Jetson/L4T — there's no single base
with working GPU torch for both), selected by `BASE_IMAGE` in `.env`.

```bash
cd apps/camera-vision
cp .env.example .env            # set ESP32_HOST; on Jetson set BASE_IMAGE (below)
docker compose up --build       # build + run; auto-restarts on boot
# open http://<host-ip>:8000
```

- **Laptop (x86 + NVIDIA):** leave `BASE_IMAGE` at its default.
- **Jetson (JetPack 6/7):** set `BASE_IMAGE=ultralytics/ultralytics:latest-jetson-jetpack6`.
  (A JetPack 6 image runs on a JetPack 7 host via CUDA driver backward-compat; its
  torch is built for Orin's sm_87.) Requires the NVIDIA container runtime:
  `sudo nvidia-ctk runtime configure --runtime=docker`. See [docs/jetson-deployment.md](docs/jetson-deployment.md).
- **Blackwell laptops (sm_120):** the stock `ultralytics:latest` base may lag on
  the newest GPUs — point `BASE_IMAGE` at a CUDA-12.8+ base if torch falls back to CPU.
- **Docker Desktop / WSL:** the compose publishes the port (not host networking),
  so `localhost:8000` works; Ollama on the host is reached via `host.docker.internal`.

Downloaded weights persist in the `models` volume, so they're fetched once.

### TensorRT on the Jetson (optional speedup)
```bash
docker compose exec gateway yolo export model=yolov8m-oiv7.pt format=engine half=True
# then set YOLO_MODEL=yolov8m-oiv7.engine in .env and `docker compose up -d`
```

## Models

- **Detection:** `yolov8m-oiv7.pt` — YOLOv8 on **Open Images V7 (601 classes)**, far
  broader than COCO's 80. Use `CONF=0.3` (these weights output lower confidences).
  Lighter: `yolov8s-oiv7.pt`, `yolov8n-oiv7.pt`.
- **Narration:** `moondream` (~1.8 B) — small enough for the 8 GB Orin Nano. Swap
  for a stronger VLM on the laptop with `VLM_MODEL=llava`.
- **Predefined list:** restrict detection with `CLASSES="Coffee cup,Pen,Mobile phone"`.

## Configuration (env vars)

| Var | Default | What |
|-----|---------|------|
| `ESP32_HOST` | `192.168.1.50` | camera IP |
| `YOLO_MODEL` | `yolo11n.pt` | detection weights (`.pt` or a TensorRT `.engine`) |
| `CONF` | `0.5` | detection confidence floor (use `0.3` for `-oiv7`) |
| `CLASSES` | *(all)* | comma-separated class names to keep |
| `DETECT_FPS` | `8` | cap on detection rate — main power/smoothness lever; `0` = uncapped |
| `MIRROR` | `1` | horizontal flip (selfie view) |
| `DETECT` | `1` | set `0` to relay raw video with no AI |
| `OLLAMA_HOST` | `localhost:11434` | Ollama endpoint (compose default: `host.docker.internal:11434`) |
| `VLM_MODEL` | `moondream` | any Ollama vision model |
| `VLM_INTERVAL` | `8` | seconds between background VLM narrations; `0` disables |
| `GATEWAY_PORT` | `8000` | web server port |

## Optimizations

- **Detect once, serve many.** One worker runs the model; every browser shares the
  same annotated frames. Inference cost is independent of viewer count.
- **Decoupled detection rate (`DETECT_FPS`).** The worker drains *every* camera frame
  (so the camera's TCP send never backs up), but runs YOLO only N times/sec — the
  biggest power lever; keeps the Orin cool.
- **Auto-reconnecting MJPEG relay.** If the stream blips, the gateway reopens it so
  the browser never freezes on a dead frame.
- **Wi-Fi modem sleep disabled** in firmware (`WiFi.setSleep(false)`).
- **Resolution vs. framerate** are firmware build flags (`CAM_FRAMESIZE`, `CAM_JPEG_QUALITY`).

## Credentials

Wi-Fi credentials are injected into the firmware **at build time** from `WIFI_SSID` /
`WIFI_PASS` in the **root** `.env` (loaded by `iotctl`). For a raw `pio run`:
`set -a; source ../../.env; set +a` first.

## Notes

- Linux hosts see the board as `/dev/ttyUSB*`; add yourself to `dialout`
  (`sudo usermod -aG dialout $USER`).
- Flashing from WSL needs `usbipd-win` to forward the USB serial device into WSL.
- If a flash won't connect, hold the board's `BOOT`/`IO0` button while it starts.
- Dark scenes make the small VLM hallucinate — **light the scene** before blaming the model.
