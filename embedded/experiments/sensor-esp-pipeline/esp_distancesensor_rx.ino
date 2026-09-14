#include <WiFi.h>
#include <WiFiUdp.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ST7735.h>

// Network config — fill in before flashing
#define SSID        "YOUR_SSID"
#define PASSWORD    "YOUR_PASSWORD"
#define UDP_PORT    4210

// Display pins — confirm against ESP32S3 pinout before wiring
#define TFT_CS      17
#define TFT_DC      20
#define TFT_RST     21

#define DIST_MIN    20
#define DIST_MAX    1000
#define SEAT_W      20
#define SEAT_H      14
#define TRACK_Y     100
#define TRACK_X     5
#define TRACK_W     150

WiFiUDP udp;
Adafruit_ST7735 tft = Adafruit_ST7735(TFT_CS, TFT_DC, TFT_RST);

int lastSeatX = -1;
long lastDist = -100;

void drawSeat(int seatX, bool clear) {
  uint16_t color = clear ? ST77XX_BLACK : ST77XX_CYAN;
  tft.fillRect(seatX, TRACK_Y - SEAT_H / 2, SEAT_W, SEAT_H, color);
}

void setup() {
  Serial.begin(115200);

  tft.initR(INITR_BLACKTAB);
  tft.setRotation(1);
  tft.fillScreen(ST77XX_BLACK);
  tft.drawFastHLine(TRACK_X, TRACK_Y, TRACK_W, ST77XX_WHITE);

  WiFi.begin(SSID, PASSWORD);
  Serial.print("Connecting to WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println(" connected");
  Serial.println(WiFi.localIP());

  udp.begin(UDP_PORT);
}

void loop() {
  int packetSize = udp.parsePacket();
  if (packetSize) {
    char buf[64];
    int len = udp.read(buf, sizeof(buf) - 1);
    buf[len] = '\0';

    // parse CSV: "dist_mm,vel_ms"
    long dist = 0;
    float vel_ms = 0.0;
    sscanf(buf, "%ld,%f", &dist, &vel_ms);

    dist = constrain(dist, DIST_MIN, DIST_MAX);
    int seatX = map(dist, DIST_MIN, DIST_MAX, TRACK_X, TRACK_X + TRACK_W - SEAT_W);

    if (abs(dist - lastDist) > 20) {
      drawSeat(lastSeatX, true);
      tft.drawFastHLine(TRACK_X, TRACK_Y, TRACK_W, ST77XX_WHITE);
      drawSeat(seatX, false);
      lastSeatX = seatX;
      lastDist = dist;
    }

    tft.setTextSize(2);
    tft.setTextColor(ST77XX_WHITE, ST77XX_BLACK);

    tft.setCursor(0, 10);
    tft.print("Dist: ");
    tft.print(dist);
    tft.print(" mm   ");

    tft.setCursor(0, 50);
    tft.print("Vel:  ");
    tft.print(vel_ms, 2);
    tft.print(" m/s ");
  }
}