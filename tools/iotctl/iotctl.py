#!/usr/bin/env python3
"""iotctl -- one control CLI for the whole iot_ai platform.

Run from the repo root via the `iot` (bash) or `iot.ps1` (PowerShell) launcher,
or directly: `python3 tools/iotctl/iotctl.py <command>`. It detects the OS and
drives the apps (apps/*) and infrastructure (infra/*) consistently on Linux,
macOS, and Windows. Stdlib-only -- no pip install needed.

Commands:
    iot install-node            set up THIS machine as an Ollama LLM node (OS-sensed)
    iot deploy [target]         push the cluster from here over SSH (nodes + master)
    iot deploy <app>            ship the repo + rebuild an app (e.g. chat) on the master
    iot up   <name|all> [--build]   start an app/infra via its docker compose
    iot down <name|all>             stop it
    iot status [name|all]           show running containers (+ machine role)
    iot list                        list discoverable apps/infra + nodes
    iot cluster                     health of every LLM node + the load balancer
    iot model ls                    list the models available on each node
    iot model pull [name]           pull the cluster model on THIS node (ollama)
    iot model set  <name>           pull on every node + make it the cluster default
    iot model set  <name> --node X  give just node X another model (routing follows)
    iot model rm   <name>           remove a model from every node (or --node) + routing
    iot flash <args...>             passthrough to tools/fleetctl (firmware)
    iot doctor                      check prerequisites (docker/ollama/python)
"""
import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CLUSTER_DIR = REPO_ROOT / "infra" / "llm-cluster"
NODES_DIR = CLUSTER_DIR / "nodes"
HAPROXY_CFG = CLUSTER_DIR / "master" / "haproxy.cfg"
FLEET = CLUSTER_DIR / "fleet.json"
FLEET_EXAMPLE = CLUSTER_DIR / "fleet.example.json"
FLEETCTL = REPO_ROOT / "tools" / "fleetctl" / "fleetctl.py"
DEFAULT_MODEL = "llama3.2:3b"
MASTER_FALLBACK = "192.168.1.111:11434"  # used if no fleet.json

DRY_RUN = False  # set by `deploy --dry-run`

C = {"g": "\033[32m", "r": "\033[31m", "y": "\033[33m", "b": "\033[1m", "x": "\033[0m"}
if platform.system() == "Windows":
    C = {k: "" for k in C}


def say(msg):
    print(msg)


def run(cmd, cwd=None, check=True):
    """Run a subprocess, streaming output. Returns the exit code (or 0 if dry-run)."""
    print(f"{C['b']}$ {' '.join(str(c) for c in cmd)}{C['x']}")
    if DRY_RUN:
        return 0
    return subprocess.run(cmd, cwd=cwd, check=check).returncode


def have(tool):
    return shutil.which(tool) is not None


def http_json(url, timeout=2):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def wait_http(url, tries=20, delay=1):
    """Poll until `url` answers (e.g. HAProxy just restarted). True if it came up."""
    if DRY_RUN:
        return True
    for _ in range(tries):
        if http_json(url) is not None:
            return True
        time.sleep(delay)
    return False


def detect_os():
    """Return (system, label, is_jetson). system in {Linux, Darwin, Windows}."""
    system = platform.system()
    is_jetson = Path("/etc/nv_tegra_release").exists()
    label = {"Linux": "Jetson/Linux" if is_jetson else "Linux",
             "Darwin": "macOS", "Windows": "Windows"}.get(system, system)
    return system, label, is_jetson


def discover_services():
    """Map a friendly name -> compose dir, by scanning apps/ and infra/."""
    services = {}
    for comp in sorted(REPO_ROOT.glob("apps/*/docker-compose.yml")):
        services[comp.parent.name] = comp.parent
    for comp in sorted(REPO_ROOT.glob("infra/*/*/docker-compose.yml")):
        services[comp.parent.parent.name] = comp.parent
    return services


# ---------- fleet (single source of truth for deploy + node list) ----------

