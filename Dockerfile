# iot_ai gateway — one image, multi-platform.
#
# The GPU base image differs per platform (there is no single base with working
# GPU PyTorch for both x86 and Jetson), but everything WE add — flask/requests
# plus the gateway app — is identical, so it's one Dockerfile selected by a build
# ARG. Pick the base per machine via BASE_IMAGE (see docker-compose.yml / .env):
#
#   x86_64 + NVIDIA GPU  : ultralytics/ultralytics:latest                 (default)
#   Jetson (JetPack 6/7) : ultralytics/ultralytics:latest-jetson-jetpack6
#
# The base already ships torch + ultralytics + opencv + (on Jetson) TensorRT.
ARG BASE_IMAGE=ultralytics/ultralytics:latest
FROM ${BASE_IMAGE}

# Gateway-only deps. Kept above the COPY so the heavy ML base layer stays cached
# and code edits rebuild only the final layer.
RUN pip install --no-cache-dir "flask>=3.0" "requests>=2.31"

# The app lives in /opt; CWD is /data so auto-downloaded weights (and any
# bind-mounted models) persist in the mounted volume rather than the image.
COPY gateway/ /opt/gateway/
WORKDIR /data

EXPOSE 8000
CMD ["python3", "/opt/gateway/app.py"]
