#include <Adafruit_GFX.h>
#include <Adafruit_ST7735.h>

#define TFT_CS   17
#define TFT_DC   20
#define TFT_RST  21
#define TRIG_PIN 2
#define ECHO_PIN 3

Adafruit_ST7735 tft = Adafruit_ST7735(TFT_CS, TFT_DC, TFT_RST);

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
  Serial1.begin(115200);
  pinMode(TRIG_PIN, OUTPUT);
  pinMode(ECHO_PIN, INPUT);
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

  Serial.print("Distance: ");
  Serial.print(dist);
  Serial.print(" mm  Vel: ");
  Serial.print(vel_ms, 2);
  Serial.println(" m/s");

  Serial1.print("Distance: ");
  Serial1.print(dist);
  Serial1.print(" mm  Vel: ");
  Serial1.print(vel_ms, 2);
  Serial1.println(" m/s");

  delay(50);
}