def load_fleet(required=False):
    if FLEET.exists():
        return json.loads(FLEET.read_text())
    if required:
        say(f"{C['r']}no fleet config.{C['x']} Copy "
            f"{FLEET_EXAMPLE.relative_to(REPO_ROOT)} -> {FLEET.relative_to(REPO_ROOT)} "
            f"and edit the hosts/ssh targets.")
        sys.exit(1)
    return None


def list_nodes():
    """(name, host, port) for each LLM node -- from fleet.json, else haproxy.cfg."""
    fleet = load_fleet()
    if fleet:
        return [(n["name"], n["host"], "11434") for n in fleet.get("nodes", [])]
    nodes = []
    if HAPROXY_CFG.exists():
        for line in HAPROXY_CFG.read_text().splitlines():
            p = line.split()
            if len(p) >= 3 and p[0] == "server":
                host, _, port = p[2].partition(":")
                nodes.append((p[1], host, port or "11434"))
    return nodes


def master_endpoint():
    fleet = load_fleet()
    if fleet and fleet.get("master", {}).get("host"):
        return f"{fleet['master']['host']}:11434"
    return MASTER_FALLBACK


def cluster_model():
    """The cluster's shared model — fleet.json is the source of truth."""
    fleet = load_fleet()
    if fleet and fleet.get("model"):
        return fleet["model"]
    return DEFAULT_MODEL


def _san(model):
    """Safe identifier for HAProxy acl/backend names: llama3.1:8b -> llama3_1_8b."""
    return re.sub(r"[^a-zA-Z0-9]", "_", model)


def _model_acl_rx(model):
    """ERE fragment matching a model name in the request body, treating an untagged
    name as equivalent to its `:latest` tag (Ollama does: `mistral` == `mistral:latest`).
    So fleet model `mistral` still routes a `"model":"mistral:latest"` request, and
    vice-versa. A tagged name like `llama3.2:3b` must match exactly."""
    base, _, tag = model.partition(":")
    return re.escape(base) + "(:latest)?" if tag in ("", "latest") else re.escape(model)


def node_models(n, default_model):
    """Models a node serves: its explicit `models` list, else the cluster default."""
    return n.get("models") or [default_model]


def by_power(nodes):
    """Nodes strongest-first, by their optional `power` (higher = more capable; 0 if
    unset). Name breaks ties so the rendered order is deterministic. Combined with
    HAProxy `balance first`, this routes every request to the most powerful node and
    only spills to weaker ones when it's at capacity (maxconn) or down."""
    return sorted(nodes, key=lambda n: (-n.get("power", 0), n["name"]))


