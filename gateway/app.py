#!/usr/bin/env python3
"""iot_ai gateway web service.

A background worker pulls the ESP32's MJPEG stream, runs YOLO object detection
on each frame (once, regardless of viewer count), and publishes the latest
annotated frame. The web app's /stream then serves that to any number of
browsers. A second worker periodically narrates the scene with a vision-language
model (moondream by default) via Ollama -- the YOLO -> VLM cascade.

    ESP32_HOST=192.168.1.164 python app.py     # then open http://localhost:8000

Env vars: ESP32_HOST, GATEWAY_PORT (8000), YOLO_MODEL, DETECT (1), CONF, CLASSES,
          OLLAMA_HOST, VLM_MODEL (moondream), VLM_INTERVAL (8).
"""
import base64
import os
import re
import signal
import threading
import time

import cv2
import numpy as np
import requests
from flask import Flask, Response, jsonify, send_from_directory
from ultralytics import YOLO

ESP32_HOST = os.environ.get("ESP32_HOST", "192.168.1.164")
PORT = int(os.environ.get("GATEWAY_PORT", "8000"))
YOLO_MODEL = os.environ.get("YOLO_MODEL", "yolo11n.pt")
DETECT = os.environ.get("DETECT", "1") != "0"
# Drop low-confidence detections (the "finger -> hot dog" nonsense).
CONF = float(os.environ.get("CONF", "0.5"))
# Restrict detection to a predefined list of class names (comma-separated). Empty
# = detect everything the model knows. Pair with YOLO_MODEL=yolov8n-oiv7.pt for
# Open Images' 601 classes:  CLASSES="Coffee cup,Scissors,Hammer"
CLASSES = [c.strip() for c in os.environ.get("CLASSES", "").split(",") if c.strip()]
# Mirror horizontally (selfie view) so moving the camera left/right feels natural.
MIRROR = os.environ.get("MIRROR", "1") != "0"

# Vision-language model (open-vocabulary "what is this?") via Ollama. Default is
# moondream (~1.8B) -- small enough to run on the 8GB Orin Nano. Swap with any
# Ollama VLM (llava, llama3.2-vision, ...). Run `ollama pull moondream` first.
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "localhost:11434")
VLM_MODEL = os.environ.get("VLM_MODEL", "moondream")
# Periodically narrate the scene with the VLM, but only when Ollama is reachable.
# Seconds between runs; 0 disables.
VLM_INTERVAL = float(os.environ.get("VLM_INTERVAL", "8"))
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
STREAM_BOUNDARY = "frame"
TARGET_FPS = 20
# Cap detection rate -- the big power/RAM lever. The worker still drains the
# camera stream every frame (no backpressure/freeze), but only runs YOLO this
# often. Lower = less GPU/power; 8 fps still looks smooth. 0 = uncapped.
DETECT_FPS = float(os.environ.get("DETECT_FPS", "8"))

app = Flask(__name__, static_folder=None)


