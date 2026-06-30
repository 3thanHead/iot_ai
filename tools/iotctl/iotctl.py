#!/usr/bin/env python3
"""iotctl -- deploy firmware to the iot_ai ESP32 fleet from the gateway.

Phase 1 (wired): build + flash a board over USB, stamping the firmware with a
version the gateway controls. Wraps PlatformIO (which wraps esptool) so the
build toolchain stays the canonical Espressif one.

    python iotctl.py devices                       # list attached boards
    python iotctl.py flash --board sunfounder      # build + flash over USB
    python iotctl.py flash --board sunfounder --version 0.2.0 --port /dev/ttyUSB0
    python iotctl.py versions                       # what we've deployed

Phase 2 will add an `ota serve` subcommand here (host firmware + manifest so
devices self-update over WiFi). Same CLI, same registry.
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# Repo layout: tools/iotctl/iotctl.py -> repo root is two parents up.
REPO_ROOT = Path(__file__).resolve().parents[2]
FIRMWARE_DIR = REPO_ROOT / "apps" / "camera-vision" / "firmware"
REGISTRY = REPO_ROOT / "tools" / "iotctl" / "deployments.json"
ENV_FILE = REPO_ROOT / ".env"


def load_env():
    """Load KEY=VALUE lines from the repo-root .env into the environment.

    Build-time secrets (WIFI_SSID, WIFI_PASS) live here, not in a tracked
    header. Real environment variables take precedence over the file.
    """
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def list_ports():
    """Return attached serial ports, or [] if pyserial isn't installed."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return None
    return list(list_ports.comports())


def cmd_devices(_args):
    ports = list_ports()
    if ports is None:
        print("pyserial not installed -- run: pip install -r requirements.txt")
        print("(falling back) check manually:  ls /dev/ttyUSB* /dev/ttyACM*")
        return 1
    if not ports:
        print("no serial devices found. Plug in a board (and check the USB driver).")
        return 0
    for p in ports:
        print(f"  {p.device:16} {p.description}")
    return 0


def run_pio(env, version, port, upload):
    """Invoke PlatformIO, injecting FW_VERSION via build flags."""
    # Call via `python -m platformio` so it works whether or not the venv's
    # `pio` shim is on PATH (it isn't, unless the venv is activated).
    cmd = [sys.executable, "-m", "platformio", "run", "-e", env]
    if upload:
        cmd += ["-t", "upload"]
        if port:
            cmd += ["--upload-port", port]

    proc_env = dict(os.environ)
    if version:
        # Appended after the ini build_flags, so it wins.
        proc_env["PLATFORMIO_BUILD_FLAGS"] = f'-DFW_VERSION=\\"{version}\\"'

    print(f"$ {' '.join(cmd)}" + (f"   (FW_VERSION={version})" if version else ""))
    try:
        return subprocess.run(cmd, cwd=FIRMWARE_DIR, env=proc_env).returncode
    except FileNotFoundError:
        print("PlatformIO not found -- run: pip install -r requirements.txt")
        return 127


def record_deploy(board, version):
    data = {}
    if REGISTRY.exists():
        data = json.loads(REGISTRY.read_text())
    data[board] = version
    REGISTRY.write_text(json.dumps(data, indent=2) + "\n")


def cmd_build(args):
    return run_pio(args.board, args.version, None, upload=False)


def cmd_flash(args):
    rc = run_pio(args.board, args.version, args.port, upload=True)
    if rc == 0 and args.version:
        record_deploy(args.board, args.version)
        print(f"deployed {args.board} -> {args.version}")
    return rc


def cmd_versions(_args):
    if not REGISTRY.exists():
        print("no deployments recorded yet.")
        return 0
    for board, version in json.loads(REGISTRY.read_text()).items():
        print(f"  {board:16} {version}")
    return 0


def main():
    parser = argparse.ArgumentParser(prog="iotctl", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("devices", help="list attached serial devices").set_defaults(func=cmd_devices)

    p_build = sub.add_parser("build", help="compile firmware without flashing")
    p_build.add_argument("--board", required=True, help="platformio env (e.g. sunfounder)")
    p_build.add_argument("--version", help="FW_VERSION to stamp in")
    p_build.set_defaults(func=cmd_build)

    p_flash = sub.add_parser("flash", help="build + flash over USB")
    p_flash.add_argument("--board", required=True, help="platformio env (e.g. sunfounder)")
    p_flash.add_argument("--version", help="FW_VERSION to stamp in (also recorded)")
    p_flash.add_argument("--port", help="serial port; auto-detected if omitted")
    p_flash.set_defaults(func=cmd_flash)

    sub.add_parser("versions", help="show recorded deployments").set_defaults(func=cmd_versions)

    load_env()   # pull WIFI_SSID/WIFI_PASS etc. from .env before building
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