def render_haproxy(nodes, default_model):
    """Model-aware HAProxy config: route each request to a node that has the model.

    HAProxy buffers the request body, reads the JSON `"model"` field, and routes to
    a per-model backend. Each backend only counts a node UP if /api/tags actually
    reports the model (`http-check expect rstring`), so routing self-corrects.
    """
    by_model = {}
    for n in nodes:
        for m in node_models(n, default_model):
            by_model.setdefault(m, []).append(n)
    models = sorted(by_model)

    L = [
        "# GENERATED by `iot deploy` / `iot model set` from fleet.json — do not hand-edit.",
        "# Model-aware routing: requests go to a node that actually serves the requested model.",
        "# Within each model, `balance first` prefers the most powerful node (fleet `power`),",
        "# overflowing to weaker nodes only when it's saturated or down.",
        "",
        "global",
        "    log stdout format raw local0",
        "    tune.bufsize 65536",        # headroom to inspect the JSON request body
        "",
        "defaults",
        "    mode http",
        "    log global",
        "    option httplog",
        "    timeout connect 5s",
        "    timeout client 300s",
        "    timeout server 300s",
        # Fault tolerance: if a node fails to answer OR returns a 5xx (e.g. Ollama up
        # but its runner is broken, so it passes the /api/tags check yet 500s on
        # generate), transparently re-dispatch the request to a *different* healthy
        # node instead of surfacing the error. The body is buffered (http-buffer-request
        # below) so even POST /api/chat is safely replayable. Retries happen before any
        # bytes reach the client, so a good node's stream is unaffected. 404 is included
        # because Ollama returns it for "model not found on this node" — redispatching
        # sends the request to a node that actually has the model.
        "    retries 3",
        "    option redispatch",
        "    retry-on all-retryable-errors 404 500 502 503 504",
        "",
        "frontend llm",
        "    bind *:11434",
        "    option http-buffer-request",   # buffer the body so ACLs can read "model"
    ]
    for m in models:
        rx = _model_acl_rx(m)
        L.append(f'    acl want_{_san(m)} req.body -m reg '
                 f'"\\"model\\"[[:space:]]*:[[:space:]]*\\"{rx}\\""')
    for m in models:
        L.append(f"    use_backend be_{_san(m)} if want_{_san(m)}")
    L.append("    default_backend all_nodes")
    L.append("")

    def server_line(n):
        # `balance first` honours declaration order; annotate power for readability.
        return f"    server {n['name']:<8} {n['host']}:11434    # power {n.get('power', 0)}"

    for m in models:
        L.append(f"backend be_{_san(m)}    # nodes serving {m}, strongest first")
        # `balance first`: prefer the most powerful node; overflow to the next only when
        # it hits maxconn (or fails health checks / redispatch).
        L.append("    balance first")
        L.append("    option httpchk GET /api/tags")
        L.append(f"    http-check expect rstring {re.escape(m)}")
        L.append("    default-server check inter 3s fall 3 rise 2 maxconn 64")
        for n in by_power(by_model[m]):
            L.append(server_line(n))
        L.append("")

    # Liveness backend for /api/tags, /v1/models, health, and unmatched requests.
    L.append("backend all_nodes")
    L.append("    balance first")
    L.append("    option httpchk GET /api/tags")
    L.append("    http-check expect status 200")
    L.append("    default-server check inter 3s fall 3 rise 2 maxconn 64")
    for n in by_power(nodes):
        L.append(server_line(n))
    L.append("")
    L += ["frontend stats", "    bind *:8404", "    stats enable", "    stats uri /",
          "    stats refresh 5s", "    stats show-node", ""]
    return "\n".join(L)


# ---------- ssh/rsync helpers ----------

RSYNC_EXCLUDES = [".git", ".venv", "__pycache__", "*.pt", "runs", ".env",
                  "fleet.json", "apps/camera-vision/firmware/.pio"]


def rsync_repo(ssh_target, remote_path):
    ex = []
    for e in RSYNC_EXCLUDES:
        ex += ["--exclude", e]
    return run(["rsync", "-az", "-e", "ssh -o ConnectTimeout=10",
                *ex, f"{REPO_ROOT}/", f"{ssh_target}:{remote_path}/"], check=False)


def ssh_run(ssh_target, remote_cmd, tty=True):
    # -t so interactive sudo prompts (systemd override, etc.) work during deploy.
    # ConnectTimeout so a down node fails fast instead of hanging the whole run.
    base = (["ssh", "-o", "ConnectTimeout=10"] + (["-t"] if tty else [])
            + [ssh_target, remote_cmd])
    return run(base, check=False)


def scp_text(ssh_target, content, remote_file):
    tmp = tempfile.NamedTemporaryFile("w", suffix=".cfg", delete=False)
    tmp.write(content)
    tmp.close()
    try:
        return run(["scp", tmp.name, f"{ssh_target}:{remote_file}"], check=False)
    finally:
        os.remove(tmp.name)


def need_docker():
    if not have("docker"):
        say(f"{C['r']}docker not found on this machine.{C['x']} "
            f"Install Docker first (or run `iot doctor`).")
        sys.exit(1)


def compose(service_dir, *args):
    return run(["docker", "compose", *args], cwd=service_dir, check=False)


# ---------- commands ----------

