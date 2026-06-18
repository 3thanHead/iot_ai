#!/usr/bin/env python3
"""iot_ai gateway web service.

A background worker pulls the ESP32's MJPEG stream, runs YOLO object detection
on each frame (once, regardless of viewer count), and publishes the latest
annotated frame. The web app's /stream then serves that to any number of
browsers. This is the YOLO stage of the YOLO -> LLaVA cascade; LLaVA slots in
by acting on the detections the worker already has.

    ESP32_HOST=192.168.1.164 python app.py     # then open http://localhost:8000

Env vars: ESP32_HOST, GATEWAY_PORT (8000), YOLO_MODEL (yolo11n.pt), DETECT (1).
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
# Drop low-confidence detections (the "finger -> hot dog" nonsense). YOLO only
# knows COCO's 80 classes, so a higher floor keeps just the confident ones.
CONF = float(os.environ.get("CONF", "0.5"))

# LLaVA (open-vocabulary "what is this?") via Ollama. Run `ollama pull llava`.
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "localhost:11434")
LLAVA_MODEL = os.environ.get("LLAVA_MODEL", "llava")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
STREAM_BOUNDARY = "frame"
TARGET_FPS = 20

app = Flask(__name__, static_folder=None)


class Pipeline:
    """Pulls camera frames, runs YOLO, holds the latest annotated JPEG."""

    def __init__(self):
        self.model = YOLO(YOLO_MODEL) if DETECT else None
        self._lock = threading.Lock()
        self._jpeg = None          # latest annotated JPEG (for /stream)
        self._raw = None           # latest clean JPEG (for LLaVA /describe)
        self._labels = []          # latest detections, for /detections + LLaVA hint

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    # --- background worker ---
    def _run(self):
        while True:
            try:
                resp = requests.get(f"http://{ESP32_HOST}/stream", stream=True, timeout=10)
                for jpeg in self._iter_frames(resp):
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
        if not DETECT:
            self._publish(raw=jpeg, annotated=jpeg, labels=[])
            return
        img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return
        res = self.model.predict(img, device=0, conf=CONF, verbose=False)[0]
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


@app.route("/describe", methods=["POST"])
def describe():
    """Open-vocabulary identification: send the current clean frame to LLaVA."""
    raw = pipeline.latest_raw()
    if raw is None:
        return jsonify(error="camera not ready"), 503

    seen = sorted(set(pipeline.detections()))
    hint = f" (YOLO detected: {', '.join(seen)}.)" if seen else ""
    prompt = "Describe what is in this image in 1-2 sentences." + hint

    try:
        r = requests.post(
            f"http://{OLLAMA_HOST}/api/generate",
            json={
                "model": LLAVA_MODEL,
                "prompt": prompt,
                "images": [base64.b64encode(raw).decode()],
                "stream": False,
            },
            timeout=120,  # first call loads the model into VRAM
        )
        r.raise_for_status()
        return jsonify(description=r.json().get("response", "").strip())
    except requests.RequestException as e:
        return jsonify(error=f"LLaVA request failed ({e}). Is Ollama running and "
                             f"'{LLAVA_MODEL}' pulled?"), 502


@app.route("/config")
def config():
    return jsonify(
        esp32_host=ESP32_HOST,
        detect=DETECT,
        model=YOLO_MODEL if DETECT else None,
        llava=LLAVA_MODEL,
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
