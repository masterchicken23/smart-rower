#include <WiFi.h>
#include <esp_now.h>

typedef struct {
  long distance_mm;
} SensorData;

SensorData data;

void onReceive(const esp_now_recv_info_t *info, const uint8_t *incomingData, int len) {
  memcpy(&data, incomingData, sizeof(data));
  Serial.print(millis());
  Serial.print(",");
  Serial.println(data.distance_mm);
}

void setup() {
  Serial.begin(115200);
  delay(3000);
  Serial.println("timestamp_ms,distance_mm");  // CSV header
  WiFi.mode(WIFI_STA);

  if (esp_now_init() != ESP_OK) {
    Serial.println("ESP-NOW init failed");
    return;
  }

  esp_now_register_recv_cb(onReceive);
}

void loop() {}