def cmd_install_node(args):
    system, label, _ = detect_os()
    say(f"{C['b']}Setting up this machine ({label}) as an Ollama LLM node…{C['x']}")
    model = args.model or DEFAULT_MODEL
    if system in ("Linux", "Darwin"):
        script = NODES_DIR / ("linux" if system == "Linux" else "macos") / "setup.sh"
        if not script.exists():
            say(f"{C['r']}missing {script}{C['x']}"); sys.exit(1)
        say(f"-> running {script.relative_to(REPO_ROOT)} (model: {model})")
        env = {**os.environ, "LLM_MODEL": model}
        sys.exit(subprocess.run(["bash", str(script)], env=env, check=False).returncode)
    elif system == "Windows":
        script = NODES_DIR / "windows" / "setup.ps1"
        say(f"-> running {script} (needs an elevated PowerShell)")
        sys.exit(subprocess.run(
            ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            check=False).returncode)
    else:
        say(f"{C['r']}Unsupported OS: {system}{C['x']}"); sys.exit(1)


def _deploy_node(n, model, rpath):
    say(f"\n{C['b']}== node: {n['name']} ({n['host']}, {n['os']}) =={C['x']}")
    # Windows has no SSH/rsync by default. When deploying from WSL *on* the Windows
    # host, manage its native Ollama through WSL->Windows interop instead of skipping.
    if n.get("ssh") == "interop":
        return _deploy_windows_interop(model)
    if not n.get("ssh"):
        say(f"{C['y']}skip{C['x']} (no ssh target in fleet.json). On that machine run:")
        say(r"   .\iot.ps1 install-node" if n["os"] == "windows" else "   ./iot install-node")
        return
    say("-> syncing repo…")
    rsync_repo(n["ssh"], rpath)
    say("-> installing/refreshing the Ollama node (may prompt for sudo)…")
    ssh_run(n["ssh"], f"cd {rpath} && ./iot install-node --model {model}")


def _deploy_windows_interop(model):
    if not have("powershell.exe"):
        say(f"{C['y']}windows node is 'interop' but powershell.exe isn't reachable.{C['x']}")
        say("   Run `iot deploy` from WSL on the Windows host, or do the one-time")
        say("   elevated setup: infra/llm-cluster/nodes/windows/setup.ps1")
        return
    say("-> ensuring LAN-bound Ollama + model via WSL→Windows interop…")
    # Start a 0.0.0.0 server only if one isn't already listening (avoids duplicates),
    # then pull the model. Firewall + machine env + boot persistence are the one-time
    # elevated job of nodes/windows/setup.ps1 (interop can't elevate, like sudo on Linux).
    ps = (
        '$ErrorActionPreference="SilentlyContinue";'
        '$ollama="$env:LOCALAPPDATA\\Programs\\Ollama\\ollama.exe";'
        '$env:OLLAMA_HOST="0.0.0.0:11434";'
        'if (-not (Get-NetTCPConnection -LocalAddress 0.0.0.0 -LocalPort 11434 -State Listen'
        ' -EA SilentlyContinue)) { Start-Process -FilePath $ollama -ArgumentList "serve"'
        ' -WindowStyle Hidden; Start-Sleep 4 };'
        f'& $ollama pull {model}'
    )
    run(["powershell.exe", "-NoProfile", "-Command", ps], check=False)
    say(f"{C['y']}one-time (elevated):{C['x']} nodes/windows/setup.ps1 sets the firewall + "
        "0.0.0.0 bind on boot.")


def _pull_on_node(n, model):
    """Pull `model` on one node. Uses the node's own /api/pull (no PATH/CLI issues)."""
    name = n["name"]
    if n.get("ssh") == "interop":
        if not have("powershell.exe"):
            say(f"  {C['y']}skip {name}{C['x']} (interop unavailable here)"); return False
        say(f"-> {name}: pulling {model} (interop)…")
        ps = ('$ollama="$env:LOCALAPPDATA\\Programs\\Ollama\\ollama.exe";'
              '$env:OLLAMA_HOST="0.0.0.0:11434";'
              f'& $ollama pull {model} 2>&1 | Out-Null; exit $LASTEXITCODE')
        return run(["powershell.exe", "-NoProfile", "-Command", ps], check=False) == 0
    if not n.get("ssh"):
        say(f"  {C['y']}skip {name}{C['x']} (no ssh target)"); return False
    say(f"-> {name}: pulling {model} (may take a few minutes for big models)…")
    payload = json.dumps({"name": model})
    # Pull via the node's own API (curl is everywhere, localhost always works). Discard
    # the streaming JSON progress body -> just one status line per node, not thousands.
    return ssh_run(n["ssh"],
                   f"curl -fsS -o /dev/null -w '   {name}: pulled (HTTP %{{http_code}})\\n' "
                   f"http://localhost:11434/api/pull -d '{payload}'", tty=False) == 0


