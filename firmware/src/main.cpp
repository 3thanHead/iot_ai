// iot_ai ESP32 node -- connectivity test.
//
// Joins WiFi and runs a tiny TCP server. For each line a client sends, it
// blinks the LED (event-driven -- only when data arrives), echoes a reply, and
// logs to serial. Proves the WSL app <-> ESP32 round trip over WiFi.
//
// Credentials come from include/wifi_secrets.h (gitignored). Camera/AI come later.

#include <Arduino.h>
#include <WiFi.h>

#ifndef FW_VERSION
#define FW_VERSION "0.0.0-dev"
#endif

// WiFi credentials are injected at build time from environment variables
// (WIFI_SSID / WIFI_PASS) via platformio.ini -- see .env / .env.example.
// No secrets file lives in the tree. Empty values fail the build below.
#ifndef WIFI_SSID
#define WIFI_SSID ""
#endif
#ifndef WIFI_PASS
#define WIFI_PASS ""
#endif
static_assert(sizeof(WIFI_SSID) > 1, "WIFI_SSID is empty -- set it in .env (copy .env.example)");
static_assert(sizeof(WIFI_PASS) > 1, "WIFI_PASS is empty -- set it in .env (copy .env.example)");

// LED to flash when a message arrives. GPIO2 = conventional onboard pin; may not
// be a visible LED on this board -- the serial [rx]/[tx] log is the sure signal.
#ifndef LED_PIN
#define LED_PIN 2
#endif

#ifndef TCP_PORT
#define TCP_PORT 3333
#endif

WiFiServer server(TCP_PORT);

// Quick double-flash so a single message is visibly distinct.
static void blinkOnData() {
  for (int i = 0; i < 2; i++) {
    digitalWrite(LED_PIN, HIGH);
    delay(60);
    digitalWrite(LED_PIN, LOW);
    delay(60);
  }
}

static void connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("[wifi] connecting");
  while (WiFi.status() != WL_CONNECTED) {
    delay(400);
    Serial.print('.');
  }
  Serial.printf("\n[wifi] connected, ip=%s\n", WiFi.localIP().toString().c_str());
}

void setup() {
  Serial.begin(115200);
  delay(200);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);
  Serial.printf("\n[boot] iot_ai node  fw=%s\n", FW_VERSION);

  connectWiFi();
  server.begin();
  Serial.printf("[net] TCP server listening on %s:%d\n",
                WiFi.localIP().toString().c_str(), TCP_PORT);
}

void loop() {
  // Keep WiFi up.
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[wifi] dropped, reconnecting");
    connectWiFi();
    return;
  }

  WiFiClient client = server.available();
  if (!client) return;

  Serial.printf("[net] client %s connected\n", client.remoteIP().toString().c_str());
  while (client.connected()) {
    if (client.available()) {
      String msg = client.readStringUntil('\n');
      msg.trim();
      if (msg.length() == 0) continue;

      blinkOnData();                       // <-- flashes ONLY on incoming data
      Serial.printf("[rx] %s\n", msg.c_str());

      String reply = String("ESP32 fw=") + FW_VERSION + " received: " + msg;
      client.println(reply);
      Serial.printf("[tx] %s\n", reply.c_str());
    }
    delay(1);
  }
  client.stop();
  Serial.println("[net] client disconnected");
}
