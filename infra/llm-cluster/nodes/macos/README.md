# MacBook Air M1 LLM node (192.168.1.12)

Native Ollama on macOS uses the Apple Silicon GPU (Metal) automatically.

```bash
bash setup.sh
```

What it does:
1. Installs Ollama (Homebrew cask, or prompts for the .app download).
2. Sets `OLLAMA_HOST=0.0.0.0:11434` (LAN-reachable) via `launchctl setenv` and starts the server.
3. Pulls `llama3.2:3b`.
4. Self-checks `/api/tags`.

Verify from another machine:
```bash
curl http://192.168.1.12:11434/api/tags
```

> If you launch Ollama from the **menubar app**, make sure `OLLAMA_HOST=0.0.0.0:11434`
> is in its environment so it stays reachable after a reboot (the `launchctl setenv`
> above sets a login-wide default).

> An 8GB M1 runs `llama3.2:3b` comfortably; leave headroom for whatever else the Mac runs.