def _delete_on_node(n, model):
    """Remove `model` from one node via its own /api/delete (200 ok, 404 if absent)."""
    name = n["name"]
    if n.get("ssh") == "interop":
        if not have("powershell.exe"):
            say(f"  {C['y']}skip {name}{C['x']} (interop unavailable here)"); return False
        say(f"-> {name}: removing {model} (interop)…")
        ps = ('$ollama="$env:LOCALAPPDATA\\Programs\\Ollama\\ollama.exe";'
              '$env:OLLAMA_HOST="0.0.0.0:11434";'
              f'& $ollama rm {model} 2>&1 | Out-Null; exit $LASTEXITCODE')
        return run(["powershell.exe", "-NoProfile", "-Command", ps], check=False) == 0
    if not n.get("ssh"):
        say(f"  {C['y']}skip {name}{C['x']} (no ssh target)"); return False
    say(f"-> {name}: removing {model}…")
    payload = json.dumps({"name": model})
    # -X DELETE via the node's own API; print the status so 404 (already absent) is visible.
    return ssh_run(n["ssh"],
                   f"curl -sS -o /dev/null -w '   {name}: HTTP %{{http_code}}\\n' "
                   f"-X DELETE http://localhost:11434/api/delete -d '{payload}'", tty=False) == 0


def _deploy_master(fleet, rpath):
    m = fleet.get("master", {})
    say(f"\n{C['b']}== master (HAProxy): {m.get('ssh', '?')} =={C['x']}")
    if not m.get("ssh"):
        say(f"{C['y']}no master.ssh in fleet.json — skipping.{C['x']}")
        return
    say("-> syncing repo…")
    rsync_repo(m["ssh"], rpath)
    say("-> rendering model-aware haproxy.cfg from fleet and shipping it…")
    cfg = render_haproxy(fleet.get("nodes", []), fleet.get("model", DEFAULT_MODEL))
    if DRY_RUN:
        say(cfg)
    scp_text(m["ssh"], cfg, f"{rpath}/infra/llm-cluster/master/haproxy.cfg")
    say("-> starting + reloading the load balancer…")
    # `up -d` creates the container if needed; `restart` forces HAProxy to re-read the
    # freshly shipped (bind-mounted) haproxy.cfg — `up -d` alone won't when it's already
    # running, so a config-only change would otherwise never take effect.
    ssh_run(m["ssh"], f"cd {rpath} && ./iot up llm-cluster && "
                      f"docker compose -f infra/llm-cluster/master/docker-compose.yml restart")
    # The restart bounces HAProxy for a second or two; wait for it to answer before any
    # health check runs, so a deploy doesn't falsely report the LB "unreachable".
    host = m.get("host") or m["ssh"].split("@")[-1]
    say("-> waiting for HAProxy to come back…")
    if not wait_http(f"http://{host}:11434/api/tags"):
        say(f"{C['y']}HAProxy still not answering — re-check with `iot cluster`.{C['x']}")


def _deploy_app(app, fleet, rpath):
    """Ship the repo to the master (Mini PC) and (re)build+start an app there.

    Apps (apps/*) run on the always-on, non-GPU master alongside HAProxy. Unlike
    `iot up` (which acts on the local machine), this pushes code to the master and
    runs `iot up <app> --build` remotely, so you redeploy from your laptop.
    """
    m = fleet.get("master", {})
    say(f"\n{C['b']}== app: {app} -> master ({m.get('ssh', '?')}) =={C['x']}")
    if not m.get("ssh"):
        say(f"{C['y']}no master.ssh in fleet.json — skipping.{C['x']}")
        return
    say("-> syncing repo…")
    rsync_repo(m["ssh"], rpath)
    say(f"-> building + (re)starting {app} on the master…")
    ssh_run(m["ssh"], f"cd {rpath} && ./iot up {app} --build")