class Pipeline:
    """Pulls camera frames, runs YOLO, holds the latest annotated JPEG."""

    def __init__(self):
        self.model = YOLO(YOLO_MODEL) if DETECT else None
        self.class_ids = self._resolve_classes()
        self._lock = threading.Lock()
        self._jpeg = None          # latest annotated JPEG (for /stream)
        self._raw = None           # latest clean JPEG (for the VLM /describe)
        self._labels = []          # latest detections, for /detections + VLM hint
        self._description = ""      # latest VLM narration (auto, when available)
        self._vlm_ok = False        # is Ollama currently reachable?

    def _resolve_classes(self):
        """Map the predefined CLASSES names to model class ids (None = all)."""
        if not (self.model and CLASSES):
            return None
        by_name = {n.lower(): i for i, n in self.model.names.items()}
        ids, missing = [], []
        for c in CLASSES:
            (ids if c.lower() in by_name else missing).append(
                by_name.get(c.lower(), c))
        if missing:
            print(f"[gateway] WARNING: not in {YOLO_MODEL}: {missing}")
        return ids or None

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()
        if VLM_INTERVAL > 0:
            threading.Thread(target=self._describe_loop, daemon=True).start()

    # --- background worker ---
    def _run(self):
        min_interval = 1.0 / DETECT_FPS if DETECT_FPS > 0 else 0.0
        last = 0.0
        while True:
            try:
                resp = requests.get(f"http://{ESP32_HOST}/stream", stream=True, timeout=10)
                for jpeg in self._iter_frames(resp):
                    now = time.monotonic()
                    if now - last < min_interval:
                        continue  # drain the stream (avoid backpressure), skip detection
                    last = now
                    self._process(jpeg)
            except requests.RequestException:
                time.sleep(0.5)  # camera offline / blip -- retry

    @staticmethod
    def _iter_frames(resp):
        """Yield JPEG payloads from the ESP32's multipart stream (Content-Length framed)."""
        buf = b""
        hdr = re.compile(rb"Content-Length:\s*(\d+)\r\n\r\n")
        for chunk in resp.iter_content(chunk_size=8192):
            buf += chunk
            while True:
                m = hdr.search(buf)
                if not m:
                    break
                length = int(m.group(1))
                start = m.end()
                if len(buf) < start + length:
                    break
                yield buf[start:start + length]
                buf = buf[start + length:]

    def _process(self, jpeg):
        if not DETECT and not MIRROR:
            self._publish(raw=jpeg, annotated=jpeg, labels=[])
            return
        img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return
        if MIRROR:
            img = cv2.flip(img, 1)  # flip BEFORE detection so box labels read correctly
        if not DETECT:
            ok, out = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if ok:
                self._publish(raw=jpeg, annotated=out.tobytes(), labels=[])
            return
        res = self.model.predict(img, device=0, conf=CONF, classes=self.class_ids, verbose=False)[0]
        labels = [self.model.names[int(b.cls)] for b in res.boxes]
        annotated = res.plot()  # BGR ndarray with boxes drawn
        ok, out = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ok:
            self._publish(raw=jpeg, annotated=out.tobytes(), labels=labels)

    def _publish(self, raw, annotated, labels):
        with self._lock:
            self._raw = raw
            self._jpeg = annotated
            self._labels = labels

    # --- VLM narration (auto, only when Ollama is reachable) ---
    def call_vlm(self, raw):
        """Ask the VLM to describe a frame. Returns text, or None if unavailable."""
        seen = sorted(set(self.detections()))
        hint = f" (YOLO detected: {', '.join(seen)}.)" if seen else ""
        try:
            r = requests.post(
                f"http://{OLLAMA_HOST}/api/generate",
                json={
                    "model": VLM_MODEL,
                    "prompt": "Describe what is in this image in 1-2 sentences." + hint,
                    "images": [base64.b64encode(raw).decode()],
                    "stream": False,
                },
                timeout=120,  # first call loads the model into VRAM
            )
            r.raise_for_status()
            return r.json().get("response", "").strip()
        except requests.RequestException:
            return None

    def _describe_loop(self):
        """Every VLM_INTERVAL seconds, narrate the latest frame if Ollama is up."""
        while True:
            time.sleep(VLM_INTERVAL)
            raw = self.latest_raw()
            if raw is None:
                continue
            desc = self.call_vlm(raw)
            with self._lock:
                self._vlm_ok = desc is not None
                if desc:
                    self._description = desc

    # --- readers ---
    def latest(self):
        with self._lock:
            return self._jpeg

    def latest_raw(self):
        with self._lock:
            return self._raw

    def detections(self):
        with self._lock:
            return list(self._labels)

    def description(self):
        with self._lock:
            return self._description, self._vlm_ok


pipeline = Pipeline()


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/stream")
def stream():
    def gen():
        period = 1.0 / TARGET_FPS
        while True:
            frame = pipeline.latest()
            if frame is not None:
                yield (b"--" + STREAM_BOUNDARY.encode() + b"\r\n"
                       b"Content-Type: image/jpeg\r\n"
                       b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
                       + frame + b"\r\n")
            time.sleep(period)

    return Response(gen(), content_type=f"multipart/x-mixed-replace;boundary={STREAM_BOUNDARY}")


@app.route("/snapshot")
def snapshot():
    frame = pipeline.latest()
    if frame is None:
        return ("camera not ready", 503)
    return Response(frame, content_type="image/jpeg")


@app.route("/detections")
def detections():
    return jsonify(objects=pipeline.detections())


@app.route("/description")
def description():
    """Latest auto VLM narration (refreshed every VLM_INTERVAL seconds)."""
    text, ok = pipeline.description()
    return jsonify(description=text, available=ok)


@app.route("/describe", methods=["POST"])
def describe():
    """On-demand narration (same engine as the periodic loop)."""
    raw = pipeline.latest_raw()
    if raw is None:
        return jsonify(error="camera not ready"), 503
    desc = pipeline.call_vlm(raw)
    if desc is None:
        return jsonify(error=f"VLM unavailable. Is Ollama running and "
                             f"'{VLM_MODEL}' pulled?"), 502
    with pipeline._lock:
        pipeline._description, pipeline._vlm_ok = desc, True
    return jsonify(description=desc)


@app.route("/config")
def config():
    return jsonify(
        esp32_host=ESP32_HOST,
        detect=DETECT,
        model=YOLO_MODEL if DETECT else None,
        classes=CLASSES or "all",
        vlm=VLM_MODEL,
        vlm_interval=VLM_INTERVAL,
    )


def _force_exit(*_):
    # The YOLO/CUDA/OpenCV native threads don't unwind cleanly on Ctrl-C and
    # wedge the process. For a dev server, exit hard and immediately.
    os._exit(0)


if __name__ == "__main__":
    print(f"[gateway] ESP32_HOST={ESP32_HOST} detect={DETECT} model={YOLO_MODEL}")
    print(f"[gateway] serving http://0.0.0.0:{PORT}  (Ctrl-C to quit)")
    signal.signal(signal.SIGINT, _force_exit)
    signal.signal(signal.SIGTERM, _force_exit)
    pipeline.start()
    app.run(host="0.0.0.0", port=PORT, threaded=True)
