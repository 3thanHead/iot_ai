#!/usr/bin/env bash
# Set up a Jetson (Linux/systemd) as an Ollama LLM node for the cluster.
# Run on the Jetson at 192.168.1.11.  Usage:  bash setup.sh
#
# Idempotent: re-running (e.g. every `edge deploy`) only does work that's still
# needed — it won't re-download Ollama if it's already installed, and it skips the
# sudo systemd steps once the LAN bind is in place, so there's no password prompt
# after the first run.
set -euo pipefail

MODEL="${LLM_MODEL:-llama3.2:3b}"

if command -v ollama >/dev/null 2>&1; then
  echo "==> Ollama already installed ($(ollama --version 2>/dev/null | head -1)); skipping download."
else
  echo "==> Installing Ollama (native arm64 + Jetson GPU)…"
  curl -fsSL https://ollama.com/install.sh | sh
fi

# --- Jetson GPU libraries ----------------------------------------------------
# Ollama's installer only recognises L4T R35 (JetPack 5) and R36 (JetPack 6). On a
# newer JetPack — e.g. R39 / JetPack 7 on an Orin — it prints "Unsupported JetPack
# version" and installs NO CUDA bundle, so Ollama silently runs on CPU (slow).
# Orin's Ampere GPU runs the JetPack 6 (CUDA 12) build fine under JetPack 7's newer,
# backward-compatible driver, so when the installer skipped the bundle we overlay it
# ourselves — exactly what install.sh does for R36, just extended to R37+.
if [ -f /etc/nv_tegra_release ]; then
  INSTALL_DIR=$(dirname "$(dirname "$(command -v ollama)")")   # e.g. /usr/local or /usr
  if find "$INSTALL_DIR/lib/ollama" -iname '*cuda*' 2>/dev/null | grep -q .; then
    echo "==> Jetson CUDA libraries already present — GPU build in place."
  else
    ARCH=$(uname -m); [ "$ARCH" = "aarch64" ] && ARCH="arm64"
    # Ollama publishes only jetpack5 / jetpack6 bundles (there is no jetpack7), so
    # JetPack 7 uses the jetpack6 build. The bundles ship as .tar.zst only, so zstd
    # is required — install it if missing (same requirement as Ollama's installer).
    if grep -q R35 /etc/nv_tegra_release; then BUNDLE="jetpack5"; else BUNDLE="jetpack6"; fi
    echo "==> No CUDA libs from the installer (unrecognised JetPack) — overlaying ${BUNDLE} GPU build…"
    if ! command -v zstd >/dev/null 2>&1; then
      echo "    installing zstd (needed to unpack the bundle)…"
      sudo apt-get update -qq && sudo apt-get install -y zstd
    fi
    curl -fsSL "https://ollama.com/download/ollama-linux-${ARCH}-${BUNDLE}.tar.zst" \
      | sudo tar --use-compress-program=zstd -xf - -C "$INSTALL_DIR"
    sudo systemctl restart ollama
    echo "==> ${BUNDLE} CUDA libraries installed; ollama restarted on GPU."
  fi
fi

OVERRIDE=/etc/systemd/system/ollama.service.d/override.conf
WANT='[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"'
if [ -f "$OVERRIDE" ] && [ "$(cat "$OVERRIDE")" = "$WANT" ]; then
  echo "==> LAN bind already configured; skipping systemd setup (no sudo needed)."
else
  echo "==> Exposing Ollama on the LAN (bind 0.0.0.0) via a systemd override…"
  sudo mkdir -p /etc/systemd/system/ollama.service.d
  printf '%s\n' "$WANT" | sudo tee "$OVERRIDE" >/dev/null
  sudo systemctl daemon-reload
  sudo systemctl restart ollama
fi

echo "==> Pulling the shared model: ${MODEL}…"
ollama pull "${MODEL}"

echo "==> Self-check:"
curl -s http://localhost:11434/api/tags | head -c 300; echo

# On a Jetson, report GPU status without disrupting what the node is already serving.
# We deliberately DON'T force-load the cluster model: this is a memory-tight 8GB-class
# device that may already be serving another model (e.g. the vision gateway's
# moondream), and cold-loading a big model just to "check" would evict it and can
# outrun any timeout — the exact false negative this replaces. The CUDA libraries
# being installed is the reliable signal that Ollama will run on the GPU; `ollama ps`
# then shows live proof if a model happens to be resident.
if [ -f /etc/nv_tegra_release ]; then
  INSTALL_DIR=$(dirname "$(dirname "$(command -v ollama)")")
  if find "$INSTALL_DIR/lib/ollama" -iname '*cuda*' 2>/dev/null | grep -q .; then
    echo "==> GPU: CUDA libraries present — Ollama will run models on the GPU. ✔"
  else
    echo "==> WARNING: no CUDA libraries under ${INSTALL_DIR}/lib/ollama — Ollama will fall back to CPU."
  fi
  LOADED=$(timeout 10 ollama ps 2>/dev/null | awk 'NR==2')   # non-intrusive: only if already resident
  [ -n "$LOADED" ] && echo "    currently loaded: ${LOADED}"
fi
echo "Done. This node should now appear UP at http://192.168.1.10:8404"