def cmd_deploy(args):
    global DRY_RUN
    DRY_RUN = args.dry_run
    fleet = load_fleet(required=True)
    model = fleet.get("model", DEFAULT_MODEL)
    rpath = fleet.get("remote_path", "~/iot_ai")
    target = args.target
    if DRY_RUN:
        say(f"{C['y']}(dry-run: showing commands, executing nothing){C['x']}")

    known = {n["name"] for n in fleet.get("nodes", [])}
    # App names (apps/*); 'llm-cluster' is infra, deployed via the 'master' target.
    apps = {n for n in discover_services() if n != "llm-cluster"}

    # An app target ships the repo to the master and rebuilds the app there.
    if target in apps:
        _deploy_app(target, fleet, rpath)
        return

    if target in ("all", "nodes"):
        for n in fleet.get("nodes", []):
            _deploy_node(n, model, rpath)
    elif target in known:
        _deploy_node(next(n for n in fleet["nodes"] if n["name"] == target), model, rpath)

    if target in ("all", "master"):
        _deploy_master(fleet, rpath)

    if target not in ("all", "nodes", "master") and target not in known:
        say(f"{C['r']}unknown target: {target}{C['x']}  (use: all | nodes | master | "
            f"{' | '.join(sorted(known))} | {' | '.join(sorted(apps))})")
        sys.exit(1)

    if not DRY_RUN:
        say(f"\n{C['b']}== cluster health =={C['x']}")
        cmd_cluster(args)


def _resolve(names, services):
    if names == ["all"]:
        return list(services.items())
    out = []
    for n in names:
        if n not in services:
            say(f"{C['r']}unknown app/infra: {n}{C['x']}  (try `iot list`)")
            sys.exit(1)
        out.append((n, services[n]))
    return out


def cmd_up(args):
    need_docker()
    for name, d in _resolve(args.names, discover_services()):
        say(f"{C['b']}== up: {name} =={C['x']}")
        compose(d, "up", "-d", *(["--build"] if args.build else []))


def cmd_down(args):
    need_docker()
    for name, d in _resolve(args.names, discover_services()):
        say(f"{C['b']}== down: {name} =={C['x']}")
        compose(d, "down")


def cmd_status(args):
    _, label, _ = detect_os()
    say(f"{C['b']}machine:{C['x']} {label}    {C['b']}repo:{C['x']} {REPO_ROOT}")
    if have("docker"):
        for name, d in _resolve(args.names or ["all"], discover_services()):
            say(f"{C['b']}== {name} =={C['x']}")
            compose(d, "ps")
    else:
        say("docker not installed -> no app containers here (LLM-node-only machine?)")


def cmd_list(args):
    say(f"{C['b']}Apps & infrastructure (docker compose):{C['x']}")
    for name, d in discover_services().items():
        say(f"  {name:<16} {d.relative_to(REPO_ROOT)}")
    src = "fleet.json" if FLEET.exists() else "haproxy.cfg"
    say(f"\n{C['b']}LLM nodes (from {src}):{C['x']}")
    for name, host, port in list_nodes():
        say(f"  {name:<16} {host}:{port}")


def cmd_cluster(args):
    say(f"{C['b']}LLM cluster health{C['x']}  (model {cluster_model()})")
    nodes = list_nodes()
    if not nodes:
        say(f"{C['y']}no nodes configured (fleet.json / haproxy.cfg){C['x']}"); return
    up = 0
    for name, host, port in nodes:
        data = http_json(f"http://{host}:{port}/api/tags")
        if data is not None:
            up += 1
            models = ", ".join(m.get("name", "?") for m in data.get("models", [])) or "(none)"
            say(f"  {C['g']}● UP  {C['x']}{name:<10} {host}:{port}   models: {models}")
        else:
            say(f"  {C['r']}● DOWN{C['x']} {name:<10} {host}:{port}")
    master = master_endpoint()
    lb = http_json(f"http://{master}/api/tags")
    state = f"{C['g']}reachable{C['x']}" if lb is not None else f"{C['r']}unreachable{C['x']}"
    say(f"\n  load balancer {master}: {state}   stats: http://{master.split(':')[0]}:8404")
    say(f"  {up}/{len(nodes)} nodes up")


