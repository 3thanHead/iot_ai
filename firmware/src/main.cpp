// iot_ai ESP32 node -- camera streaming.
//
// Initializes the OV2640 and serves MJPEG over HTTP so the gateway web service
// can relay frames to a browser:
//   http://<ip>/stream    multipart MJPEG stream
//   http://<ip>/snapshot  single JPEG
//
// Camera GPIOs default to the SunFounder CN0469D extension-board pinout and can
// be overridden per board with -D...=<n> in platformio.ini. WiFi creds are
// injected at build time (WIFI_SSID / WIFI_PASS) from .env -- see .env.example.

#include <Arduino.h>
#include <WiFi.h>
#include "esp_camera.h"
#include "esp_http_server.h"

#ifndef FW_VERSION
#define FW_VERSION "0.0.0-dev"
#endif

#ifndef WIFI_SSID
#define WIFI_SSID ""
#endif
#ifndef WIFI_PASS
#define WIFI_PASS ""
#endif
static_assert(sizeof(WIFI_SSID) > 1, "WIFI_SSID is empty -- set it in .env (copy .env.example)");
static_assert(sizeof(WIFI_PASS) > 1, "WIFI_PASS is empty -- set it in .env (copy .env.example)");

// --- OV2640 pinout: SunFounder CN0469D extension board (override via -D...) ---
#ifndef PWDN_GPIO
#define PWDN_GPIO 32
#endif
#ifndef RESET_GPIO
#define RESET_GPIO 33
#endif
#ifndef XCLK_GPIO
#define XCLK_GPIO 0
#endif
#ifndef SIOD_GPIO
#define SIOD_GPIO 26
#endif
#ifndef SIOC_GPIO
#define SIOC_GPIO 27
#endif
#ifndef VSYNC_GPIO
#define VSYNC_GPIO 25
#endif
#ifndef HREF_GPIO
#define HREF_GPIO 23
#endif
#ifndef PCLK_GPIO
#define PCLK_GPIO 22
#endif
#ifndef Y2_GPIO
#define Y2_GPIO 5
#endif
#ifndef Y3_GPIO
#define Y3_GPIO 18
#endif
#ifndef Y4_GPIO
#define Y4_GPIO 19
#endif
#ifndef Y5_GPIO
#define Y5_GPIO 21
#endif
#ifndef Y6_GPIO
#define Y6_GPIO 36
#endif
#ifndef Y7_GPIO
#define Y7_GPIO 39
#endif
#ifndef Y8_GPIO
#define Y8_GPIO 34
#endif
#ifndef Y9_GPIO
#define Y9_GPIO 35
#endif

static httpd_handle_t http_server = nullptr;
#define PART_BOUNDARY "iot_ai_frame"

// Camera quality tuning -- override per board via -D in platformio.ini.
// Higher resolution + lower quality-number = sharper frames for recognition,
// at the cost of more data over WiFi. Options: FRAMESIZE_VGA(640x480),
// SVGA(800x600), XGA(1024x768), SXGA(1280x1024), UXGA(1600x1200).
#ifndef CAM_FRAMESIZE
#define CAM_FRAMESIZE FRAMESIZE_SVGA
#endif
#ifndef CAM_JPEG_QUALITY
#define CAM_JPEG_QUALITY 11
#endif

