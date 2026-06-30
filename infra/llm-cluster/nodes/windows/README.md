# Windows PC LLM node (192.168.1.13)

**Native Ollama — no WSL, no Docker.** The native Windows app uses the NVIDIA GPU
directly, which is simpler and faster than going through WSL2 GPU passthrough.

In an **elevated** PowerShell (Run as Administrator):
```powershell
powershell -ExecutionPolicy Bypass -File setup.ps1
```

What it does:
1. Installs Ollama via `winget` (if missing).
2. Sets a persistent machine env var `OLLAMA_HOST=0.0.0.0:11434` so it listens on the LAN.
3. Opens the Windows Firewall for inbound TCP 11434.
4. Restarts Ollama and pulls `llama3.2:3b`.
5. Self-checks `/api/tags`.

Verify from another machine:
```bash
curl http://192.168.1.13:11434/api/tags
```

### Why not WSL/Docker here?
You asked whether to use WSL or PowerShell — the answer is **neither WSL nor Docker**:
the native Windows Ollama app talks to the NVIDIA driver directly. WSL2 would add a
GPU-passthrough layer and Docker Desktop another; both are extra moving parts for no
benefit on a dedicated node. Just the native app + the two PowerShell settings above.