def cmd_model(args):
    if args.action in ("ls", "list"):
        # Query what each node actually has (its /api/tags) -> model -> [nodes] map.
        say(f"{C['b']}Models available on the cluster{C['x']}  (default: {cluster_model()})")
        catalog = {}
        for name, host, port in list_nodes():
            data = http_json(f"http://{host}:{port}/api/tags")
            if data is None:
                say(f"  {C['r']}● {name} unreachable{C['x']}"); continue
            for m in data.get("models", []):
                catalog.setdefault(m.get("name", "?"), set()).add(name)
        if not catalog:
            say("  (no models / no nodes reachable)"); return
        for model in sorted(catalog):
            say(f"  {model:<24} {', '.join(sorted(catalog[model]))}")
        return

    if args.action in ("rm", "remove"):
        if not args.name:
            say(f"{C['r']}usage: iot model rm <name> [--node <name>]{C['x']}"); sys.exit(1)
        model = args.name
        fleet = load_fleet(required=True)
        nodes = fleet.get("nodes", [])
        if args.node:
            targets = [n for n in nodes if n["name"] == args.node]
            if not targets:
                say(f"{C['r']}unknown node: {args.node}{C['x']}  "
                    f"({', '.join(n['name'] for n in nodes)})"); sys.exit(1)
            say(f"{C['b']}Removing {model} from node '{args.node}'{C['x']}")
        else:
            targets = nodes
            say(f"{C['b']}Removing {model} from every node{C['x']}")

        ok = 0
        for n in targets:
            if _delete_on_node(n, model):
                ok += 1
            ml = n.get("models")  # drop it from the node's explicit list, if present
            if ml and model in ml:
                ml.remove(model)
                if not ml:
                    del n["models"]  # empty list -> fall back to the cluster default
        if fleet.get("model") == model:
            say(f"{C['y']}note: {model} was the cluster default — set a new one with "
                f"`iot model set <name>`.{C['x']}")
        FLEET.write_text(json.dumps(fleet, indent=2) + "\n")
        say(f"\nremoved on {ok}/{len(targets)} node(s); fleet.json updated.")
        say(f"{C['b']}-> updating HAProxy routing on the master…{C['x']}")
        _deploy_master(fleet, fleet.get("remote_path", "~/iot_ai"))
        cmd_cluster(args)
        return

    if args.action == "pull":
        if not have("ollama"):
            say(f"{C['r']}ollama not found{C['x']} — run `iot install-node` first."); sys.exit(1)
        sys.exit(run(["ollama", "pull", args.name or cluster_model()], check=False))
    if args.action == "set":
        if not args.name:
            say(f"{C['r']}usage: iot model set <name> [--node <name>]{C['x']}")
            sys.exit(1)
        model = args.name
        fleet = load_fleet(required=True)
        nodes = fleet.get("nodes", [])
        default_model = fleet.get("model", DEFAULT_MODEL)

        if args.node:
            targets = [n for n in nodes if n["name"] == args.node]
            if not targets:
                say(f"{C['r']}unknown node: {args.node}{C['x']}  "
                    f"({', '.join(n['name'] for n in nodes)})"); sys.exit(1)
            say(f"{C['b']}Adding {model} to node '{args.node}'{C['x']}")
        else:
            targets = nodes
            say(f"{C['b']}Switching cluster default -> {model} (every node){C['x']}")

        ok = 0
        for n in targets:
            if _pull_on_node(n, model):
                ok += 1
                ml = n.setdefault("models", list(node_models(n, default_model)))
                if model not in ml:
                    ml.append(model)
        if not args.node:
            fleet["model"] = model  # new cluster default
        FLEET.write_text(json.dumps(fleet, indent=2) + "\n")
        say(f"\npulled on {ok}/{len(targets)} node(s); fleet.json updated.")

        # Routing depends on which node has which model -> re-render + ship the LB.
        say(f"{C['b']}-> updating HAProxy model-aware routing on the master…{C['x']}")
        _deploy_master(fleet, fleet.get("remote_path", "~/iot_ai"))
        cmd_cluster(args)


