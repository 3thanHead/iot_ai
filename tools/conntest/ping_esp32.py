#!/usr/bin/env python3
"""Connectivity test: send a line to the ESP32 over TCP and print its reply.

Run from WSL once the board is flashed and you've read its IP off the serial
monitor (look for `[net] TCP server listening on <ip>:3333`).

    python ping_esp32.py 192.168.1.42
    python ping_esp32.py 192.168.1.42 -m "hello esp32"
    python ping_esp32.py 192.168.1.42 --count 5 --interval 1   # repeat (watch it blink)
"""
import argparse
import socket
import sys
import time


def send_once(host, port, message, timeout):
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall((message + "\n").encode())
        reply = s.recv(1024).decode(errors="replace").strip()
        return reply


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("host", help="ESP32 IP address (from the serial monitor)")
    ap.add_argument("--port", type=int, default=3333)
    ap.add_argument("-m", "--message", default="hello from wsl")
    ap.add_argument("--count", type=int, default=1, help="how many messages to send")
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between sends")
    ap.add_argument("--timeout", type=float, default=5.0)
    args = ap.parse_args()

    for i in range(1, args.count + 1):
        msg = args.message if args.count == 1 else f"{args.message} #{i}"
        try:
            reply = send_once(args.host, args.port, msg, args.timeout)
            print(f"sent: {msg!r}  ->  reply: {reply!r}")
        except (socket.timeout, OSError) as e:
            print(f"sent: {msg!r}  ->  ERROR: {e}", file=sys.stderr)
            return 1
        if i < args.count:
            time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