static bool initCamera() {
  camera_config_t c = {};
  c.ledc_channel = LEDC_CHANNEL_0;
  c.ledc_timer = LEDC_TIMER_0;
  c.pin_d0 = Y2_GPIO;
  c.pin_d1 = Y3_GPIO;
  c.pin_d2 = Y4_GPIO;
  c.pin_d3 = Y5_GPIO;
  c.pin_d4 = Y6_GPIO;
  c.pin_d5 = Y7_GPIO;
  c.pin_d6 = Y8_GPIO;
  c.pin_d7 = Y9_GPIO;
  c.pin_xclk = XCLK_GPIO;
  c.pin_pclk = PCLK_GPIO;
  c.pin_vsync = VSYNC_GPIO;
  c.pin_href = HREF_GPIO;
  c.pin_sccb_sda = SIOD_GPIO;
  c.pin_sccb_scl = SIOC_GPIO;
  c.pin_pwdn = PWDN_GPIO;
  c.pin_reset = RESET_GPIO;
  c.xclk_freq_hz = 20000000;
  c.pixel_format = PIXFORMAT_JPEG;

  if (psramFound()) {
    c.frame_size = CAM_FRAMESIZE;
    c.jpeg_quality = CAM_JPEG_QUALITY;
    c.fb_count = 2;
    c.fb_location = CAMERA_FB_IN_PSRAM;
    c.grab_mode = CAMERA_GRAB_LATEST;
  } else {
    c.frame_size = FRAMESIZE_QVGA;  // 320x240 fallback without PSRAM
    c.jpeg_quality = 15;
    c.fb_count = 1;
    c.fb_location = CAMERA_FB_IN_DRAM;
  }

  esp_err_t err = esp_camera_init(&c);
  if (err != ESP_OK) {
    Serial.printf("[cam] init failed 0x%x -- check the FFC ribbon seating/orientation\n", err);
    return false;
  }
  Serial.println("[cam] OV2640 initialized");
  return true;
}

// GET /snapshot -> single JPEG
static esp_err_t snapshotHandler(httpd_req_t *req) {
  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) return httpd_resp_send_500(req);
  httpd_resp_set_type(req, "image/jpeg");
  httpd_resp_set_hdr(req, "Content-Disposition", "inline; filename=snapshot.jpg");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  esp_err_t res = httpd_resp_send(req, (const char *)fb->buf, fb->len);
  esp_camera_fb_return(fb);
  return res;
}

// GET /stream -> multipart MJPEG
static esp_err_t streamHandler(httpd_req_t *req) {
  httpd_resp_set_type(req, "multipart/x-mixed-replace;boundary=" PART_BOUNDARY);
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");

  char header[96];
  while (true) {
    camera_fb_t *fb = esp_camera_fb_get();
    if (!fb) return ESP_FAIL;

    int n = snprintf(header, sizeof(header),
                     "\r\n--" PART_BOUNDARY "\r\n"
                     "Content-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n",
                     fb->len);
    esp_err_t res = httpd_resp_send_chunk(req, header, n);
    if (res == ESP_OK)
      res = httpd_resp_send_chunk(req, (const char *)fb->buf, fb->len);
    esp_camera_fb_return(fb);
    if (res != ESP_OK) break;  // client disconnected
  }
  return ESP_OK;
}

static void startHttp() {
  httpd_config_t config = HTTPD_DEFAULT_CONFIG();
  config.server_port = 80;
  // Be patient with a briefly-slow client (motion bursts) before dropping the
  // stream -- defaults are ~5s; give the send/recv more headroom.
  config.send_wait_timeout = 15;
  config.recv_wait_timeout = 15;
  if (httpd_start(&http_server, &config) != ESP_OK) {
    Serial.println("[net] HTTP server failed to start");
    return;
  }
  httpd_uri_t snap = {"/snapshot", HTTP_GET, snapshotHandler, nullptr};
  httpd_uri_t stream = {"/stream", HTTP_GET, streamHandler, nullptr};
  httpd_register_uri_handler(http_server, &snap);
  httpd_register_uri_handler(http_server, &stream);
}

static void connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // disable modem power-save: it causes periodic MJPEG stalls
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
  Serial.printf("\n[boot] iot_ai camera node  fw=%s\n", FW_VERSION);

  if (!initCamera()) {
    delay(5000);
    ESP.restart();
  }
  connectWiFi();
  startHttp();
  Serial.printf("[net] stream:   http://%s/stream\n", WiFi.localIP().toString().c_str());
  Serial.printf("[net] snapshot: http://%s/snapshot\n", WiFi.localIP().toString().c_str());
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[wifi] dropped, reconnecting");
    connectWiFi();
  }
  delay(1000);
}