def cmd_flash(args):
    sys.exit(run([sys.executable, str(FLEETCTL), *args.args], check=False))


def cmd_doctor(args):
    _, label, _ = detect_os()
    say(f"{C['b']}iotctl doctor{C['x']}")
    say(f"  OS              : {label}  ({platform.platform()})")
    say(f"  Python          : {platform.python_version()}")

    def mark(ok):
        return f"{C['g']}yes{C['x']}" if ok else f"{C['r']}no{C['x']}"

    say(f"  docker          : {mark(have('docker'))}")
    if have("docker"):
        rc = subprocess.run(["docker", "compose", "version"], capture_output=True).returncode
        say(f"  docker compose  : {mark(rc == 0)}")
    say(f"  ollama          : {mark(have('ollama'))}")
    local = http_json("http://localhost:11434/api/tags")
    say(f"  ollama serving  : {mark(local is not None)} (localhost:11434)")
    say(f"  ssh / rsync     : {mark(have('ssh') and have('rsync'))} (needed for `iot deploy`)")
    say(f"  fleet.json      : {mark(FLEET.exists())}")
    say(f"  fleetctl        : {mark(FLEETCTL.exists())}")
    say(f"\n  {C['b']}services here:{C['x']} " + (", ".join(discover_services()) or "(none)"))
    role = (["LLM node"] if local is not None else []) + (["can run apps"] if have("docker") else [])
    say(f"  {C['b']}role        :{C['x']} " + (", ".join(role) or "unconfigured"))


def build_parser():
    p = argparse.ArgumentParser(prog="iot", description="Control CLI for the iot_ai platform.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("install-node", help="set up this machine as an Ollama LLM node")
    s.add_argument("--model", help=f"model to pull (default {DEFAULT_MODEL})")
    s.set_defaults(func=cmd_install_node)

    s = sub.add_parser("deploy", help="push the cluster from here over SSH (uses fleet.json)")
    s.add_argument("target", nargs="?", default="all",
                   help="all (default) | nodes | master | <node-name> | <app-name>")
    s.add_argument("--dry-run", action="store_true", help="print what would run, do nothing")
    s.set_defaults(func=cmd_deploy)

    s = sub.add_parser("up", help="start an app/infra (docker compose up -d)")
    s.add_argument("names", nargs="+", help="service name(s) or 'all' (see `iot list`)")
    s.add_argument("--build", action="store_true", help="rebuild images first")
    s.set_defaults(func=cmd_up)

    s = sub.add_parser("down", help="stop an app/infra (docker compose down)")
    s.add_argument("names", nargs="+", help="service name(s) or 'all'")
    s.set_defaults(func=cmd_down)

    s = sub.add_parser("status", help="show running containers + machine role")
    s.add_argument("names", nargs="*", help="service name(s) or 'all' (default all)")
    s.set_defaults(func=cmd_status)

    sub.add_parser("list", help="list discoverable apps/infra + nodes").set_defaults(func=cmd_list)
    sub.add_parser("cluster", help="health of every LLM node + the load balancer").set_defaults(func=cmd_cluster)

    s = sub.add_parser("model", help="list/pull/set/remove models on the cluster / a node")
    s.add_argument("action", choices=["ls", "list", "pull", "set", "rm", "remove"],
                   help="ls = what each node has; pull = this node; set = pull on every node "
                        "(or --node) + update routing; rm = remove from every node (or --node)")
    s.add_argument("name", nargs="?", help="model name (any model Ollama can pull)")
    s.add_argument("--node", help="with set/rm: target just this node")
    s.set_defaults(func=cmd_model)

    s = sub.add_parser("flash", help="passthrough to fleetctl (firmware)")
    s.add_argument("args", nargs=argparse.REMAINDER)
    s.set_defaults(func=cmd_flash)

    sub.add_parser("doctor", help="check prerequisites on this machine").set_defaults(func=cmd_doctor)
    return p


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
