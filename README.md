# iot_ai

ESP32 sensor/camera fleet with a gateway that deploys firmware updates to the
devices. The gateway is just a Linux/Windows host (mini PC now; Jetson when it
arrives) — nothing is Jetson-specific yet.

## Layout

```
firmware/              ESP32 firmware (PlatformIO, one env per board)
  platformio.ini       board definitions: `sunfounder` (WROOM-32E), `esp32-s3` (stub)
  src/main.cpp         WiFi + TCP server: blinks on incoming data, echoes a reply
tools/fleetctl/        the gateway-side deploy CLI (Python)
  fleetctl.py          build + flash boards over USB, track deployed versions
tools/conntest/        connectivity test
  ping_esp32.py        send a TCP message to the board, print its reply
.env.example           copy to .env; WiFi creds injected into the build
```

## Credentials

WiFi credentials are injected into the firmware **at build time** from env vars
(`WIFI_SSID` / `WIFI_PASS`) — there is no secrets file in the tree. `fleetctl`
loads them from a gitignored `.env`:

```bash
cp .env.example .env        # then edit WIFI_SSID / WIFI_PASS (2.4 GHz network)
```
(For a raw `pio run` outside fleetctl: `set -a; source .env; set +a` first.)

## Firmware delivery — phased

1. **Wired (now):** the gateway flashes a USB-attached board with esptool (via
   PlatformIO). The first flash onto any board is always wired.
2. **Wireless OTA (later):** firmware gains WiFi + HTTP OTA pull; the gateway
   hosts the binary + a version manifest and devices self-update. Same CLI gains
   an `ota serve` command.

## Quick start (gateway host)

```bash
pip install -r tools/fleetctl/requirements.txt
cp .env.example .env                                       # set WiFi creds

python tools/fleetctl/fleetctl.py devices                  # find the board
python tools/fleetctl/fleetctl.py flash --board sunfounder --version 0.2.0
pio device monitor -d firmware -b 115200                   # read the IP, then Ctrl+C
```

The serial monitor prints the board's IP:
```
[boot] iot_ai node  fw=0.2.0
[wifi] connected, ip=192.168.x.y
[net] TCP server listening on 192.168.x.y:3333
```
(If the monitor shows nothing, press the board's `EN` button — it only logs at
boot and when a message arrives.)

## Connectivity test

Send the board a message over WiFi; it blinks on receipt and echoes a reply:

```bash
python tools/conntest/ping_esp32.py 192.168.x.y -m "hello esp32"
python tools/conntest/ping_esp32.py 192.168.x.y --count 5 --interval 1   # watch it blink
```

## Notes

- Linux hosts see boards as `/dev/ttyUSB*` (WROOM-32E) or `/dev/ttyACM*` (S3).
  Add yourself to `dialout` for serial access: `sudo usermod -aG dialout $USER`.
- If upload won't connect, hold the board's `BOOT`/`IO0` button while it starts.
