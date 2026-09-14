#include <WiFi.h>
#include <WiFiUdp.h>

// Network config — fill in before flashing
#define SSID        "YOUR_SSID"
#define PASSWORD    "YOUR_PASSWORD"
#define JETSON_IP   "YOUR_JETSON_IP"
#define UDP_PORT    4210

// Sensor pins — confirm against ESP32S3 pinout before wiring
#define TRIG_PIN    4
#define ECHO_PIN    5

WiFiUDP udp;

const int SAMPLES = 10;
long distances[SAMPLES];
unsigned long timestamps[SAMPLES];
int idx = 0;
bool bufferFull = false;

long getDistance() {
  digitalWrite(TRIG_PIN, LOW);
  delayMicroseconds(2);
  digitalWrite(TRIG_PIN, HIGH);
  delayMicroseconds(10);
  digitalWrite(TRIG_PIN, LOW);
  long duration = pulseIn(ECHO_PIN, HIGH);
  return duration * 0.17;
}

void setup() {
  Serial.begin(115200);
  pinMode(TRIG_PIN, OUTPUT);
  pinMode(ECHO_PIN, INPUT);

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
  unsigned long now = millis();
  long dist = getDistance();
  dist = constrain(dist, 20, 1000);

  distances[idx] = dist;
  timestamps[idx] = now;

  int vel5Idx = (idx - 4 + SAMPLES) % SAMPLES;
  long vel_mms = 0;
  if (bufferFull || idx >= 5) {
    unsigned long dt = timestamps[idx] - timestamps[vel5Idx];
    if (dt > 0)
      vel_mms = (distances[idx] - distances[vel5Idx]) * 1000 / (long)dt;
  }

  idx = (idx + 1) % SAMPLES;
  if (idx == 0) bufferFull = true;

  float vel_ms = vel_mms / 1000.0;

  // pack into a simple CSV string: "dist_mm,vel_ms"
  char packet[64];
  snprintf(packet, sizeof(packet), "%ld,%.2f", dist, vel_ms);

  udp.beginPacket(JETSON_IP, UDP_PORT);
  udp.print(packet);
  udp.endPacket();

  delay(50);
}