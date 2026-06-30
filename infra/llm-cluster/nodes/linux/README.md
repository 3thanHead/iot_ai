# Linux LLM node (e.g. Jetson Orin Nano Super, 192.168.1.11)

Native Ollama on a systemd Linux box, exposed to the LAN, serving the shared model.
Works on any systemd Linux; on a Jetson it uses the CUDA GPU automatically.

```bash
bash setup.sh
# or, from the repo root on this machine:  ./edge install-node
```

What it does:
1. Installs Ollama (native build, GPU-enabled where available).
2. Adds a systemd override so Ollama binds `0.0.0.0:11434` (LAN-reachable, not just localhost).
3. Pulls `llama3.2:3b`.
4. Self-checks `/api/tags`.

Verify from another machine:
```bash
curl http://192.168.1.11:11434/api/tags
```

> ⚠️ If this is the **same** 8GB Jetson that runs the camera app, it can't comfortably
> hold YOLO + moondream + llama3.2:3b at once. Use it as an LLM node only when the
> camera app is stopped, or keep the camera and LLM roles on separate Jetsons.
